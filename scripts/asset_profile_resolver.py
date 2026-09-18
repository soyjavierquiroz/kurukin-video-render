#!/usr/bin/env python3
"""Resolve allowed asset profiles to the existing material-source policy."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.custom.material_source_policy import (
    AssetHubCatalogPolicy,
    AssetHubIncludePolicy,
    MaterialProviderPolicy,
    MaterialSourcePolicy,
    PROVIDER_ATLAS,
    PROVIDER_ASSET_HUB,
    build_asset_hub_source_policy,
    open_sources_policy,
)
from app.custom.material_provider_availability import native_stock_provider_configured

try:  # Supports both ``python scripts/...`` and package imports in tests.
    from scripts.niche_registry import DEFAULT_REGISTRY_PATH, NicheRegistryError, load_niche
except ModuleNotFoundError:  # pragma: no cover - exercised by the direct CLI
    from niche_registry import DEFAULT_REGISTRY_PATH, NicheRegistryError, load_niche


class AssetProfileError(ValueError):
    """Raised when an asset profile cannot be safely resolved."""


class AssetProfileNotReadyError(AssetProfileError):
    """Raised for an allowed profile whose required source data is unverified."""


# This is intentionally declarative: adding a verified profile is data work,
# not a title-specific change to discovery or provider code.
ASSET_PROFILE_CONFIG: dict[str, dict[str, Any]] = {
    "MI_OTRA_YO": {
        "status": "ready",
        "providers": (PROVIDER_ASSET_HUB,),
        "asset_hub_titles": ("mi-otra-yo",),
    },
    "GENERALES": {
        "status": "ready",
        "use_current_generic_routing": True,
    },
    "ROMPIENDO_CIRCULO": {
        "status": "ready",
        "providers": (PROVIDER_ATLAS,),
        # This is Atlas namespace identity, not an Asset Hub title scope and
        # never a signal to enable Atlas by itself.
        "atlas_title_id": "romper-el-circulo",
    },
    "CF_MIX": {
        "status": "not_ready",
        "reason": "ROMPIENDO_CIRCULO dependency is not configured",
    },
}


def asset_profile_metadata(profile_id: str) -> dict[str, Any]:
    """Return declarative profile metadata after policy resolution authorized it.

    Callers must resolve the profile through ``resolve_asset_profile`` first.
    Keeping identity metadata separate from the policy prevents a title ID from
    becoming an implicit provider opt-in.
    """
    profile = ASSET_PROFILE_CONFIG.get(profile_id)
    if profile is None:
        raise AssetProfileError(f"asset profile configuration not found: {profile_id}")
    return {
        key: value
        for key, value in profile.items()
        if key not in {"providers", "asset_hub_titles", "use_current_generic_routing"}
    }


def asset_profile_atlas_title_id(profile_id: str) -> str | None:
    """Return the explicit Atlas identity configured for a resolved profile."""
    value = asset_profile_metadata(profile_id).get("atlas_title_id")
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def resolve_asset_profile(
    niche_id: str,
    profile_id: str,
    registry_path: Path = DEFAULT_REGISTRY_PATH,
) -> MaterialSourcePolicy:
    """Return a native policy only for a profile allowed by the niche registry."""
    try:
        niche = load_niche(niche_id, registry_path)
    except NicheRegistryError as exc:
        raise AssetProfileError(str(exc)) from exc

    if profile_id not in niche["allowed_asset_profiles"]:
        raise AssetProfileError(
            f"asset profile '{profile_id}' is not allowed by niche '{niche_id}'"
        )

    profile = ASSET_PROFILE_CONFIG.get(profile_id)
    if profile is None:
        raise AssetProfileError(f"asset profile configuration not found: {profile_id}")
    if profile["status"] != "ready":
        raise AssetProfileNotReadyError(
            f"ASSET PROFILE NOT READY: {profile_id}\nreason={profile['reason']}"
        )

    if profile.get("use_current_generic_routing"):
        # GENERALES retains native MPT support, but must not advertise a stock
        # provider as enabled until the exact key list its native function
        # consumes is configured in this runtime.
        open_policy = open_sources_policy()
        enabled = tuple(
            provider
            for provider in open_policy.providers.enabled
            if provider not in {"pexels", "pixabay", "coverr"}
            or native_stock_provider_configured(provider)
        )
        return MaterialSourcePolicy(
            providers=MaterialProviderPolicy(enabled),
            asset_hub=open_policy.asset_hub,
        )

    providers = MaterialProviderPolicy(profile["providers"])
    asset_hub = AssetHubCatalogPolicy()
    if providers.is_enabled(PROVIDER_ASSET_HUB):
        asset_hub = AssetHubCatalogPolicy(
            include=AssetHubIncludePolicy(titles=profile["asset_hub_titles"])
        )
    return MaterialSourcePolicy(providers=providers, asset_hub=asset_hub)


def _source_labels(policy: MaterialSourcePolicy) -> str:
    if not policy.providers.is_enabled(PROVIDER_ASSET_HUB):
        return "NONE"
    sources = build_asset_hub_source_policy(policy)["sources"]
    return ",".join(
        f"{source['scope']}:{source[source['scope']]}"
        if source["scope"] != "generic"
        else "generic"
        for source in sources
    ) or "NONE"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate an allowed asset profile safely.")
    parser.add_argument("--niche", required=True, help="Niche ID")
    parser.add_argument("--profile", required=True, help="Asset profile ID")
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY_PATH)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        policy = resolve_asset_profile(args.niche, args.profile, args.registry)
    except AssetProfileNotReadyError as exc:
        print(exc, file=sys.stderr)
        return 2
    except AssetProfileError as exc:
        print(f"ASSET PROFILE ERROR: {exc}", file=sys.stderr)
        return 1

    print("ASSET PROFILE OK")
    print(f"niche_id={args.niche}")
    print(f"profile_id={args.profile}")
    print("providers=" + ",".join(policy.providers.enabled))
    print(f"asset_hub_sources={_source_labels(policy)}")
    print(f"generic_fallback={'YES' if policy.asset_hub.include.generic else 'NO'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
