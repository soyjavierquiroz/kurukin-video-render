import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.custom.asset_materializer import (
    materialize_assets_for_aroll_broll,
    normalize_asset_materialization_request,
)


class TestAssetMaterializer(unittest.TestCase):
    def test_normalize_defaults_to_open_sources_request(self):
        request = normalize_asset_materialization_request(None)

        self.assertEqual(request["asset_policy"]["mode"], "open_sources")
        self.assertEqual(request["desired_count"], 3)
        self.assertEqual(request["local_candidates"], [])
        self.assertTrue(
            request["output_dir"].startswith(
                "storage/local_videos/_aroll_broll_materialized/"
            )
        )

    def test_open_sources_uses_enough_local_candidates_without_downloader(self):
        calls = []

        def downloader(_request):
            calls.append("called")
            return ["storage/local_videos/downloaded.mp4"]

        result = materialize_assets_for_aroll_broll(
            {
                "desired_count": 2,
                "local_candidates": [
                    "storage/local_videos/one.mp4",
                    "storage/local_assets/two.mp4",
                    "storage/local_videos/one.mp4",
                ],
            },
            project_root=Path.cwd(),
            downloader=downloader,
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["source_provider"], "local_library")
        self.assertEqual(
            result["b_roll_assets"],
            ["storage/local_videos/one.mp4", "storage/local_assets/two.mp4"],
        )
        self.assertEqual(calls, [])

    def test_materializer_does_not_create_output_dir_with_enough_local_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "storage" / "local_videos" / "materialized"
            result = materialize_assets_for_aroll_broll(
                {
                    "desired_count": 2,
                    "output_dir": "storage/local_videos/materialized",
                    "local_candidates": [
                        "storage/local_videos/one.mp4",
                        "storage/local_assets/two.mp4",
                    ],
                },
                project_root=root,
            )

            self.assertTrue(result["ok"], result)
            self.assertFalse(output_dir.exists())

    def test_open_sources_uses_fake_downloader_to_complete_assets(self):
        seen_requests = []

        def fake_downloader(request):
            seen_requests.append(request)
            return {
                "source_provider": "pexels",
                "assets": [
                    "storage/local_videos/_aroll_broll_materialized/job/city.mp4",
                    "storage/local_videos/_aroll_broll_materialized/job/walk.mp4",
                ],
                "metadata": {"fake": True},
            }

        result = materialize_assets_for_aroll_broll(
            {
                "query": "city walking",
                "desired_count": 3,
                "output_dir": "storage/local_videos/_aroll_broll_materialized/job",
                "local_candidates": ["storage/local_assets/local.mp4"],
            },
            project_root=Path.cwd(),
            downloader=fake_downloader,
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["source_provider"], "mixed")
        self.assertEqual(result["b_roll_asset_count"], 3)
        self.assertEqual(result["metadata"]["query"], "city walking")
        self.assertTrue(result["metadata"]["fake"])
        self.assertEqual(seen_requests[0]["needed_count"], 2)

    def test_open_sources_accepts_fake_pexels_downloader_metadata(self):
        def fake_pexels_downloader(_request):
            return {
                "source_provider": "pexels",
                "assets": ["storage/local_videos/pexels-city.mp4"],
                "metadata": {
                    "pexels_assets": [
                        {
                            "source_provider": "pexels",
                            "pexels_video_id": "101",
                            "photographer": "Ana Video",
                            "photographer_url": "https://www.pexels.com/@ana/",
                            "pexels_url": "https://www.pexels.com/video/city-101/",
                            "width": 720,
                            "height": 1280,
                            "path": "storage/local_videos/pexels-city.mp4",
                        }
                    ]
                },
            }

        result = materialize_assets_for_aroll_broll(
            {
                "asset_policy": {
                    "mode": "open_sources",
                    "allowed_sources": ["pexels", "local_library"],
                },
                "query": "city",
                "desired_count": 1,
            },
            project_root=Path.cwd(),
            downloader=fake_pexels_downloader,
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["source_provider"], "pexels")
        self.assertEqual(
            result["metadata"]["pexels_assets"][0]["photographer"],
            "Ana Video",
        )

    def test_open_sources_combines_allowed_adapters_and_deduplicates(self):
        calls = []

        def fake_asset_hub(request):
            calls.append(("asset_hub", request["needed_count"]))
            return {
                "assets": [
                    "storage/local_assets/shared.mp4",
                    "storage/local_assets/hub.mp4",
                ],
                "metadata": {"hub_manifest": "fake"},
            }

        def fake_pexels(request):
            calls.append(("pexels", request["needed_count"]))
            return {
                "assets": [
                    "storage/local_assets/hub.mp4",
                    "storage/local_videos/pexels.mp4",
                ],
                "metadata": {"pexels_assets": [{"pexels_video_id": "42"}]},
            }

        result = materialize_assets_for_aroll_broll(
            {
                "asset_policy": {
                    "mode": "open_sources",
                    "allowed_sources": [
                        "local_library",
                        "asset_hub",
                        "pexels",
                    ],
                },
                "desired_count": 3,
                "local_candidates": ["storage/local_assets/shared.mp4"],
            },
            project_root=Path.cwd(),
            source_adapters={
                "pexels": fake_pexels,
                "asset_hub": fake_asset_hub,
            },
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(calls, [("asset_hub", 2), ("pexels", 1)])
        self.assertEqual(
            result["b_roll_assets"],
            [
                "storage/local_assets/shared.mp4",
                "storage/local_assets/hub.mp4",
                "storage/local_videos/pexels.mp4",
            ],
        )
        self.assertEqual(result["source_provider"], "mixed")

    def test_open_sources_calls_only_adapters_allowed_by_policy(self):
        calls = []

        result = materialize_assets_for_aroll_broll(
            {
                "asset_policy": {
                    "mode": "open_sources",
                    "allowed_sources": ["pexels"],
                },
                "desired_count": 1,
            },
            project_root=Path.cwd(),
            source_adapters={
                "asset_hub": lambda _request: calls.append("asset_hub"),
                "pexels": lambda _request: {
                    "assets": ["storage/local_videos/pexels.mp4"]
                },
            },
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["source_provider"], "pexels")
        self.assertEqual(calls, [])

    def test_open_sources_can_use_uploaded_adapter_after_local_candidates(self):
        calls = []

        def fake_uploaded(request):
            calls.append(request["needed_count"])
            return {"assets": ["storage/local_assets/uploaded.mp4"]}

        result = materialize_assets_for_aroll_broll(
            {
                "asset_policy": {
                    "mode": "open_sources",
                    "allowed_sources": ["local_library", "uploaded"],
                },
                "desired_count": 2,
                "local_candidates": ["storage/local_videos/local.mp4"],
            },
            project_root=Path.cwd(),
            source_adapters={"uploaded": fake_uploaded},
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(calls, [1])
        self.assertEqual(result["source_provider"], "mixed")
        self.assertEqual(
            result["b_roll_assets"],
            [
                "storage/local_videos/local.mp4",
                "storage/local_assets/uploaded.mp4",
            ],
        )

    def test_open_sources_rejects_disallowed_pexels_downloader(self):
        result = materialize_assets_for_aroll_broll(
            {
                "asset_policy": {
                    "mode": "open_sources",
                    "allowed_sources": ["asset_hub"],
                },
                "desired_count": 1,
            },
            project_root=Path.cwd(),
            downloader=lambda _request: {
                "source_provider": "pexels",
                "assets": ["storage/local_videos/pexels-city.mp4"],
            },
        )

        self.assertFalse(result["ok"])
        self.assertIn(
            "Source provider is not allowed: pexels",
            result["errors"],
        )

    def test_downloader_fake_can_return_assets_and_metadata(self):
        def fake_downloader(_request):
            return {
                "source_provider": "pexels",
                "b_roll_assets": ["storage/local_assets/downloaded.mp4"],
                "metadata": {"request_id": "fake-001"},
            }

        result = materialize_assets_for_aroll_broll(
            {
                "query": "city",
                "desired_count": 1,
                "local_candidates": [],
            },
            project_root=Path.cwd(),
            downloader=fake_downloader,
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["source_provider"], "pexels")
        self.assertEqual(result["b_roll_assets"], ["storage/local_assets/downloaded.mp4"])
        self.assertEqual(result["metadata"]["request_id"], "fake-001")

    def test_external_downloader_must_identify_its_source_provider(self):
        result = materialize_assets_for_aroll_broll(
            {"desired_count": 1, "query": "city"},
            project_root=Path.cwd(),
            downloader=lambda _request: ["storage/local_assets/downloaded.mp4"],
        )

        self.assertFalse(result["ok"])
        self.assertIn(
            "External downloader must identify source_provider",
            result["errors"],
        )
        self.assertNotEqual(result["source_provider"], "pexels")

    def test_open_sources_requires_injected_downloader_for_external_assets(self):
        result = materialize_assets_for_aroll_broll(
            {"desired_count": 1, "query": "city walking"},
            project_root=Path.cwd(),
        )

        self.assertFalse(result["ok"])
        self.assertIn("External downloader is not configured", result["errors"])

    def test_open_sources_respects_allowed_sources(self):
        calls = []

        def fake_downloader(_request):
            calls.append("called")
            return ["storage/local_videos/downloaded.mp4"]

        result = materialize_assets_for_aroll_broll(
            {
                "asset_policy": {
                    "mode": "open_sources",
                    "allowed_sources": ["local_library"],
                },
                "desired_count": 2,
                "local_candidates": ["storage/local_videos/one.mp4"],
            },
            project_root=Path.cwd(),
            downloader=fake_downloader,
        )

        self.assertFalse(result["ok"])
        self.assertIn(
            "Local-only policy requires enough local candidates",
            result["errors"],
        )
        self.assertEqual(calls, [])

    def test_local_only_uses_only_local_candidates(self):
        downloader_calls = []

        result = materialize_assets_for_aroll_broll(
            {
                "asset_policy": {"mode": "local_only"},
                "desired_count": 2,
                "local_candidates": [
                    "storage/local_videos/one.mp4",
                    "storage/local_images/two.png",
                ],
            },
            project_root=Path.cwd(),
            downloader=lambda _request: downloader_calls.append("called"),
            source_adapters={
                "pexels": lambda _request: downloader_calls.append("pexels")
            },
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["source_provider"], "local_library")
        self.assertEqual(result["b_roll_asset_count"], 2)
        self.assertEqual(downloader_calls, [])

    def test_local_only_requires_enough_candidates(self):
        result = materialize_assets_for_aroll_broll(
            {
                "asset_policy": {"mode": "local_only"},
                "desired_count": 2,
                "local_candidates": ["storage/local_videos/one.mp4"],
            },
            project_root=Path.cwd(),
        )

        self.assertFalse(result["ok"])
        self.assertIn(
            "Local-only policy requires enough local candidates",
            result["errors"],
        )

    def test_exclusive_brand_assets_requires_local_manifest(self):
        result = materialize_assets_for_aroll_broll(
            {
                "asset_policy": {
                    "mode": "exclusive_brand_assets",
                    "brand_asset_bundle_uid": "jab_test",
                },
                "desired_count": 1,
                "manifest_path": "",
                "brand_asset_bundle_uid": "",
            },
            project_root=Path.cwd(),
        )

        self.assertFalse(result["ok"])
        self.assertIn(
            "Exclusive brand assets require a local manifest",
            result["errors"],
        )

    def test_exclusive_brand_assets_uses_manifest_reader_only(self):
        downloader_calls = []

        def fake_manifest_reader(manifest_path):
            self.assertEqual(
                manifest_path,
                "/data/job-assets/jab_test/manifests/renderer-manifest.json",
            )
            return {
                "assets": [
                    {"path": "storage/local_assets/brand-one.mp4"},
                    {"local_path": "storage/local_videos/brand-two.mp4"},
                ]
            }

        def fake_downloader(_request):
            downloader_calls.append("called")
            return ["storage/local_videos/open.mp4"]

        result = materialize_assets_for_aroll_broll(
            {
                "asset_policy": {
                    "mode": "exclusive_brand_assets",
                    "brand_asset_bundle_uid": "jab_test",
                },
                "desired_count": 2,
            },
            project_root=Path.cwd(),
            downloader=fake_downloader,
            source_adapters={
                "pexels": lambda _request: downloader_calls.append("pexels")
            },
            manifest_reader=fake_manifest_reader,
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["source_provider"], "asset_hub")
        self.assertEqual(
            result["b_roll_assets"],
            [
                "storage/local_assets/brand-one.mp4",
                "storage/local_videos/brand-two.mp4",
            ],
        )
        self.assertEqual(downloader_calls, [])

    def test_exclusive_manifest_reader_fake_returns_assets(self):
        result = materialize_assets_for_aroll_broll(
            {
                "asset_policy": {
                    "mode": "exclusive_brand_assets",
                    "brand_asset_bundle_uid": "jab_test",
                },
                "desired_count": 1,
            },
            project_root=Path.cwd(),
            manifest_reader=lambda _path: {
                "assets": [{"path": "storage/local_assets/brand.mp4"}]
            },
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["source_provider"], "asset_hub")
        self.assertEqual(result["b_roll_assets"], ["storage/local_assets/brand.mp4"])

    def test_exclusive_brand_assets_blocks_non_local_manifest_assets(self):
        result = materialize_assets_for_aroll_broll(
            {
                "asset_policy": {
                    "mode": "exclusive_brand_assets",
                    "brand_asset_bundle_uid": "jab_test",
                },
                "desired_count": 1,
                "manifest_path": "/data/job-assets/jab_test/manifests/renderer-manifest.json",
            },
            project_root=Path.cwd(),
            manifest_reader=lambda _path: {"assets": ["https://example.com/a.mp4"]},
        )

        self.assertFalse(result["ok"])
        self.assertIn("Materialized assets must be local paths", result["errors"])

    def test_desired_count_must_be_between_one_and_eight(self):
        result = materialize_assets_for_aroll_broll(
            {"desired_count": 9},
            project_root=Path.cwd(),
        )

        self.assertFalse(result["ok"])
        self.assertIn("desired_count must be between 1 and 8", result["errors"])

    def test_output_dir_must_stay_under_allowed_roots(self):
        result = materialize_assets_for_aroll_broll(
            {
                "desired_count": 1,
                "output_dir": "storage/tasks/not-allowed",
                "local_candidates": ["storage/local_videos/one.mp4"],
            },
            project_root=Path.cwd(),
        )

        self.assertFalse(result["ok"])
        self.assertIn(
            "output_dir must stay under storage/local_videos or storage/local_assets",
            result["errors"],
        )

    def test_materialized_assets_must_be_local_paths(self):
        result = materialize_assets_for_aroll_broll(
            {
                "desired_count": 1,
                "local_candidates": ["/tmp/outside.mp4"],
            },
            project_root=Path.cwd(),
        )

        self.assertFalse(result["ok"])
        self.assertIn("Materialized assets must be local paths", result["errors"])

    def test_local_library_resolver_can_add_local_candidates(self):
        def resolver(request):
            self.assertEqual(request["query"], "city")
            return ["storage/local_assets/resolved.mp4"]

        result = materialize_assets_for_aroll_broll(
            {"asset_policy": {"mode": "local_only"}, "query": "city", "desired_count": 1},
            project_root=Path.cwd(),
            local_library_resolver=resolver,
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["b_roll_assets"], ["storage/local_assets/resolved.mp4"])

    def test_no_real_storage_is_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = materialize_assets_for_aroll_broll(
                {
                    "asset_policy": {"mode": "local_only"},
                    "desired_count": 1,
                    "local_candidates": ["storage/local_videos/fake.mp4"],
                },
                project_root=Path(tmp),
            )

        self.assertTrue(result["ok"], result)


if __name__ == "__main__":
    unittest.main()
