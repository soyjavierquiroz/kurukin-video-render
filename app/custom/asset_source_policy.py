"""Pure asset source policy helpers for Kurukin A-roll/B-roll jobs."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


ASSET_SOURCE_MODE_OPEN_SOURCES = "open_sources"
ASSET_SOURCE_MODE_EXCLUSIVE_BRAND_ASSETS = "exclusive_brand_assets"
ASSET_SOURCE_MODE_LOCAL_ONLY = "local_only"

ASSET_SOURCE_ASSET_HUB = "asset_hub"
ASSET_SOURCE_PEXELS = "pexels"
ASSET_SOURCE_LOCAL_LIBRARY = "local_library"
ASSET_SOURCE_UPLOADED = "uploaded"
ASSET_SOURCE_MANIFEST = "manifest"

ALLOWED_ASSET_SOURCE_MODES = {
    ASSET_SOURCE_MODE_OPEN_SOURCES,
    ASSET_SOURCE_MODE_EXCLUSIVE_BRAND_ASSETS,
    ASSET_SOURCE_MODE_LOCAL_ONLY,
}
ALLOWED_ASSET_SOURCE_IDS = {
    ASSET_SOURCE_ASSET_HUB,
    ASSET_SOURCE_PEXELS,
    ASSET_SOURCE_LOCAL_LIBRARY,
    ASSET_SOURCE_UPLOADED,
    ASSET_SOURCE_MANIFEST,
}
OPEN_SOURCES_DEFAULT_ALLOWED = [
    ASSET_SOURCE_ASSET_HUB,
    ASSET_SOURCE_PEXELS,
    ASSET_SOURCE_LOCAL_LIBRARY,
    ASSET_SOURCE_UPLOADED,
]
LOCAL_ONLY_ALLOWED = [
    ASSET_SOURCE_LOCAL_LIBRARY,
    ASSET_SOURCE_UPLOADED,
]
EXCLUSIVE_BRAND_ALLOWED = [
    ASSET_SOURCE_ASSET_HUB,
]
EXCLUSIVE_BRAND_SOURCE_IDS = {
    ASSET_SOURCE_ASSET_HUB,
    ASSET_SOURCE_MANIFEST,
}


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _clean_allowed_sources(value: Any, fallback: list[str]) -> list[str]:
    if isinstance(value, str):
        raw_items = [value]
    elif isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        raw_items = fallback

    normalized: list[str] = []
    for item in raw_items:
        source = _clean_text(item)
        if source and source not in normalized:
            normalized.append(source)
    return normalized or list(fallback)


def normalize_asset_source_policy(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Return a normalized asset source policy without external side effects."""

    source = deepcopy(raw) if isinstance(raw, dict) else {}
    mode = _clean_text(source.get("mode")) or ASSET_SOURCE_MODE_OPEN_SOURCES

    if mode == ASSET_SOURCE_MODE_EXCLUSIVE_BRAND_ASSETS:
        exclusive_source = _clean_text(source.get("exclusive_source"))
        if exclusive_source not in EXCLUSIVE_BRAND_SOURCE_IDS:
            allowed_sources = _clean_allowed_sources(
                source.get("allowed_sources"),
                EXCLUSIVE_BRAND_ALLOWED,
            )
            if len(allowed_sources) == 1 and allowed_sources[0] in EXCLUSIVE_BRAND_SOURCE_IDS:
                exclusive_source = allowed_sources[0]
            else:
                exclusive_source = ASSET_SOURCE_ASSET_HUB
        allowed_sources = [exclusive_source]
        return {
            "mode": mode,
            "allowed_sources": allowed_sources,
            "exclusive_source": exclusive_source,
            "brand_asset_bundle_uid": _clean_text(
                source.get("brand_asset_bundle_uid")
            ),
            "require_manifest": True,
        }

    if mode == ASSET_SOURCE_MODE_LOCAL_ONLY:
        return {
            "mode": mode,
            "allowed_sources": list(LOCAL_ONLY_ALLOWED),
            "exclusive_source": None,
            "brand_asset_bundle_uid": None,
            "require_manifest": False,
        }

    allowed_sources = _clean_allowed_sources(
        source.get("allowed_sources"),
        OPEN_SOURCES_DEFAULT_ALLOWED,
    )
    return {
        "mode": mode,
        "allowed_sources": allowed_sources,
        "exclusive_source": None,
        "brand_asset_bundle_uid": _clean_text(
            source.get("brand_asset_bundle_uid")
        )
        or None,
        "require_manifest": bool(source.get("require_manifest", False)),
    }


