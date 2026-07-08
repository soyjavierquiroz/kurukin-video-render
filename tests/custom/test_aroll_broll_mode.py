import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import app.custom.aroll_broll_mode as aroll_broll_mode
from app.custom.aroll_broll_mode import (
    AROLL_AUDIO_ORIGINAL,
    BROLL_AUDIO_MUTED,
    BROLL_SOURCE_LOCAL_ASSETS,
    LAYOUT_ALTERNATING_FULLSCREEN,
    MAX_BROLL_ASSETS,
    RENDER_MODE_AROLL_BROLL,
    build_aroll_broll_preview_timeline,
    build_aroll_broll_queue_payload,
    build_default_aroll_broll_config,
    summarize_aroll_broll_config,
    validate_aroll_broll_config,
)


class TestArollBrollMode(unittest.TestCase):
    def _local_config(self, root: Path, assets) -> dict:
        aroll = root / "storage" / "local_videos" / "presenter.mp4"
        aroll.parent.mkdir(parents=True, exist_ok=True)
        aroll.write_bytes(b"video")
        config = build_default_aroll_broll_config()
        config["a_roll"]["path"] = "storage/local_videos/presenter.mp4"
        config["b_roll"]["source"] = BROLL_SOURCE_LOCAL_ASSETS
        config["b_roll"]["assets"] = assets
        return config

    def _write_broll(self, root: Path, name: str) -> Path:
        path = root / "storage" / "local_assets" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"broll")
        return path

    def test_default_config_has_aroll_broll_render_mode(self):
        config = build_default_aroll_broll_config()

        self.assertEqual(config["render_mode"], RENDER_MODE_AROLL_BROLL)

    def test_default_aroll_audio_policy_is_original(self):
        config = build_default_aroll_broll_config()

        self.assertEqual(config["a_roll"]["audio_policy"], AROLL_AUDIO_ORIGINAL)

    def test_default_broll_audio_policy_is_muted(self):
        config = build_default_aroll_broll_config()

        self.assertEqual(config["b_roll"]["audio_policy"], BROLL_AUDIO_MUTED)

    def test_default_layout_is_alternating_fullscreen(self):
        config = build_default_aroll_broll_config()

        self.assertEqual(config["layout"]["preset"], LAYOUT_ALTERNATING_FULLSCREEN)

    def test_validate_rejects_invalid_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "storage" / "local_videos" / "presenter.mp4"
            video.parent.mkdir(parents=True)
            video.write_bytes(b"video")
            config = build_default_aroll_broll_config()
            config["a_roll"]["path"] = "storage/local_videos/presenter.mp4"
            config["layout"]["preset"] = "bad_layout"

            result = validate_aroll_broll_config(config, project_root=root)

        self.assertFalse(result["ok"])
        self.assertIn("layout.preset is not supported", result["errors"])

    def test_validate_rejects_broll_audio_policy_not_muted(self):
        config = build_default_aroll_broll_config()
        config["b_roll"]["audio_policy"] = "original"

        result = validate_aroll_broll_config(config, strict=False)

        self.assertFalse(result["ok"])
        self.assertIn("b_roll.audio_policy must be muted", result["errors"])

    def test_validate_rejects_path_traversal(self):
        config = build_default_aroll_broll_config()
        config["a_roll"]["path"] = "../presenter.mp4"

        result = validate_aroll_broll_config(config, strict=False)

        self.assertFalse(result["ok"])
        self.assertIn("a_roll.path cannot use path traversal", result["errors"])

    def test_validate_generates_manifest_path_from_bundle_uid(self):
        config = build_default_aroll_broll_config()
        config["b_roll"]["bundle_uid"] = "jab_test_bundle"

        result = validate_aroll_broll_config(config, strict=False)

        self.assertTrue(
            result["normalized"]["b_roll"]["manifest_path"].endswith(
                "/data/job-assets/jab_test_bundle/manifests/renderer-manifest.json"
            )
        )

    def test_validate_accepts_manifest_under_data_job_assets(self):
        config = build_default_aroll_broll_config()
        config["b_roll"][
            "manifest_path"
        ] = "/data/job-assets/jab_test/manifests/renderer-manifest.json"

        result = validate_aroll_broll_config(config, strict=False)

        self.assertNotIn("b_roll.manifest_path must stay under /data/job-assets", result["errors"])
        self.assertNotIn(
            "b_roll.manifest_path must match /data/job-assets/<bundle_uid>/manifests/renderer-manifest.json",
            result["errors"],
        )

    def test_validate_normalizes_one_local_broll_asset(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            asset = self._write_broll(root, "one.mp4")
            result = validate_aroll_broll_config(
                self._local_config(root, ["storage/local_assets/one.mp4"]),
                project_root=root,
            )

        self.assertTrue(result["ok"], result["errors"])
        self.assertEqual(result["normalized"]["b_roll"]["assets"], [asset.as_posix()])

    def test_validate_normalizes_multiple_local_broll_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            assets = [
                self._write_broll(root, "one.mp4"),
                self._write_broll(root, "two.mp4"),
                self._write_broll(root, "three.mp4"),
            ]
            result = validate_aroll_broll_config(
                self._local_config(
                    root,
                    [f"storage/local_assets/{asset.name}" for asset in assets],
                ),
                project_root=root,
            )

        self.assertTrue(result["ok"], result["errors"])
        self.assertEqual(
            result["normalized"]["b_roll"]["assets"],
            [asset.as_posix() for asset in assets],
        )

    def test_validate_deduplicates_local_broll_assets_preserving_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = self._write_broll(root, "first.mp4")
            second = self._write_broll(root, "second.mp4")
            result = validate_aroll_broll_config(
                self._local_config(
                    root,
                    [
                        "storage/local_assets/first.mp4",
                        "storage/local_assets/second.mp4",
                        "storage/local_assets/first.mp4",
                    ],
                ),
                project_root=root,
            )

        self.assertEqual(
            result["normalized"]["b_roll"]["assets"],
            [first.as_posix(), second.as_posix()],
        )

    def test_validate_rejects_empty_local_broll_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = validate_aroll_broll_config(
                self._local_config(root, []),
                project_root=root,
            )

        self.assertFalse(result["ok"])
        self.assertIn(
            "b_roll.assets must include at least one local asset",
            result["errors"],
        )

    def test_validate_rejects_more_than_eight_local_broll_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = []
            for index in range(MAX_BROLL_ASSETS + 1):
                asset = self._write_broll(root, f"asset-{index}.mp4")
                paths.append(f"storage/local_assets/{asset.name}")
            result = validate_aroll_broll_config(
                self._local_config(root, paths),
                project_root=root,
            )

        self.assertFalse(result["ok"])
        self.assertIn(
            "b_roll.assets cannot include more than 8 assets",
            result["errors"],
        )

    def test_validate_rejects_local_broll_asset_outside_allowed_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root / "outside.mp4"
            outside.write_bytes(b"broll")
            result = validate_aroll_broll_config(
                self._local_config(root, [outside.as_posix()]),
                project_root=root,
            )

        self.assertFalse(result["ok"])
        self.assertTrue(
            any(
                error.startswith("b_roll.assets[0] must stay under")
                for error in result["errors"]
            )
        )

    def test_validate_parses_multiline_local_broll_assets_ignoring_empty_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = self._write_broll(root, "first.mp4")
            second = self._write_broll(root, "second.mp4")
            result = validate_aroll_broll_config(
                self._local_config(
                    root,
                    "\nstorage/local_assets/first.mp4\n\n"
                    "storage/local_assets/second.mp4\n",
                ),
                project_root=root,
            )

        self.assertTrue(result["ok"], result["errors"])
        self.assertEqual(
            result["normalized"]["b_roll"]["assets"],
            [first.as_posix(), second.as_posix()],
        )

    def test_timeline_without_broll_returns_full_aroll(self):
        timeline = build_aroll_broll_preview_timeline(
            30,
            0,
            4,
            "medium",
            LAYOUT_ALTERNATING_FULLSCREEN,
        )

        self.assertEqual(
            timeline,
            [
                {
                    "start": 0.0,
                    "end": 30.0,
                    "visual": "a_roll",
                    "layout": LAYOUT_ALTERNATING_FULLSCREEN,
                }
            ],
        )

    def test_timeline_with_broll_never_exceeds_aroll_duration(self):
        timeline = build_aroll_broll_preview_timeline(
            18,
            5,
            4,
            "high",
            LAYOUT_ALTERNATING_FULLSCREEN,
        )

        self.assertLessEqual(max(item["end"] for item in timeline), 18)

    def test_timeline_respects_clip_seconds(self):
        timeline = build_aroll_broll_preview_timeline(
            30,
            2,
            4,
            "medium",
            LAYOUT_ALTERNATING_FULLSCREEN,
        )
        broll_segments = [item for item in timeline if item["visual"] == "b_roll"]

        self.assertTrue(broll_segments)
        self.assertTrue(
            all(
                round(item["end"] - item["start"], 2) <= 4
                for item in broll_segments
            )
        )

    def test_summarize_returns_presenter_audio_label(self):
        summary = summarize_aroll_broll_config(build_default_aroll_broll_config())

        self.assertEqual(summary["audio"], "Audio original del presentador")

    def test_summarize_marks_renderer_prepared(self):
        summary = summarize_aroll_broll_config(build_default_aroll_broll_config())

        self.assertEqual(summary["renderer"], "Renderer preparado: alternating_fullscreen")

    def test_build_queue_payload_marks_aroll_broll_as_guarded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "storage" / "local_videos" / "presenter.mp4"
            video.parent.mkdir(parents=True)
            video.write_bytes(b"video")
            asset_root = root / "data" / "job-assets"
            manifest = (
                asset_root
                / "jab_test_bundle"
                / "manifests"
                / "renderer-manifest.json"
            )
            manifest.parent.mkdir(parents=True)
            manifest.write_text('{"assets": []}', encoding="utf-8")
            config = build_default_aroll_broll_config()
            config["a_roll"]["path"] = "storage/local_videos/presenter.mp4"
            config["b_roll"]["bundle_uid"] = "jab_test_bundle"

            with mock.patch.object(
                aroll_broll_mode,
                "DEFAULT_ASSET_HUB_JOB_ASSETS_DIR",
                asset_root,
            ):
                payload = build_aroll_broll_queue_payload(
                    config,
                    job_id="aroll-broll-001",
                    project_root=root,
                    render_quality="draft_720p",
                    title="Presenter edit",
                )

        self.assertEqual(payload["render_mode"], RENDER_MODE_AROLL_BROLL)
        self.assertEqual(payload["aroll_broll"]["render_mode"], RENDER_MODE_AROLL_BROLL)
        self.assertEqual(payload["video_subject"], "Presenter edit")
        self.assertFalse(payload["runner"]["renderer_enabled"])
        self.assertEqual(payload["runner"]["execution_guard"], "renderer_not_enabled")

    def test_build_queue_payload_rejects_incomplete_strict_config(self):
        config = build_default_aroll_broll_config()

        with self.assertRaises(ValueError):
            build_aroll_broll_queue_payload(
                config,
                job_id="aroll-broll-001",
                strict=True,
            )


if __name__ == "__main__":
    unittest.main()
