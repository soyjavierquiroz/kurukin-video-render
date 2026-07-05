import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.custom.kurukin_render_console import (
    build_render_console_spec,
    default_asset_hub_manifest_path,
    validate_and_build_payload_from_console_spec,
)


BUNDLE_UID = "jab_b28367fb22d44a40bae507c175f464c4"


def make_spec(**overrides):
    values = {
        "job_id": "render-console-test-001",
        "video_subject": "Render Console Test",
        "video_script": "Example script.",
        "render_quality": "draft_720p",
        "video_aspect": "9:16",
        "asset_hub_bundle_uid": BUNDLE_UID,
        "subtitles_mode": "none",
    }
    values.update(overrides)
    return build_render_console_spec(**values)


class TestKurukinRenderConsole(unittest.TestCase):
    def test_default_asset_hub_manifest_path_with_bundle_uid(self):
        self.assertEqual(
            default_asset_hub_manifest_path(BUNDLE_UID),
            f"/data/job-assets/{BUNDLE_UID}/manifests/renderer-manifest.json",
        )

    def test_default_asset_hub_manifest_path_rejects_path_traversal(self):
        with self.assertRaises(ValueError):
            default_asset_hub_manifest_path("../bundle")

    def test_build_render_console_spec_with_bundle_uid_creates_asset_hub(self):
        spec = make_spec()

        self.assertEqual(spec["asset_hub"]["bundle_uid"], BUNDLE_UID)
        self.assertTrue(
            spec["asset_hub"]["renderer_manifest_path"].endswith(
                "/manifests/renderer-manifest.json"
            )
        )

    def test_build_render_console_spec_with_audio_file_creates_audio(self):
        spec = make_spec(audio_file="audio-prueba.mp3")

        self.assertEqual(spec["audio"], {"file": "audio-prueba.mp3"})

    def test_build_render_console_spec_mode_none_creates_subtitles_none(self):
        spec = make_spec(subtitles_mode="none")

        self.assertEqual(spec["subtitles"], {"mode": "none"})
        self.assertFalse(spec["video"]["subtitle_enabled"])

    def test_build_render_console_spec_mode_custom_srt_includes_file(self):
        spec = make_spec(
            subtitles_mode="custom_srt",
            custom_subtitle_file="captions.srt",
        )

        self.assertEqual(spec["subtitles"]["mode"], "custom_srt")
        self.assertEqual(spec["subtitles"]["file"], "captions.srt")

    def test_build_render_console_spec_image_motion_enabled_creates_image_motion(self):
        spec = make_spec(
            image_motion_enabled=True,
            image_motion_preset="slow_zoom_in",
            image_motion_intensity=0.06,
        )

        self.assertEqual(
            spec["image_motion"],
            {"enabled": True, "preset": "slow_zoom_in", "intensity": 0.06},
        )

    def test_validate_and_build_payload_produces_asset_hub_fields(self):
        payload, _ = validate_and_build_payload_from_console_spec(make_spec())

        self.assertEqual(payload["asset_hub_bundle_uid"], BUNDLE_UID)
        self.assertTrue(
            payload["asset_hub_renderer_manifest_path"].endswith(
                "/renderer-manifest.json"
            )
        )
        self.assertEqual(payload["video_resolution"], "draft_720p")

    def test_validate_and_build_payload_produces_summary(self):
        _, summary = validate_and_build_payload_from_console_spec(make_spec())

        self.assertEqual(summary["job_id"], "render-console-test-001")
        self.assertEqual(summary["asset_hub_bundle_uid"], BUNDLE_UID)

    def test_no_selected_assets_in_basic_form_spec(self):
        self.assertNotIn("selectedAssets", make_spec())


if __name__ == "__main__":
    unittest.main()
