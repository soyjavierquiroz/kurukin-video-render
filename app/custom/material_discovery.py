"""Multi-provider material discovery without downloading or selecting assets."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Mapping, Sequence

from app.custom.asset_search_v2 import build_visual_queries_v2
from app.custom.kurukin_asset_hub import (
    KurukinAssetHubAuthError,
    KurukinAssetHubError,
    KurukinAssetHubUnavailableError,
    KurukinAssetHubValidationError,
    KurukinAssetProvider,
    dedupe_key as asset_hub_dedupe_key,
    validate_asset_uid,
)
from app.custom.material_source_policy import (
    CatalogExpansionRequired,
    MaterialSourcePolicy,
    PROVIDER_ASSET_HUB,
    PROVIDER_LOCAL,
    build_discovery_plan,
)
from app.custom.kurukin_local_visual_picker import pick_local_visual_for_intent
from app.services import material


_FORBIDDEN_SOURCE_KEY_PARTS = (
    "drive_file_id", "remote_path", "rclone_remote", "target_path",
    "credential", "api_key", "apikey", "token", "password", "secret",
    "authorization", "auth",
)
_VISUAL_SUBJECT_WORDS = {
    "adulto", "adulta", "hombre", "mujer", "nino", "nina", "niño", "niña",
    "persona", "personas",
}
_CONNECTOR_WORDS = {"con", "de", "del", "la", "las", "los", "un", "una", "y"}
_PREVIEW_SOURCE_KEYS = (
    "thumbnail_url", "thumbnail", "preview_url", "preview", "poster_url", "poster",
    "image_url", "image", "keyframe_url", "keyframe", "cover_url", "cover",
    "source_thumbnail_url", "source_thumbnail",
)
TITLE_PREFERRED_MIN_CANDIDATES = 4


class MaterialDiscoveryError(RuntimeError):
    """No usable provider completed discovery."""


class AssetHubExclusionNotEnforceable(MaterialDiscoveryError):
    """The current Asset Hub contract cannot safely enforce an exclusion."""


@dataclass(frozen=True)
class MaterialCandidate:
    provider: str
    canonical_id: str
    dedupe_key: str
    search_term: str
    rank: int | None = None
    url: str | None = None
    duration: float | None = None
    width: int | None = None
    height: int | None = None
    orientation: str | None = None
    filename: str | None = None
    source_info: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DiscoveryDiagnostic:
    provider: str
    term: str | None
    status: str
    message: str
    candidate_count: int | None = None


@dataclass(frozen=True)
class MaterialDiscoveryResult:
    candidates: tuple[MaterialCandidate, ...]
    diagnostics: tuple[DiscoveryDiagnostic, ...]
    providers_attempted: tuple[str, ...]
    providers_succeeded: tuple[str, ...]
    terms_used: dict[str, tuple[str, ...]]


def _normalize_terms(values: Sequence[str] | None) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, str):
        values = (values,)
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return tuple(result)


def _term_words(term: str) -> list[str]:
    return re.findall(r"[\wáéíóúüñÁÉÍÓÚÜÑ]+", term, flags=re.UNICODE)


def _simplify_asset_hub_retry_term(term: str) -> str:
    words = _term_words(term)
    if len(words) <= 1:
        return ""
    lowered = [word.lower() for word in words]
    if lowered[0] == "pareja":
        return words[0]
    if lowered[0] in _VISUAL_SUBJECT_WORDS:
        for word, clean in zip(reversed(words), reversed(lowered)):
            if clean not in _VISUAL_SUBJECT_WORDS and clean not in _CONNECTOR_WORDS:
                return word
    for word, clean in zip(words, lowered):
        if clean not in _CONNECTOR_WORDS:
            return word
    return words[0]


def _exclusive_title_from_source_policy(source_policy: Mapping[str, Any] | None) -> str:
    sources = (source_policy or {}).get("sources")
    if not isinstance(sources, list) or len(sources) != 1:
        return ""
    source = sources[0]
    if not isinstance(source, Mapping) or str(source.get("scope") or "").strip() != "title":
        return ""
    return str(source.get("title") or "").strip()


def _source_policy_sources(source_policy: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    sources = (source_policy or {}).get("sources")
    return [source for source in sources if isinstance(source, Mapping)] if isinstance(sources, list) else []


def _policy_with_sources(sources: Sequence[Mapping[str, Any]]) -> dict[str, list[dict[str, str]]]:
    return {"sources": [dict(source) for source in sources]}


def _title_preferred_policies(
    source_policy: Mapping[str, Any] | None,
) -> tuple[dict[str, list[dict[str, str]]] | None, dict[str, list[dict[str, str]]] | None]:
    sources = _source_policy_sources(source_policy)
    title_sources = [source for source in sources if str(source.get("scope") or "").strip() == "title"]
    generic_sources = [source for source in sources if str(source.get("scope") or "").strip() == "generic"]
    if title_sources and generic_sources:
        return _policy_with_sources(title_sources), _policy_with_sources(generic_sources)
    return None, None


def _safe_number(value: Any, converter: type[int] | type[float]) -> int | float | None:
    try:
        result = converter(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def _safe_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _is_forbidden_key(key: Any) -> bool:
    normalized = "".join(char for char in str(key).lower() if char.isalnum() or char == "_")
    compact = normalized.replace("_", "")
    forbidden = {part.replace("_", "") for part in _FORBIDDEN_SOURCE_KEY_PARTS}
    # Exact keys handle ordinary metadata (notably ``author``) without false
    # positives, while compound credential names such as ``refresh_token`` or
    # ``clientSecret`` remain redacted.
    return compact in forbidden or compact.endswith("token") or compact.endswith("secret")


def _sanitize_source_info(value: Any) -> dict[str, Any]:
    """Copy public metadata while removing transport paths and credentials."""
    if not isinstance(value, Mapping):
        return {}

    def clean(item: Any) -> Any:
        if isinstance(item, Mapping):
            return {str(key): clean(nested) for key, nested in item.items() if not _is_forbidden_key(key)}
        if isinstance(item, (list, tuple)):
            return [clean(nested) for nested in item]
        return item

    return clean(value)


def _preview_source_info(source: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: source[key]
        for key in _PREVIEW_SOURCE_KEYS
        if key in source and source[key] not in (None, "")
    }


def _orientation(width: int | None, height: int | None) -> str | None:
    if width is None or height is None or width <= 0 or height <= 0:
        return None
    if height > width:
        return "portrait"
    if width > height:
        return "landscape"
    return "square"


def _stock_candidate(item: Any, *, provider: str, term: str, rank: int) -> MaterialCandidate | None:
    info = _sanitize_source_info(getattr(item, "source_info", None))
    asset_id = None
    for name in ("asset_id", "video_id", "id"):
        value = getattr(item, name, None)
        if value not in (None, ""):
            asset_id = _safe_text(value)
            break
    asset_id = asset_id or _safe_text(info.get("asset_id"))
    if not asset_id:
        # A provider namespace alone is not a canonical identity.  Ignore
        # malformed records rather than fabricate an ID from a download URL.
        return None
    rendition = info.get("rendition") if isinstance(info.get("rendition"), Mapping) else {}
    width = _safe_number(getattr(item, "width", None), int) or _safe_number(rendition.get("width"), int)
    height = _safe_number(getattr(item, "height", None), int) or _safe_number(rendition.get("height"), int)
    duration = _safe_number(getattr(item, "duration", None), float)
    canonical_id = f"{provider}:{asset_id}"
    return MaterialCandidate(
        provider=provider,
        canonical_id=canonical_id,
        dedupe_key=canonical_id,
        search_term=term,
        rank=rank,
        url=_safe_text(getattr(item, "url", None)),
        duration=duration,
        width=width,
        height=height,
        orientation=_orientation(width, height),
        filename=_safe_text(getattr(item, "filename", None)),
        source_info=info,
    )


def _asset_hub_candidate(asset: Mapping[str, Any], *, term: str, rank: int) -> MaterialCandidate:
    asset_uid = validate_asset_uid(asset.get("asset_uid"))
    source = _sanitize_source_info(asset)
    width = _safe_number(asset.get("width"), int)
    height = _safe_number(asset.get("height"), int)
    orientation = _safe_text(asset.get("orientation")) or _orientation(width, height)
    safe_info = {
        key: source[key] for key in (
            "filename", "media_type", "orientation", "duration", "width", "height",
            "scope", "brand", "title", "title_type",
        )
        if key in source
    }
    safe_info.update(_preview_source_info(source))
    return MaterialCandidate(
        provider=PROVIDER_ASSET_HUB,
        canonical_id=asset_uid,
        dedupe_key=asset_hub_dedupe_key(asset_uid),
        search_term=term,
        rank=rank,
        url=_safe_text(asset.get("url")),
        duration=_safe_number(asset.get("duration"), float),
        width=width,
        height=height,
        orientation=orientation,
        filename=_safe_text(asset.get("filename")),
        source_info=safe_info,
    )


def _title_only_asset_hub_candidate(asset: Mapping[str, Any], *, title: str, rank: int) -> MaterialCandidate:
    candidate = _asset_hub_candidate(asset, term=title, rank=rank)
    return MaterialCandidate(
        provider=candidate.provider,
        canonical_id=candidate.canonical_id,
        dedupe_key=candidate.dedupe_key,
        search_term=candidate.search_term,
        rank=candidate.rank,
        url=candidate.url,
        duration=candidate.duration,
        width=candidate.width,
        height=candidate.height,
        orientation=candidate.orientation,
        filename=candidate.filename,
        source_info={**candidate.source_info, "discovery_fallback": "title_only"},
    )


def _safe_error_message(exc: Exception) -> str:
    message = str(exc or "").strip()
    lowered = message.lower()
    if any(token in lowered for token in _FORBIDDEN_SOURCE_KEY_PARTS):
        return f"{type(exc).__name__}: <redacted>"
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def _is_fatal_asset_hub_error(exc: Exception) -> bool:
    if isinstance(exc, (KurukinAssetHubAuthError, KurukinAssetHubValidationError)):
        return True
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", getattr(exc, "status_code", None))
    try:
        return int(status) in (401, 403, 404, 422)
    except (TypeError, ValueError):
        return False


def _asset_hub_found_candidates(
    provider: KurukinAssetProvider,
    *,
    query: str,
    source_policy: Mapping[str, Any],
    original_term: str,
    limit: int = 20,
) -> list[MaterialCandidate]:
    assets = provider.search(query=query, source_policy=source_policy, limit=limit)
    candidates = []
    for index, asset in enumerate(assets):
        if not _asset_hub_is_video_asset(asset):
            continue
        candidate = _asset_hub_candidate(asset, term=query, rank=index)
        candidate.source_info["visual_query_source_term"] = original_term
        candidates.append(candidate)
    return candidates


def _asset_hub_is_video_asset(asset: Mapping[str, Any]) -> bool:
    media_type = str(asset.get("media_type") or "").strip().lower()
    return not media_type or media_type == "video"


def _asset_hub_search_queries(term: str) -> tuple[str, ...]:
    queries = list(build_visual_queries_v2(term, [term]))
    if term and term not in queries:
        queries.insert(0, term)
    simplified = _simplify_asset_hub_retry_term(term)
    if simplified and simplified not in queries:
        queries.append(simplified)
    return tuple(dict.fromkeys(queries))


def _rank_asset_hub_candidates(
    candidates: list[MaterialCandidate],
    *,
    video_aspect: str,
    clip_duration: int | float,
) -> list[MaterialCandidate]:
    def key(pair: tuple[int, MaterialCandidate]) -> tuple[int, int, int]:
        from app.custom.material_selection import _duration_score, orientation_score

        index, item = pair
        return (
            -orientation_score(item, video_aspect),
            -_duration_score(item, float(clip_duration or 0) or 1.0),
            index,
        )

    return [item for _, item in sorted(enumerate(candidates), key=key)]


def _merge_asset_hub_query_results(
    batches: Sequence[Sequence[MaterialCandidate]],
    *,
    video_aspect: str,
    clip_duration: int | float,
) -> list[MaterialCandidate]:
    merged: list[MaterialCandidate] = []
    seen: set[str] = set()
    for batch in batches:
        for candidate in batch:
            if candidate.dedupe_key in seen:
                continue
            seen.add(candidate.dedupe_key)
            merged.append(candidate)
    return _rank_asset_hub_candidates(
        merged,
        video_aspect=video_aspect,
        clip_duration=clip_duration,
    )


def _asset_hub_multi_query_candidates(
    provider: KurukinAssetProvider,
    *,
    term: str,
    source_policy: Mapping[str, Any],
    video_aspect: str,
    clip_duration: int | float,
    limit: int = 20,
) -> tuple[list[MaterialCandidate], list[DiscoveryDiagnostic]]:
    batches: list[list[MaterialCandidate]] = []
    diagnostics: list[DiscoveryDiagnostic] = []
    for query in _asset_hub_search_queries(term):
        found = _asset_hub_found_candidates(
            provider,
            query=query,
            source_policy=source_policy,
            original_term=term,
            limit=limit,
        )
        diagnostics.append(DiscoveryDiagnostic(PROVIDER_ASSET_HUB, query, "success", "ok", len(found)))
        batches.append(found)
    return (
        _merge_asset_hub_query_results(
            batches,
            video_aspect=video_aspect,
            clip_duration=clip_duration,
        ),
        diagnostics,
    )


def _search_title_preferred_asset_hub(
    provider: KurukinAssetProvider,
    *,
    term: str,
    title_policy: Mapping[str, Any],
    generic_policy: Mapping[str, Any],
    video_aspect: str,
    clip_duration: int | float,
    limit: int,
) -> tuple[list[MaterialCandidate], list[DiscoveryDiagnostic]]:
    title_found, diagnostics = _asset_hub_multi_query_candidates(
        provider,
        term=term,
        source_policy=title_policy,
        video_aspect=video_aspect,
        clip_duration=clip_duration,
        limit=limit,
    )
    if len(title_found) >= TITLE_PREFERRED_MIN_CANDIDATES:
        return title_found, diagnostics

    generic_found, generic_diagnostics = _asset_hub_multi_query_candidates(
        provider,
        term=term,
        source_policy=generic_policy,
        video_aspect=video_aspect,
        clip_duration=clip_duration,
        limit=limit,
    )
    merged = _merge_asset_hub_query_results(
        (title_found, generic_found),
        video_aspect=video_aspect,
        clip_duration=clip_duration,
    )
    return merged, diagnostics + generic_diagnostics


def _dedupe(candidates: list[MaterialCandidate]) -> tuple[MaterialCandidate, ...]:
    seen: set[str] = set()
    result: list[MaterialCandidate] = []
    for candidate in candidates:
        if candidate.dedupe_key not in seen:
            seen.add(candidate.dedupe_key)
            result.append(candidate)
    return tuple(result)


def discover_material_candidates(
    *,
    policy: MaterialSourcePolicy,
    stock_terms: Sequence[str],
    asset_hub_terms: Sequence[str] | None = None,
    video_aspect: str = "9:16",
    minimum_duration: int | float = 0,
    asset_hub_provider: KurukinAssetProvider | None = None,
) -> MaterialDiscoveryResult:
    """Search every enabled remote provider and return normalized candidates.

    This does not download, materialize, bundle, rank across providers, or make
    a final asset choice.
    """
    plan = build_discovery_plan(policy)
    normalized_stock_terms = _normalize_terms(stock_terms)
    normalized_hub_terms = _normalize_terms(asset_hub_terms) if asset_hub_terms is not None else normalized_stock_terms
    terms_used = {"stock": normalized_stock_terms, "asset_hub": normalized_hub_terms}
    if plan["asset_hub"]["requires_catalog_expansion"]:
        raise CatalogExpansionRequired("Asset Hub all_titles/all_brands requires catalog expansion before discovery")

    candidates: list[MaterialCandidate] = []
    diagnostics: list[DiscoveryDiagnostic] = []
    attempted: list[str] = []
    succeeded: list[str] = []
    technical_failures = 0
    remote_attempts = 0
    remote_provider_count = len(plan["external_providers"]) + int(plan["asset_hub"]["enabled"])

    for provider in policy.providers.enabled:
        attempted.append(provider)
        if provider == PROVIDER_LOCAL:
            local_count = 0
            for rank, term in enumerate(normalized_stock_terms, start=1):
                # The existing picker only searches its explicit allow-list and
                # applies its tested semantic ranking; do not crawl arbitrary paths.
                picked = pick_local_visual_for_intent({"topic": term, "visual_keywords": [term]})
                if not picked:
                    continue
                path = _safe_text(picked.get("path"))
                if not path:
                    continue
                candidates.append(MaterialCandidate(
                    provider=PROVIDER_LOCAL, canonical_id=f"local:{path}", dedupe_key=f"local:{path}",
                    search_term=term, rank=rank, url=path, filename=path.rsplit("/", 1)[-1],
                    source_info=_sanitize_source_info({"source": picked.get("source"), "type": picked.get("type")})))
                local_count += 1
            diagnostics.append(DiscoveryDiagnostic(
                provider, None, "success" if local_count else "pending_adapter",
                "local safe picker searched" if local_count else "local safe picker found no usable visual", local_count))
            if local_count:
                succeeded.append(provider)
            continue
        terms = normalized_hub_terms if provider == PROVIDER_ASSET_HUB else normalized_stock_terms
        if provider == PROVIDER_ASSET_HUB:
            active_provider = asset_hub_provider or KurukinAssetProvider()
            source_policy = plan["asset_hub"]["source_policy"]
        for term in terms:
            found: list[MaterialCandidate] = []
            try:
                if provider != PROVIDER_ASSET_HUB:
                    remote_attempts += 1
                    items = material.search_videos_for_provider(provider, term, minimum_duration, video_aspect)
                    found = [candidate for index, item in enumerate(items) if (candidate := _stock_candidate(item, provider=provider, term=term, rank=index)) is not None]
                else:
                    title_policy, generic_policy = _title_preferred_policies(source_policy)
                    query_count = len(_asset_hub_search_queries(term))
                    remote_attempts += query_count
                    if title_policy and generic_policy:
                        found, hub_diagnostics = _search_title_preferred_asset_hub(
                            active_provider,
                            term=term,
                            title_policy=title_policy,
                            generic_policy=generic_policy,
                            video_aspect=video_aspect,
                            clip_duration=minimum_duration,
                            limit=20,
                        )
                    else:
                        found, hub_diagnostics = _asset_hub_multi_query_candidates(
                            active_provider,
                            term=term,
                            source_policy=source_policy,
                            video_aspect=video_aspect,
                            clip_duration=minimum_duration,
                            limit=20,
                        )
                    diagnostics.extend(hub_diagnostics)
            except Exception as exc:
                if provider == PROVIDER_ASSET_HUB and _is_fatal_asset_hub_error(exc):
                    raise
                technical_failures += 1
                diagnostics.append(DiscoveryDiagnostic(provider, term, "failure", _safe_error_message(exc), None))
                if remote_provider_count == 1:
                    raise MaterialDiscoveryError(f"material provider '{provider}' failed") from exc
                continue
            if provider != PROVIDER_ASSET_HUB:
                diagnostics.append(DiscoveryDiagnostic(provider, term, "success", "ok", len(found)))
            candidates.extend(found)
            if provider not in succeeded:
                succeeded.append(provider)

    if remote_attempts and technical_failures == remote_attempts:
        raise MaterialDiscoveryError("all enabled remote material providers failed")
    return MaterialDiscoveryResult(_dedupe(candidates), tuple(diagnostics), tuple(attempted), tuple(succeeded), terms_used)



def discover_asset_hub_review_reserve_candidates(
    *,
    policy: MaterialSourcePolicy,
    terms: Sequence[str],
    video_aspect: str = "9:16",
    asset_hub_provider: KurukinAssetProvider | None = None,
    limit_per_term: int = 100,
) -> MaterialDiscoveryResult:
    """
    Build a large Human Review reserve pool.

    Unlike normal autonomous discovery, Human Review needs enough
    unique assets to show alternatives/backups for every scene.

    Search each semantic term independently, then globally dedupe.
    """

    plan = build_discovery_plan(policy)

    if not plan["asset_hub"]["enabled"]:
        return MaterialDiscoveryResult(
            (),
            (),
            (),
            (),
            {
                "stock": (),
                "asset_hub": (),
            },
        )

    if plan["asset_hub"]["requires_catalog_expansion"]:
        raise CatalogExpansionRequired(
            "Asset Hub all_titles/all_brands requires "
            "catalog expansion before discovery"
        )

    source_policy = plan["asset_hub"]["source_policy"]
    normalized_terms = _normalize_terms(terms)

    if not normalized_terms:
        return MaterialDiscoveryResult(
            (),
            (),
            (PROVIDER_ASSET_HUB,),
            (),
            {
                "stock": (),
                "asset_hub": (),
            },
        )

    provider = (
        asset_hub_provider
        or KurukinAssetProvider()
    )

    candidates: list[MaterialCandidate] = []
    diagnostics: list[DiscoveryDiagnostic] = []

    for term in normalized_terms:
        try:
            title_policy, generic_policy = _title_preferred_policies(source_policy)
            if title_policy and generic_policy:
                found, term_diagnostics = _search_title_preferred_asset_hub(
                    provider,
                    term=term,
                    title_policy=title_policy,
                    generic_policy=generic_policy,
                    video_aspect=video_aspect,
                    clip_duration=limit_per_term,
                    limit=limit_per_term,
                )
            else:
                found, term_diagnostics = _asset_hub_multi_query_candidates(
                    provider,
                    term=term,
                    source_policy=source_policy,
                    video_aspect=video_aspect,
                    clip_duration=limit_per_term,
                    limit=limit_per_term,
                )
        except Exception as exc:
            if _is_fatal_asset_hub_error(exc):
                raise

            diagnostics.append(
                DiscoveryDiagnostic(
                    PROVIDER_ASSET_HUB,
                    term,
                    "failure",
                    _safe_error_message(exc),
                    None,
                )
            )
            continue

        diagnostics.extend(
            DiscoveryDiagnostic(
                item.provider,
                item.term,
                item.status,
                "human_review_reserve",
                item.candidate_count,
            )
            for item in term_diagnostics
        )

        candidates.extend(found)

    unique = _dedupe(candidates)

    return MaterialDiscoveryResult(
        candidates=unique,
        diagnostics=tuple(diagnostics),
        providers_attempted=(PROVIDER_ASSET_HUB,),
        providers_succeeded=(
            (PROVIDER_ASSET_HUB,)
            if unique
            else ()
        ),
        terms_used={
            "stock": (),
            "asset_hub": normalized_terms,
        },
    )


def discover_asset_hub_title_fallback_candidates(
    *,
    policy: MaterialSourcePolicy,
    video_aspect: str = "9:16",
    asset_hub_provider: KurukinAssetProvider | None = None,
    limit: int = 20,
) -> MaterialDiscoveryResult:
    """Search the exclusive Asset Hub title once as a global low-priority fallback."""
    plan = build_discovery_plan(policy)
    if not plan["asset_hub"]["enabled"]:
        return MaterialDiscoveryResult((), (), (), (), {"stock": (), "asset_hub": ()})
    if plan["asset_hub"]["requires_catalog_expansion"]:
        raise CatalogExpansionRequired("Asset Hub all_titles/all_brands requires catalog expansion before discovery")

    source_policy = plan["asset_hub"]["source_policy"]
    title = _exclusive_title_from_source_policy(source_policy)
    if not title:
        return MaterialDiscoveryResult((), (), (PROVIDER_ASSET_HUB,), (), {"stock": (), "asset_hub": ()})

    active_provider = asset_hub_provider or KurukinAssetProvider()
    try:
        assets = active_provider.search(
            query=title,
            source_policy=source_policy,
            limit=limit,
        )
    except Exception as exc:
        if _is_fatal_asset_hub_error(exc):
            raise
        raise MaterialDiscoveryError("material provider 'asset_hub' failed") from exc

    candidates = [
        _title_only_asset_hub_candidate(asset, title=title, rank=index)
        for index, asset in enumerate(assets)
    ]
    candidates = _rank_asset_hub_candidates(
        candidates,
        video_aspect=video_aspect,
        clip_duration=limit,
    )
    diagnostics = (DiscoveryDiagnostic(PROVIDER_ASSET_HUB, title, "success", "global_title_only_fallback", len(candidates)),)
    succeeded = (PROVIDER_ASSET_HUB,) if candidates else ()
    return MaterialDiscoveryResult(_dedupe(candidates), diagnostics, (PROVIDER_ASSET_HUB,), succeeded, {"stock": (), "asset_hub": (title,)})
