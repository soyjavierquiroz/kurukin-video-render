"""Focused contract tests for the generic non-secret niche registry."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from scripts.niche_registry import NicheRegistryError, load_niche


VALID_NICHE = {
    "sheet_id": "sheet-id",
    "rclone_remote": "remote-name",
    "final_drive_folder_id": "folder-id",
    "default_asset_profile": "DEFAULT",
    "allowed_asset_profiles": ["DEFAULT", "OTHER"],
}


class NicheRegistryTests(unittest.TestCase):
    def write_registry(self, niches: dict) -> Path:
        temporary = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        self.addCleanup(Path(temporary.name).unlink, missing_ok=True)
        with temporary:
            json.dump({"version": 1, "niches": niches}, temporary)
        return Path(temporary.name)

    def test_valid_niche_config_loads(self):
        config = load_niche("example", self.write_registry({"example": VALID_NICHE}))
        self.assertEqual(config, VALID_NICHE)

    def test_unknown_niche_fails_clearly(self):
        with self.assertRaisesRegex(NicheRegistryError, "unknown niche_id: missing"):
            load_niche("missing", self.write_registry({"example": VALID_NICHE}))

    def test_invalid_default_profile_fails(self):
        invalid = {**VALID_NICHE, "default_asset_profile": "MISSING"}
        with self.assertRaisesRegex(NicheRegistryError, "default asset profile"):
            load_niche("example", self.write_registry({"example": invalid}))

    def test_duplicate_profiles_fail(self):
        invalid = {**VALID_NICHE, "allowed_asset_profiles": ["DEFAULT", "DEFAULT"]}
        with self.assertRaisesRegex(NicheRegistryError, "contain duplicates"):
            load_niche("example", self.write_registry({"example": invalid}))

    def test_missing_required_field_fails(self):
        invalid = dict(VALID_NICHE)
        del invalid["sheet_id"]
        with self.assertRaisesRegex(NicheRegistryError, "field 'sheet_id'"):
            load_niche("example", self.write_registry({"example": invalid}))
