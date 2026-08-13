import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.custom.material_source_policy import (
    PROVIDER_ASSET_HUB, PROVIDER_COVERR, PROVIDER_LOCAL, PROVIDER_PEXELS,
    PROVIDER_PIXABAY, AssetHubCatalogPolicy, AssetHubExcludePolicy,
    AssetHubIncludePolicy, CatalogExpansionRequired, MaterialProviderPolicy,
    MaterialSourcePolicy, asset_hub_only_policy, build_asset_hub_source_policy,
    build_discovery_plan, external_only_policy, local_only_policy, open_sources_policy,
)


def asset_hub_policy(**include):
    return MaterialSourcePolicy(
        MaterialProviderPolicy((PROVIDER_ASSET_HUB,)),
        AssetHubCatalogPolicy(include=AssetHubIncludePolicy(**include)),
    )


class TestMaterialSourcePolicy(unittest.TestCase):
    def test_open_sources(self):
        policy = open_sources_policy()
        self.assertEqual(policy.providers.enabled, (PROVIDER_ASSET_HUB, PROVIDER_PEXELS, PROVIDER_PIXABAY, PROVIDER_COVERR, PROVIDER_LOCAL))
        self.assertEqual(build_asset_hub_source_policy(policy), {"sources": [{"scope": "generic"}]})

    def test_asset_hub_generic_only(self):
        self.assertEqual(build_discovery_plan(asset_hub_only_policy())["external_providers"], [])

    def test_brand_only_and_brand_stock(self):
        hub = asset_hub_policy(brands=("Mi Marca",))
        self.assertEqual(build_asset_hub_source_policy(hub), {"sources": [{"scope": "brand", "brand": "mi marca"}]})
        stock = MaterialSourcePolicy(MaterialProviderPolicy((PROVIDER_ASSET_HUB, PROVIDER_PEXELS)), hub.asset_hub)
        self.assertEqual(build_discovery_plan(stock)["external_providers"], [PROVIDER_PEXELS])

    def test_title_only_title_stock_and_multiple_titles(self):
        hub = asset_hub_policy(titles=("Mi Otra Yo", "Otra"))
        self.assertEqual(build_asset_hub_source_policy(hub)["sources"], [{"scope": "title", "title": "mi otra yo"}, {"scope": "title", "title": "otra"}])
        stock = MaterialSourcePolicy(MaterialProviderPolicy((PROVIDER_ASSET_HUB, PROVIDER_PEXELS)), hub.asset_hub)
        self.assertEqual(build_discovery_plan(stock)["external_providers"], [PROVIDER_PEXELS])

    def test_generic_titles_and_brand_titles(self):
        self.assertEqual(len(build_asset_hub_source_policy(asset_hub_policy(generic=True, titles=("t",)))["sources"]), 2)
        self.assertEqual(len(build_asset_hub_source_policy(asset_hub_policy(brands=("b",), titles=("t",)))["sources"]), 2)

    def test_external_only_and_local_only(self):
        self.assertFalse(build_discovery_plan(external_only_policy())["asset_hub"]["enabled"])
        self.assertTrue(build_discovery_plan(local_only_policy())["use_local"])

    def test_asset_hub_disabled_with_scopes_and_no_providers_error(self):
        with self.assertRaises(ValueError):
            MaterialProviderPolicy(())
        with self.assertRaises(ValueError):
            MaterialSourcePolicy(MaterialProviderPolicy((PROVIDER_PEXELS,)), AssetHubCatalogPolicy(include=AssetHubIncludePolicy(generic=True)))

    def test_include_exclude_collision_and_slug_normalization(self):
        with self.assertRaises(ValueError):
            AssetHubCatalogPolicy(AssetHubIncludePolicy(titles=(" Mi Otra Yo ",)), AssetHubExcludePolicy(titles=("mi otra yo",)))
        include = AssetHubIncludePolicy(brands=(" Brand ", "brand", "", "OTHER"), titles=(" A ", "a", ""))
        self.assertEqual(include.brands, ("brand", "other"))
        self.assertEqual(include.titles, ("a",))

    def test_all_scopes_require_catalog_expansion_without_wildcards(self):
        configurations = (
            (AssetHubIncludePolicy(all_titles=True), AssetHubExcludePolicy(titles=("mi-otra-yo",))),
            (AssetHubIncludePolicy(all_titles=True, titles=("ignored",)), AssetHubExcludePolicy()),
            (AssetHubIncludePolicy(all_brands=True), AssetHubExcludePolicy(brands=("brand",))),
        )
        for include, exclude in configurations:
            policy = MaterialSourcePolicy(MaterialProviderPolicy((PROVIDER_ASSET_HUB,)), AssetHubCatalogPolicy(include=include, exclude=exclude))
            plan = build_discovery_plan(policy)
            self.assertTrue(plan["asset_hub"]["requires_catalog_expansion"])
            self.assertIsNone(plan["asset_hub"]["source_policy"])
            with self.assertRaises(CatalogExpansionRequired):
                build_asset_hub_source_policy(policy)
            self.assertNotIn("*", str(policy.to_dict()))

    def test_useless_asset_hub_states_are_rejected(self):
        with self.assertRaises(ValueError):
            MaterialSourcePolicy(MaterialProviderPolicy((PROVIDER_ASSET_HUB,)))
        with self.assertRaises(ValueError):
            AssetHubCatalogPolicy(exclude=AssetHubExcludePolicy(titles=("mi-otra-yo",)))

    def test_deterministic_provider_order_plan_and_safe_serialization(self):
        policy = MaterialSourcePolicy(MaterialProviderPolicy((PROVIDER_LOCAL, PROVIDER_COVERR, PROVIDER_PEXELS, PROVIDER_PIXABAY)))
        plan = build_discovery_plan(policy)
        self.assertEqual(plan["external_providers"], [PROVIDER_PEXELS, PROVIDER_PIXABAY, PROVIDER_COVERR])
        serialized = str(policy.to_dict())
        for forbidden in ("drive_file_id", "remote_path", "rclone_remote", "credentials", "target_path"):
            self.assertNotIn(forbidden, serialized)


if __name__ == "__main__":
    unittest.main()
