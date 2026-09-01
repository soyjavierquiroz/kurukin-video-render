"""Multi-provider material discovery without downloading or selecting assets."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
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
from app.custom.material_selection import _is_orientation_compatible
from app.custom.material_provider_availability import (
    native_stock_config_key,
    native_stock_provider_configured,
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
    raw_count: int | None = None
    normalized_count: int | None = None
    orientation_valid_count: int | None = None


@dataclass(frozen=True)
class MaterialDiscoveryResult:
    candidates: tuple[MaterialCandidate, ...]
    diagnostics: tuple[DiscoveryDiagnostic, ...]
    providers_attempted: tuple[str, ...]
    providers_succeeded: tuple[str, ...]
    terms_used: dict[str, tuple[str, ...]]


def provider_diagnostics_for_review(
    result: MaterialDiscoveryResult,
    *,
    enabled_providers: Sequence[str],
    selection: Any = None,
) -> list[dict[str, Any]]:
    """Return secret-safe, durable funnel data for a review plan."""
    diagnostics = tuple(getattr(result, "diagnostics", ()) or ())
    candidates = tuple(getattr(result, "candidates", ()) or ())
    decisions = tuple(getattr(selection, "decisions", ()) or ())
    payload: list[dict[str, Any]] = []
    for provider in enabled_providers:
        entries = [item for item in diagnostics if item.provider == provider]
        failures = [item for item in entries if item.status == "error"]
        config_missing = [item for item in entries if item.status == "config_missing"]
        unavailable = [item for item in entries if item.status == "unavailable"]
        raw = sum(int(item.raw_count or 0) for item in entries)
        normalized = sum(int(item.normalized_count or 0) for item in entries)
        orientation_valid = sum(int(item.orientation_valid_count or item.candidate_count or 0) for item in entries)
        deduped = sum(1 for item in candidates if getattr(item, "provider", "") == provider)
        selected = sum(1 for item in decisions if getattr(getattr(item, "candidate", None), "provider", "") == provider)
        status = (
            "error" if failures else
            "config_missing" if config_missing else
            "unavailable" if unavailable else
            ("success" if deduped else "empty")
        )
        problem = (failures or config_missing or unavailable)
        payload.append({
            "provider": provider,
            "attempted": provider in getattr(result, "providers_attempted", ()),
            "status": status,
            "adapter_status": "operational" if status not in {"config_missing", "unavailable"} else status,
            "raw_count": raw,
            "normalized_count": normalized,
            "orientation_valid_count": orientation_valid,
            "deduped_count": deduped,
            "candidate_count": deduped,
            "selected_count": selected,
            "review_visible_count": 0,
            "error_class": problem[0].message.split(":", 1)[0] if problem else "",
            "error": problem[0].message if problem else "",
        })
    return payload


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
    # Normalize public stock metadata into the same review/ranking contract as
    # Asset Hub.  Adapters need not provide every field: unavailable signals
    # are omitted and the ranking renormalizes over what is present.
    for field in (
        "title", "description", "tags", "keywords", "duration", "width", "height",
        "orientation", "preview", "thumbnail_url", "source_identity", "source_url",
        "rights_state", "rights", "license", "media_type", "visual_description",
    ):
        value = getattr(item, field, None)
        if value not in (None, ""):
            info.setdefault(field, value)
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
    info.setdefault("provider_asset_id", asset_id)
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
            "primary_theme", "primary_topic", "visual_description",
            "action_description", "contains_people", "people_count",
            "visual_presentation", "visual_presentation_confidence",
            "person_visibility",
            # Native Asset Hub metadata retained verbatim for review/ranking.
            "tags", "search_text", "embedding_text", "presentation",
            "editorial_quality", "quality_score", "vertical_suitability",
            "horizontal_suitability", "camera_motion", "rights_state", "rights",
            "production_eligible", "authorized", "provenance_state",
            "safe_for_subtitles", "safe_for_text_overlay", "provider_asset_id", "source_identity",
            "source_url", "source_provider", "provider",
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
    video_aspect: str,
    limit: int = 20,
    request_attempts: int | None = None,
    search_cache: dict[tuple[str, str, int], list[dict[str, Any]]] | None = None,
) -> list[MaterialCandidate]:
    cache_key = (query, json.dumps(source_policy, sort_keys=True), limit)
    if search_cache is not None and cache_key in search_cache:
        assets = search_cache[cache_key]
    elif request_attempts is not None and isinstance(provider, KurukinAssetProvider):
        assets = provider.search(
            query=query, source_policy=source_policy, limit=limit,
            max_attempts=request_attempts,
        )
    else:
        assets = provider.search(query=query, source_policy=source_policy, limit=limit)
    if search_cache is not None and cache_key not in search_cache:
        search_cache[cache_key] = assets
    candidates = []
    for index, asset in enumerate(assets):
        if not _asset_hub_is_video_asset(asset):
            continue
        candidate = _asset_hub_candidate(asset, term=query, rank=index)
        if not _is_orientation_compatible(candidate, video_aspect):
            continue
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
    request_attempts: int | None = None,
    search_cache: dict[tuple[str, str, int], list[dict[str, Any]]] | None = None,
    queries_are_visual: bool = False,
) -> tuple[list[MaterialCandidate], list[DiscoveryDiagnostic]]:
    batches: list[list[MaterialCandidate]] = []
    diagnostics: list[DiscoveryDiagnostic] = []
    for query in ((term,) if queries_are_visual else _asset_hub_search_queries(term)):
        found = _asset_hub_found_candidates(
            provider,
            query=query,
            source_policy=source_policy,
            original_term=term,
            video_aspect=video_aspect,
            limit=limit,
            request_attempts=request_attempts,
            search_cache=search_cache,
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
    request_attempts: int | None = None,
    search_cache: dict[tuple[str, str, int], list[dict[str, Any]]] | None = None,
    queries_are_visual: bool = False,
) -> tuple[list[MaterialCandidate], list[DiscoveryDiagnostic]]:
    title_found, diagnostics = _asset_hub_multi_query_candidates(
        provider,
        term=term,
        source_policy=title_policy,
        video_aspect=video_aspect,
        clip_duration=clip_duration,
        limit=limit,
        request_attempts=request_attempts,
        search_cache=search_cache,
        queries_are_visual=queries_are_visual,
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
        request_attempts=request_attempts,
        search_cache=search_cache,
        queries_are_visual=queries_are_visual,
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
                provider, None, "success" if local_count else "empty",
                "local production picker searched" if local_count else "local production picker found no usable visual",
                local_count, local_count, local_count, local_count))
            if local_count:
                succeeded.append(provider)
            continue
        terms = normalized_hub_terms if provider == PROVIDER_ASSET_HUB else normalized_stock_terms
        if provider != PROVIDER_ASSET_HUB and not native_stock_provider_configured(provider):
            config_key = native_stock_config_key(provider) or "native provider configuration"
            diagnostics.append(DiscoveryDiagnostic(
                provider, None, "config_missing",
                f"MPT native configuration missing: {config_key}",
                0, 0, 0, 0,
            ))
            continue
        if provider == PROVIDER_ASSET_HUB:
            active_provider = asset_hub_provider or KurukinAssetProvider()
            source_policy = plan["asset_hub"]["source_policy"]
        for term in terms:
            found: list[MaterialCandidate] = []
            try:
                if provider != PROVIDER_ASSET_HUB:
                    remote_attempts += 1
                    items = material.search_videos_for_provider(provider, term, minimum_duration, video_aspect)
                    raw_count = len(items)
                    normalized = [candidate for index, item in enumerate(items) if (candidate := _stock_candidate(item, provider=provider, term=term, rank=index)) is not None]
                    found = [
                        candidate
                        for candidate in normalized
                        if _is_orientation_compatible(candidate, video_aspect)
                    ]
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
                if provider == PROVIDER_ASSET_HUB and isinstance(exc, KurukinAssetHubUnavailableError):
                    # Availability is not a match.  Discard any prior result
                    # from this provider for this job and let other providers
                    # determine whether review coverage is sufficient.
                    candidates = [item for item in candidates if item.provider != PROVIDER_ASSET_HUB]
                    diagnostics.append(DiscoveryDiagnostic(
                        provider, term, "unavailable", _safe_error_message(exc), 0,
                    ))
                    break
                technical_failures += 1
                diagnostics.append(DiscoveryDiagnostic(provider, term, "error", _safe_error_message(exc), None))
                if remote_provider_count == 1:
                    raise MaterialDiscoveryError(f"material provider '{provider}' failed") from exc
                continue
            if provider != PROVIDER_ASSET_HUB:
                diagnostics.append(DiscoveryDiagnostic(
                    provider, term, "success" if found else "empty", "ok" if found else "no usable candidates",
                    len(found), raw_count, len(normalized), len(found)))
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
    request_attempts: int = 1,
    queries_are_visual: bool = False,
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
    # Neighbouring V2 scene queries can overlap.  Reuse exact responses only
    # within this job; no cross-job cache or new service is required.
    search_cache: dict[tuple[str, str, int], list[dict[str, Any]]] = {}

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
                    request_attempts=request_attempts,
                    search_cache=search_cache,
                    queries_are_visual=queries_are_visual,
                )
            else:
                found, term_diagnostics = _asset_hub_multi_query_candidates(
                    provider,
                    term=term,
                    source_policy=source_policy,
                    video_aspect=video_aspect,
                    clip_duration=limit_per_term,
                    limit=limit_per_term,
                    request_attempts=request_attempts,
                    search_cache=search_cache,
                    queries_are_visual=queries_are_visual,
                )
        except Exception as exc:
            # Per-job circuit breaker: a failed provider must not be called
            # again for every remaining scene/query in this execution.
            if isinstance(exc, KurukinAssetHubUnavailableError):
                return MaterialDiscoveryResult(
                    (),
                    tuple(diagnostics) + (DiscoveryDiagnostic(
                        PROVIDER_ASSET_HUB,
                        term,
                        "unavailable",
                        _safe_error_message(exc),
                        0,
                    ),),
                    (PROVIDER_ASSET_HUB,),
                    (),
                    {"stock": (), "asset_hub": normalized_terms},
                )
            if _is_fatal_asset_hub_error(exc):
                raise

            diagnostics.append(
                DiscoveryDiagnostic(
                    PROVIDER_ASSET_HUB,
                    term,
                    "error",
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
        if isinstance(exc, KurukinAssetHubUnavailableError):
            return MaterialDiscoveryResult(
                (),
                (DiscoveryDiagnostic(
                    PROVIDER_ASSET_HUB, title, "unavailable", _safe_error_message(exc), 0,
                ),),
                (PROVIDER_ASSET_HUB,),
                (),
                {"stock": (), "asset_hub": (title,)},
            )
        raise MaterialDiscoveryError("material provider 'asset_hub' failed") from exc

    candidates = []
    for index, asset in enumerate(assets):
        candidate = _title_only_asset_hub_candidate(asset, title=title, rank=index)
        if _is_orientation_compatible(candidate, video_aspect):
            candidates.append(candidate)
    candidates = _rank_asset_hub_candidates(
        candidates,
        video_aspect=video_aspect,
        clip_duration=limit,
    )
    diagnostics = (DiscoveryDiagnostic(PROVIDER_ASSET_HUB, title, "success", "global_title_only_fallback", len(candidates)),)
    succeeded = (PROVIDER_ASSET_HUB,) if candidates else ()
    return MaterialDiscoveryResult(_dedupe(candidates), diagnostics, (PROVIDER_ASSET_HUB,), succeeded, {"stock": (), "asset_hub": (title,)})