def validate_asset_source_policy(policy: dict[str, Any]) -> list[str]:
    """Return validation errors for a normalized or raw asset source policy."""

    normalized = normalize_asset_source_policy(policy)
    errors: list[str] = []
    mode = normalized.get("mode")
    allowed_sources = normalized.get("allowed_sources") or []

    if mode not in ALLOWED_ASSET_SOURCE_MODES:
        errors.append("asset_policy.mode is not supported")

    for source in allowed_sources:
        if source not in ALLOWED_ASSET_SOURCE_IDS:
            errors.append(f"asset_policy.allowed_sources has unknown source: {source}")

    if mode == ASSET_SOURCE_MODE_OPEN_SOURCES:
        if normalized.get("require_manifest"):
            errors.append("asset_policy.require_manifest must be false for open_sources")
    elif mode == ASSET_SOURCE_MODE_EXCLUSIVE_BRAND_ASSETS:
        bundle_uid = _clean_text(normalized.get("brand_asset_bundle_uid"))
        exclusive_source = normalized.get("exclusive_source")
        if not bundle_uid:
            errors.append(
                "asset_policy.brand_asset_bundle_uid is required for exclusive_brand_assets"
            )
        if not normalized.get("require_manifest"):
            errors.append(
                "asset_policy.require_manifest must be true for exclusive_brand_assets"
            )
        if exclusive_source not in EXCLUSIVE_BRAND_SOURCE_IDS:
            errors.append(
                "asset_policy.exclusive_source must be asset_hub or manifest"
            )
        blocked = [
            source
            for source in allowed_sources
            if source not in EXCLUSIVE_BRAND_SOURCE_IDS
        ]
        if blocked:
            errors.append(
                "asset_policy.exclusive_brand_assets cannot allow "
                + ", ".join(blocked)
            )
    elif mode == ASSET_SOURCE_MODE_LOCAL_ONLY:
        blocked = [
            source
            for source in allowed_sources
            if source not in LOCAL_ONLY_ALLOWED
        ]
        if blocked:
            errors.append(
                "asset_policy.local_only cannot allow " + ", ".join(blocked)
            )
        if normalized.get("require_manifest"):
            errors.append("asset_policy.require_manifest must be false for local_only")

    return errors


def is_source_allowed(policy: dict[str, Any], source: str) -> bool:
    """Return whether a normalized/raw policy allows one source id."""

    clean_source = _clean_text(source)
    if clean_source not in ALLOWED_ASSET_SOURCE_IDS:
        return False
    normalized = normalize_asset_source_policy(policy)
    if validate_asset_source_policy(normalized):
        return False
    return clean_source in (normalized.get("allowed_sources") or [])


def requires_exclusive_brand_assets(policy: dict[str, Any]) -> bool:
    """Return true when the policy requires exclusive brand assets."""

    return (
        normalize_asset_source_policy(policy).get("mode")
        == ASSET_SOURCE_MODE_EXCLUSIVE_BRAND_ASSETS
    )


def summarize_asset_source_policy(policy: dict[str, Any]) -> dict[str, Any]:
    """Return stable human labels for UI and queue summaries."""

    normalized = normalize_asset_source_policy(policy)
    mode = normalized.get("mode")
    labels = {
        ASSET_SOURCE_MODE_OPEN_SOURCES: ("Open sources", "Fuentes: abiertas"),
        ASSET_SOURCE_MODE_EXCLUSIVE_BRAND_ASSETS: (
            "Exclusive brand assets",
            "Fuentes: marca exclusiva",
        ),
        ASSET_SOURCE_MODE_LOCAL_ONLY: ("Local only", "Fuentes: locales"),
    }
    label, short_label = labels.get(mode, ("Unknown", "Fuentes: sin definir"))
    return {
        "mode": mode,
        "label": label,
        "console_label": f"Asset policy: {label}",
        "short_label": short_label,
        "allowed_sources": list(normalized.get("allowed_sources") or []),
        "exclusive_source": normalized.get("exclusive_source"),
        "brand_asset_bundle_uid": normalized.get("brand_asset_bundle_uid"),
        "require_manifest": bool(normalized.get("require_manifest")),
        "exclusive": mode == ASSET_SOURCE_MODE_EXCLUSIVE_BRAND_ASSETS,
    }
