"""Declarative visual-material discovery policy for future Kurukin discovery.

This module deliberately models provider selection separately from Asset Hub
scopes.  It is pure: it neither searches nor materializes assets.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable


PROVIDER_ASSET_HUB = "asset_hub"
PROVIDER_PEXELS = "pexels"
PROVIDER_PIXABAY = "pixabay"
PROVIDER_COVERR = "coverr"
PROVIDER_LOCAL = "local"

# This is also the canonical discovery order, independent of input ordering.
PROVIDER_ORDER = (
    PROVIDER_ASSET_HUB,
    PROVIDER_PEXELS,
    PROVIDER_PIXABAY,
    PROVIDER_COVERR,
    PROVIDER_LOCAL,
)
EXTERNAL_PROVIDER_ORDER = (
    PROVIDER_PEXELS,
    PROVIDER_PIXABAY,
    PROVIDER_COVERR,
)


class CatalogExpansionRequired(RuntimeError):
    """Raised when broad Asset Hub scopes need a catalog unavailable to this client."""


def _normalize_slugs(values: Iterable[Any] | Any) -> tuple[str, ...]:
    if values is None:
        items: Iterable[Any] = ()
    elif isinstance(values, str):
        items = (values,)
    else:
        try:
            items = iter(values)
        except TypeError:
            items = (values,)

    normalized: list[str] = []
    for value in items:
        slug = str(value or "").strip().lower()
        if slug and slug not in normalized:
            normalized.append(slug)
    return tuple(normalized)


def _normalize_providers(values: Iterable[Any] | Any) -> tuple[str, ...]:
    requested = _normalize_slugs(values)
    unknown = [provider for provider in requested if provider not in PROVIDER_ORDER]
    if unknown:
        raise ValueError("unknown material providers: " + ", ".join(unknown))
    return tuple(provider for provider in PROVIDER_ORDER if provider in requested)


@dataclass(frozen=True)
class MaterialProviderPolicy:
    """Enabled providers, normalized to the stable provider order."""

    enabled: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        enabled = _normalize_providers(self.enabled)
        if not enabled:
            raise ValueError("at least one material provider must be enabled")
        object.__setattr__(self, "enabled", enabled)

    def is_enabled(self, provider: str) -> bool:
        return provider in self.enabled


@dataclass(frozen=True)
class AssetHubIncludePolicy:
    generic: bool = False
    brands: tuple[str, ...] = field(default_factory=tuple)
    titles: tuple[str, ...] = field(default_factory=tuple)
    all_brands: bool = False
    all_titles: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "generic", bool(self.generic))
        object.__setattr__(self, "brands", _normalize_slugs(self.brands))
        # all_titles already includes every explicit title; retain no duplicate meaning.
        object.__setattr__(self, "titles", () if self.all_titles else _normalize_slugs(self.titles))
        object.__setattr__(self, "all_brands", bool(self.all_brands))
        object.__setattr__(self, "all_titles", bool(self.all_titles))


@dataclass(frozen=True)
class AssetHubExcludePolicy:
    brands: tuple[str, ...] = field(default_factory=tuple)
    titles: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "brands", _normalize_slugs(self.brands))
        object.__setattr__(self, "titles", _normalize_slugs(self.titles))


@dataclass(frozen=True)
class AssetHubCatalogPolicy:
    include: AssetHubIncludePolicy = field(default_factory=AssetHubIncludePolicy)
    exclude: AssetHubExcludePolicy = field(default_factory=AssetHubExcludePolicy)

    def __post_init__(self) -> None:
        if not isinstance(self.include, AssetHubIncludePolicy):
            object.__setattr__(self, "include", AssetHubIncludePolicy(**dict(self.include)))
        if not isinstance(self.exclude, AssetHubExcludePolicy):
            object.__setattr__(self, "exclude", AssetHubExcludePolicy(**dict(self.exclude)))
        overlap_brands = set(self.include.brands) & set(self.exclude.brands)
        overlap_titles = set(self.include.titles) & set(self.exclude.titles)
        if overlap_brands:
            raise ValueError("Asset Hub brand included and excluded: " + ", ".join(sorted(overlap_brands)))
        if overlap_titles:
            raise ValueError("Asset Hub title included and excluded: " + ", ".join(sorted(overlap_titles)))
        if self.exclude.brands and not self.include.all_brands:
            raise ValueError("Asset Hub brand exclusions require all_brands")
        if self.exclude.titles and not self.include.all_titles:
            raise ValueError("Asset Hub title exclusions require all_titles")

    @property
    def requires_catalog_expansion(self) -> bool:
        return self.include.all_brands or self.include.all_titles


@dataclass(frozen=True)
class MaterialSourcePolicy:
    providers: MaterialProviderPolicy
    asset_hub: AssetHubCatalogPolicy = field(default_factory=AssetHubCatalogPolicy)

    def __post_init__(self) -> None:
        if not isinstance(self.providers, MaterialProviderPolicy):
            object.__setattr__(self, "providers", MaterialProviderPolicy(self.providers))
        if not isinstance(self.asset_hub, AssetHubCatalogPolicy):
            object.__setattr__(self, "asset_hub", AssetHubCatalogPolicy(**dict(self.asset_hub)))
        include = self.asset_hub.include
        exclude = self.asset_hub.exclude
        has_asset_hub_scopes = any((
            include.generic, include.brands, include.titles, include.all_brands,
            include.all_titles, exclude.brands, exclude.titles,
        ))
        if not self.providers.is_enabled(PROVIDER_ASSET_HUB) and has_asset_hub_scopes:
            raise ValueError("Asset Hub scopes require the asset_hub provider")
        if self.providers.is_enabled(PROVIDER_ASSET_HUB) and not any((
            include.generic, include.brands, include.titles, include.all_brands,
            include.all_titles,
        )):
            raise ValueError("asset_hub requires at least one included scope")

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe declarative form; it contains no transport secrets."""
        return asdict(self)


