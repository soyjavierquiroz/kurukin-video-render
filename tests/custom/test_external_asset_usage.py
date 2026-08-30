import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.models.schema import MaterialInfo
from app.services import material


def make_material(provider="pexels", url="https://videos.example/clip.mp4", asset_id=""):
    item = MaterialInfo()
    item.provider = provider
    item.url = url
    item.duration = 5
    if asset_id:
        item.asset_id = asset_id
    return item


class ExternalAssetUsageTest(unittest.TestCase):
    def test_writes_valid_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            history_file = str(Path(tmp) / "asset_usage" / "external_assets_used.jsonl")
            item = make_material(asset_id="101")

            material.record_external_asset_usage(
                item,
                task_id="task-001",
                subject="city",
                history_file=history_file,
            )

            lines = Path(history_file).read_text(encoding="utf-8").splitlines()
            payload = json.loads(lines[0])

        self.assertEqual(payload["provider"], "pexels")
        self.assertEqual(payload["asset_id"], "101")
        self.assertEqual(payload["url"], "https://videos.example/clip.mp4")
        self.assertEqual(payload["task_id"], "task-001")
        self.assertEqual(payload["subject"], "city")
        self.assertEqual(payload["transform"], "none")
        self.assertIn("used_at", payload)

    def test_detects_recent_asset_by_provider_and_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            history_file = str(Path(tmp) / "history.jsonl")
            material.record_external_asset_usage(
                make_material(asset_id="101"),
                task_id="task-001",
                history_file=history_file,
            )

            self.assertTrue(
                material.is_recent_external_asset(
                    make_material(asset_id="101", url="https://other.example/clip.mp4"),
                    history_file=history_file,
                )
            )

    def test_detects_recent_asset_by_provider_and_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            history_file = str(Path(tmp) / "history.jsonl")
            material.record_external_asset_usage(
                make_material(asset_id=""),
                task_id="task-001",
                history_file=history_file,
            )

            self.assertTrue(
                material.is_recent_external_asset(
                    make_material(asset_id=""),
                    history_file=history_file,
                )
            )

    def test_fallback_does_not_break_when_history_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing_history = str(Path(tmp) / "missing" / "history.jsonl")
            items, dedup_fallback = material.filter_recent_external_assets(
                [make_material(asset_id="101")],
                history_file=missing_history,
            )

        self.assertEqual(len(items), 1)
        self.assertFalse(dedup_fallback)

    def test_no_recent_alternatives_use_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            history_file = str(Path(tmp) / "history.jsonl")
            item = make_material(asset_id="101")
            material.record_external_asset_usage(
                item,
                task_id="task-001",
                history_file=history_file,
            )

            items, dedup_fallback = material.filter_recent_external_assets(
                [item],
                history_file=history_file,
            )
            material.record_external_asset_usage(
                items[0],
                task_id="task-002",
                dedup_fallback=dedup_fallback,
                history_file=history_file,
            )
            payload = json.loads(Path(history_file).read_text(encoding="utf-8").splitlines()[-1])

        self.assertTrue(dedup_fallback)
        self.assertTrue(payload["dedup_fallback"])

    def test_does_not_register_local_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            history_file = str(Path(tmp) / "history.jsonl")

            material.record_external_asset_usage(
                make_material(provider="local", url="storage/local_videos/clip.mp4"),
                task_id="task-001",
                history_file=history_file,
            )

            self.assertFalse(Path(history_file).exists())

    def test_parse_material_sources_accepts_mixed_and_comma_list(self):
        self.assertEqual(
            material.parse_material_sources("mixed"),
            ("pexels", "pixabay", "coverr"),
        )
        self.assertEqual(
            material.parse_material_sources("pexels,pixabay,coverr"),
            ("pexels", "pixabay", "coverr"),
        )

    def test_rotated_material_sources_uses_expected_order(self):
        self.assertEqual(
            material.rotated_material_sources("mixed", 0),
            ("pexels", "pixabay", "coverr"),
        )
        self.assertEqual(
            material.rotated_material_sources("mixed", 1),
            ("pixabay", "coverr", "pexels"),
        )
        self.assertEqual(
            material.rotated_material_sources("mixed", 2),
            ("coverr", "pexels", "pixabay"),
        )

    def test_search_videos_with_fallback_tries_next_source(self):
        calls = []

        def fake_search(provider):
            def _search(**kwargs):
                calls.append(provider)
                if provider == "pixabay":
                    return [make_material(provider="pixabay", asset_id="202")]
                return []

            return _search

        with patch.object(material, "_search_videos_for_source", side_effect=fake_search):
            items, provider = material.search_videos_with_fallback(
                search_term="city",
                minimum_duration=5,
                source="mixed",
                index=0,
            )

        self.assertEqual(calls, ["pexels", "pixabay"])
        self.assertEqual(provider, "pixabay")
        self.assertEqual(items[0].provider, "pixabay")


if __name__ == "__main__":
    unittest.main()
