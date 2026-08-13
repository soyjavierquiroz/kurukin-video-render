"""Multi-provider material discovery without downloading or selecting assets."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

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
from app.services import material


_FORBIDDEN_SOURCE_KEY_PARTS = (
    "drive_file_id", "remote_path", "rclone_remote", "target_path",
    "credential", "api_key", "apikey", "token", "password", "secret",
    "authorization", "auth",
)


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
        key: source[key] for key in ("filename", "orientation", "duration", "width", "height", "scope", "brand", "title")
        if key in source
    }
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
            diagnostics.append(DiscoveryDiagnostic(provider, None, "pending_adapter", "local semantic discovery adapter is not available", None))
            continue
        terms = normalized_hub_terms if provider == PROVIDER_ASSET_HUB else normalized_stock_terms
        if provider == PROVIDER_ASSET_HUB:
            active_provider = asset_hub_provider or KurukinAssetProvider()
            source_policy = plan["asset_hub"]["source_policy"]
        for term in terms:
            remote_attempts += 1
            try:
                if provider == PROVIDER_ASSET_HUB:
                    assets = active_provider.search(query=term, source_policy=source_policy)
                    found = [_asset_hub_candidate(asset, term=term, rank=index) for index, asset in enumerate(assets)]
                else:
                    items = material.search_videos_for_provider(provider, term, minimum_duration, video_aspect)
                    found = [candidate for index, item in enumerate(items) if (candidate := _stock_candidate(item, provider=provider, term=term, rank=index)) is not None]
            except Exception as exc:
                if provider == PROVIDER_ASSET_HUB and _is_fatal_asset_hub_error(exc):
                    raise
                technical_failures += 1
                diagnostics.append(DiscoveryDiagnostic(provider, term, "failure", _safe_error_message(exc), None))
                if remote_provider_count == 1:
                    raise MaterialDiscoveryError(f"material provider '{provider}' failed") from exc
                continue
            candidates.extend(found)
            if provider not in succeeded:
                succeeded.append(provider)
            diagnostics.append(DiscoveryDiagnostic(provider, term, "success", "ok", len(found)))

    if remote_attempts and technical_failures == remote_attempts:
        raise MaterialDiscoveryError("all enabled remote material providers failed")
    return MaterialDiscoveryResult(_dedupe(candidates), tuple(diagnostics), tuple(attempted), tuple(succeeded), terms_used)
