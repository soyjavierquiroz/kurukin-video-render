import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.custom.asset_source_policy import (
    ASSET_SOURCE_ASSET_HUB,
    ASSET_SOURCE_LOCAL_LIBRARY,
    ASSET_SOURCE_MANIFEST,
    ASSET_SOURCE_MODE_EXCLUSIVE_BRAND_ASSETS,
    ASSET_SOURCE_MODE_LOCAL_ONLY,
    ASSET_SOURCE_MODE_OPEN_SOURCES,
    ASSET_SOURCE_PEXELS,
    ASSET_SOURCE_UPLOADED,
    is_source_allowed,
    normalize_asset_source_policy,
    requires_exclusive_brand_assets,
    summarize_asset_source_policy,
    validate_asset_source_policy,
)


class TestAssetSourcePolicy(unittest.TestCase):
    def test_default_none_normalizes_to_open_sources(self):
        policy = normalize_asset_source_policy(None)

        self.assertEqual(policy["mode"], ASSET_SOURCE_MODE_OPEN_SOURCES)
        self.assertEqual(
            policy["allowed_sources"],
            [
                ASSET_SOURCE_ASSET_HUB,
                ASSET_SOURCE_PEXELS,
                ASSET_SOURCE_LOCAL_LIBRARY,
                ASSET_SOURCE_UPLOADED,
            ],
        )
        self.assertIsNone(policy["exclusive_source"])
        self.assertIsNone(policy["brand_asset_bundle_uid"])
        self.assertFalse(policy["require_manifest"])

    def test_open_sources_allows_available_default_sources(self):
        policy = normalize_asset_source_policy(None)

        for source in (
            ASSET_SOURCE_ASSET_HUB,
            ASSET_SOURCE_PEXELS,
            ASSET_SOURCE_LOCAL_LIBRARY,
            ASSET_SOURCE_UPLOADED,
        ):
            with self.subTest(source=source):
                self.assertTrue(is_source_allowed(policy, source))

    def test_open_sources_does_not_require_manifest(self):
        policy = normalize_asset_source_policy(None)

        self.assertFalse(policy["require_manifest"])
        self.assertEqual(validate_asset_source_policy(policy), [])

    def test_exclusive_brand_assets_requires_bundle_uid(self):
        errors = validate_asset_source_policy(
            {"mode": ASSET_SOURCE_MODE_EXCLUSIVE_BRAND_ASSETS}
        )

        self.assertIn(
            "asset_policy.brand_asset_bundle_uid is required for exclusive_brand_assets",
            errors,
        )

    def test_exclusive_brand_assets_blocks_open_sources(self):
        policy = normalize_asset_source_policy(
            {
                "mode": ASSET_SOURCE_MODE_EXCLUSIVE_BRAND_ASSETS,
                "brand_asset_bundle_uid": "jab_test",
            }
        )

        self.assertTrue(requires_exclusive_brand_assets(policy))
        self.assertFalse(is_source_allowed(policy, ASSET_SOURCE_PEXELS))
        self.assertFalse(is_source_allowed(policy, ASSET_SOURCE_LOCAL_LIBRARY))
        self.assertFalse(is_source_allowed(policy, ASSET_SOURCE_UPLOADED))

    def test_exclusive_brand_assets_allows_asset_hub(self):
        policy = normalize_asset_source_policy(
            {
                "mode": ASSET_SOURCE_MODE_EXCLUSIVE_BRAND_ASSETS,
                "brand_asset_bundle_uid": "jab_test",
            }
        )

        self.assertEqual(policy["allowed_sources"], [ASSET_SOURCE_ASSET_HUB])
        self.assertEqual(policy["exclusive_source"], ASSET_SOURCE_ASSET_HUB)
        self.assertTrue(policy["require_manifest"])
        self.assertTrue(is_source_allowed(policy, ASSET_SOURCE_ASSET_HUB))

    def test_exclusive_brand_assets_can_target_manifest(self):
        policy = normalize_asset_source_policy(
            {
                "mode": ASSET_SOURCE_MODE_EXCLUSIVE_BRAND_ASSETS,
                "exclusive_source": ASSET_SOURCE_MANIFEST,
                "brand_asset_bundle_uid": "jab_test",
            }
        )

        self.assertEqual(policy["allowed_sources"], [ASSET_SOURCE_MANIFEST])
        self.assertTrue(is_source_allowed(policy, ASSET_SOURCE_MANIFEST))
        self.assertFalse(is_source_allowed(policy, ASSET_SOURCE_ASSET_HUB))

    def test_local_only_allows_local_and_uploaded_assets(self):
        policy = normalize_asset_source_policy({"mode": ASSET_SOURCE_MODE_LOCAL_ONLY})

        self.assertTrue(is_source_allowed(policy, ASSET_SOURCE_LOCAL_LIBRARY))
        self.assertTrue(is_source_allowed(policy, ASSET_SOURCE_UPLOADED))

    def test_local_only_blocks_external_sources(self):
        policy = normalize_asset_source_policy({"mode": ASSET_SOURCE_MODE_LOCAL_ONLY})

        self.assertFalse(is_source_allowed(policy, ASSET_SOURCE_PEXELS))
        self.assertFalse(is_source_allowed(policy, ASSET_SOURCE_ASSET_HUB))

    def test_unknown_source_is_rejected(self):
        policy = normalize_asset_source_policy(
            {"allowed_sources": [ASSET_SOURCE_PEXELS, "unknown_source"]}
        )

        self.assertIn(
            "asset_policy.allowed_sources has unknown source: unknown_source",
            validate_asset_source_policy(policy),
        )
        self.assertFalse(is_source_allowed(policy, "unknown_source"))

    def test_policy_summary_returns_human_labels(self):
        self.assertEqual(
            summarize_asset_source_policy(None)["console_label"],
            "Asset policy: Open sources",
        )
        self.assertEqual(
            summarize_asset_source_policy(
                {
                    "mode": ASSET_SOURCE_MODE_EXCLUSIVE_BRAND_ASSETS,
                    "brand_asset_bundle_uid": "jab_test",
                }
            )["short_label"],
            "Fuentes: marca exclusiva",
        )
        self.assertEqual(
            summarize_asset_source_policy({"mode": ASSET_SOURCE_MODE_LOCAL_ONLY})[
                "short_label"
            ],
            "Fuentes: locales",
        )


if __name__ == "__main__":
    unittest.main()