def material_source_policy_from_dict(value: MaterialSourcePolicy | dict[str, Any]) -> MaterialSourcePolicy:
    """Parse the JSON-safe job form into the existing policy value object."""
    if isinstance(value, MaterialSourcePolicy):
        return value
    if not isinstance(value, dict):
        raise ValueError("material_source_policy must be an object")

    providers = value.get("providers")
    if isinstance(providers, dict):
        providers = providers.get("enabled")
    asset_hub = value.get("asset_hub", {})
    if not isinstance(asset_hub, dict):
        raise ValueError("material_source_policy.asset_hub must be an object")
    return MaterialSourcePolicy(
        providers=MaterialProviderPolicy(providers),
        asset_hub=AssetHubCatalogPolicy(**asset_hub),
    )


def build_asset_hub_source_policy(policy: MaterialSourcePolicy) -> dict[str, list[dict[str, str]]]:
    """Compile explicit scopes to the current Asset Hub search contract.

    The checked-in client has no catalog/listing operation, so broad scopes must
    first be expanded by a future catalog adapter.
    """
    if not policy.providers.is_enabled(PROVIDER_ASSET_HUB):
        raise ValueError("asset_hub provider is disabled")
    catalog = policy.asset_hub
    if catalog.requires_catalog_expansion:
        raise CatalogExpansionRequired(
            "Asset Hub all_titles/all_brands requires catalog expansion; "
            "the current client exposes no catalog API"
        )
    sources: list[dict[str, str]] = []
    if catalog.include.generic:
        sources.append({"scope": "generic"})
    sources.extend({"scope": "brand", "brand": brand} for brand in catalog.include.brands)
    sources.extend({"scope": "title", "title": title} for title in catalog.include.titles)
    return {"sources": sources}


def build_discovery_plan(policy: MaterialSourcePolicy) -> dict[str, Any]:
    """Build the deterministic, non-executing discovery contract."""
    asset_hub_enabled = policy.providers.is_enabled(PROVIDER_ASSET_HUB)
    expansion_required = asset_hub_enabled and policy.asset_hub.requires_catalog_expansion
    return {
        "external_providers": [
            provider for provider in EXTERNAL_PROVIDER_ORDER if policy.providers.is_enabled(provider)
        ],
        "use_local": policy.providers.is_enabled(PROVIDER_LOCAL),
        "asset_hub": {
            "enabled": asset_hub_enabled,
            "source_policy": (
                None if not asset_hub_enabled or expansion_required
                else build_asset_hub_source_policy(policy)
            ),
            "requires_catalog_expansion": expansion_required,
        },
    }


def open_sources_policy() -> MaterialSourcePolicy:
    return MaterialSourcePolicy(
        providers=MaterialProviderPolicy(PROVIDER_ORDER),
        asset_hub=AssetHubCatalogPolicy(include=AssetHubIncludePolicy(generic=True)),
    )


def asset_hub_only_policy() -> MaterialSourcePolicy:
    return MaterialSourcePolicy(
        providers=MaterialProviderPolicy((PROVIDER_ASSET_HUB,)),
        asset_hub=AssetHubCatalogPolicy(include=AssetHubIncludePolicy(generic=True)),
    )


def local_only_policy() -> MaterialSourcePolicy:
    return MaterialSourcePolicy(providers=MaterialProviderPolicy((PROVIDER_LOCAL,)))


def external_only_policy() -> MaterialSourcePolicy:
    return MaterialSourcePolicy(providers=MaterialProviderPolicy(EXTERNAL_PROVIDER_ORDER))
