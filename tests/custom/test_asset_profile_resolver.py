"""Focused contracts for declarative asset-profile resolution."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.custom.material_source_policy import (
    PROVIDER_ASSET_HUB,
    PROVIDER_COVERR,
    PROVIDER_LOCAL,
    PROVIDER_PEXELS,
    PROVIDER_PIXABAY,
    build_asset_hub_source_policy,
)
from scripts.asset_profile_resolver import (
    AssetProfileError,
    AssetProfileNotReadyError,
    resolve_asset_profile,
)


class AssetProfileResolverTests(unittest.TestCase):
    def registry(self, allowed_profiles: list[str]) -> Path:
        payload = {
            "version": 1,
            "niches": {
                "test-niche": {
                    "sheet_id": "sheet",
                    "rclone_remote": "remote",
                    "final_drive_folder_id": "folder",
                    "default_asset_profile": allowed_profiles[0],
                    "allowed_asset_profiles": allowed_profiles,
                }
            },
        }
        temporary = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        self.addCleanup(Path(temporary.name).unlink, missing_ok=True)
        with temporary:
            json.dump(payload, temporary)
        return Path(temporary.name)

    def test_mi_otra_yo_resolves_to_title_only_source(self):
        policy = resolve_asset_profile("test-niche", "MI_OTRA_YO", self.registry(["MI_OTRA_YO"]))
        self.assertEqual(policy.providers.enabled, (PROVIDER_ASSET_HUB,))
        self.assertEqual(
            build_asset_hub_source_policy(policy),
            {"sources": [{"scope": "title", "title": "mi-otra-yo"}]},
        )

    def test_mi_otra_yo_has_no_generic_fallback(self):
        policy = resolve_asset_profile("test-niche", "MI_OTRA_YO", self.registry(["MI_OTRA_YO"]))
        self.assertFalse(policy.asset_hub.include.generic)

    def test_generales_resolves_only_runtime_ready_stock_providers(self):
        with patch(
            "scripts.asset_profile_resolver.native_stock_provider_configured",
            return_value=False,
        ):
            policy = resolve_asset_profile(
                "test-niche", "GENERALES", self.registry(["GENERALES"])
            )
        self.assertEqual(
            policy.providers.enabled,
            (PROVIDER_ASSET_HUB, PROVIDER_LOCAL),
        )
        self.assertTrue(policy.asset_hub.include.generic)

    def test_rompiendo_circulo_is_not_ready(self):
        with self.assertRaisesRegex(AssetProfileNotReadyError, "ROMPIENDO_CIRCULO"):
            resolve_asset_profile("test-niche", "ROMPIENDO_CIRCULO", self.registry(["ROMPIENDO_CIRCULO"]))

    def test_cf_mix_is_not_ready(self):
        with self.assertRaisesRegex(AssetProfileNotReadyError, "ROMPIENDO_CIRCULO dependency"):
            resolve_asset_profile("test-niche", "CF_MIX", self.registry(["CF_MIX"]))

    def test_profile_not_allowed_by_niche_fails(self):
        with self.assertRaisesRegex(AssetProfileError, "not allowed by niche"):
            resolve_asset_profile("test-niche", "MI_OTRA_YO", self.registry(["GENERALES"]))

    def test_unknown_niche_fails(self):
        with self.assertRaisesRegex(AssetProfileError, "unknown niche_id"):
            resolve_asset_profile("missing", "MI_OTRA_YO", self.registry(["MI_OTRA_YO"]))
