import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.custom.aroll_broll_mode import (
    AROLL_AUDIO_ORIGINAL,
    BROLL_AUDIO_MUTED,
    LAYOUT_ALTERNATING_FULLSCREEN,
    RENDER_MODE_AROLL_BROLL,
    build_aroll_broll_preview_timeline,
    build_default_aroll_broll_config,
    summarize_aroll_broll_config,
    validate_aroll_broll_config,
)


class TestArollBrollMode(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
