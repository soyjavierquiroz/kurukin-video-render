import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.custom.kurukin_render_console import (
    build_operator_summary,
    build_render_console_spec,
    default_asset_hub_manifest_path,
    get_manifest_summary_for_ui,
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
    def test_get_manifest_summary_for_ui_with_empty_path(self):
        self.assertEqual(
            get_manifest_summary_for_ui(""),
            {
                "exists": False,
                "status": "missing_path",
                "message": "No manifest path provided",
            },
        )

    def test_get_manifest_summary_for_ui_with_missing_path(self):
        summary = get_manifest_summary_for_ui("/data/job-assets/missing/manifest.json")

        self.assertEqual(summary["exists"], False)
        self.assertEqual(summary["status"], "not_found")
        self.assertEqual(summary["message"], "Manifest file not found")

    def test_get_manifest_summary_for_ui_with_valid_manifest(self):
        original_base = os.environ.get("ASSET_HUB_JOB_ASSETS_DIR")
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp) / "job-assets"
            bundle_dir = base_dir / "jab_test"
            image_path = bundle_dir / "scene-00" / "still-a.png"
            video_path = bundle_dir / "scene-01" / "clip-a.mp4"
            for asset_path in (image_path, video_path):
                asset_path.parent.mkdir(parents=True, exist_ok=True)
                asset_path.write_text("dummy", encoding="utf-8")
            manifest_path = bundle_dir / "manifests" / "renderer-manifest.json"
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(
                json.dumps(
                    {
                        "manifest_version": "1.0",
                        "generated_by": "kurukin-asset-hub",
                        "bundle_uid": "jab_test",
                        "job_id": "asset-job-001",
                        "scenes": [
                            {
                                "scene_index": 0,
                                "needs_human_review": True,
                                "assets": [
                                    {
                                        "type": "image",
                                        "filename": "still-a.png",
                                        "local_path": str(image_path),
                                        "duration_seconds": 3,
                                        "safe_for_subtitles": False,
                                    },
                                    {
                                        "type": "video",
                                        "filename": "clip-a.mp4",
                                        "local_path": str(video_path),
                                        "duration_seconds": 4,
                                        "safe_for_text_overlay": False,
                                    },
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            os.environ["ASSET_HUB_JOB_ASSETS_DIR"] = str(base_dir)

            summary = get_manifest_summary_for_ui(str(manifest_path))

        if original_base is None:
            os.environ.pop("ASSET_HUB_JOB_ASSETS_DIR", None)
        else:
            os.environ["ASSET_HUB_JOB_ASSETS_DIR"] = original_base

        self.assertEqual(summary["exists"], True)
        self.assertEqual(summary["status"], "ready")
        self.assertEqual(summary["bundle_uid"], "jab_test")
        self.assertEqual(summary["job_id"], "asset-job-001")
        self.assertEqual(summary["total_scenes"], 1)
        self.assertEqual(summary["total_assets"], 2)
        self.assertEqual(summary["asset_types"], {"image": 1, "video": 1})
        self.assertEqual(summary["duration_total_seconds"], 7.0)
        self.assertEqual(summary["preview_filenames"], ["still-a.png", "clip-a.mp4"])
        self.assertEqual(summary["needs_human_review_count"], 1)
        self.assertEqual(summary["safe_for_subtitles_false_count"], 1)
        self.assertEqual(summary["safe_for_text_overlay_false_count"], 1)

    def test_get_manifest_summary_for_ui_with_invalid_manifest(self):
        original_base = os.environ.get("ASSET_HUB_JOB_ASSETS_DIR")
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp) / "job-assets"
            manifest_path = base_dir / "jab_test" / "manifests" / "renderer-manifest.json"
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(
                json.dumps({"manifest_version": "2.0"}),
                encoding="utf-8",
            )
            os.environ["ASSET_HUB_JOB_ASSETS_DIR"] = str(base_dir)

            summary = get_manifest_summary_for_ui(str(manifest_path))

        if original_base is None:
            os.environ.pop("ASSET_HUB_JOB_ASSETS_DIR", None)
        else:
            os.environ["ASSET_HUB_JOB_ASSETS_DIR"] = original_base

        self.assertEqual(summary["exists"], True)
        self.assertEqual(summary["status"], "invalid")
        self.assertIn("manifest_version", summary["message"])

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

    def test_build_operator_summary_for_asset_hub_manifest_notes_deferred_assets(self):
        payload, _ = validate_and_build_payload_from_console_spec(make_spec())
        operator = build_operator_summary(
            payload,
            {"status": "ready", "total_assets": 3, "bundle_uid": BUNDLE_UID},
        )

        self.assertEqual(operator["mode"], "Asset Hub manifest")
        self.assertEqual(operator["payload_material_count"], 0)
        self.assertEqual(operator["manifest_asset_count"], 3)
        self.assertEqual(
            operator["note"],
            "Los assets se resolverán desde el manifest cuando el worker inicie el render.",
        )

    def test_build_operator_summary_for_local_materials(self):
        payload = {
            "job_id": "local-job-001",
            "video_subject": "Local assets",
            "video_source": "local",
            "video_resolution": "draft_720p",
            "video_aspect": "16:9",
            "subtitle_enabled": False,
            "image_motion_enabled": False,
            "video_materials": [
                {"provider": "local", "url": "clip-01.mp4"},
                {"provider": "local", "url": "clip-02.mp4"},
            ],
            "runner": {},
        }

        operator = build_operator_summary(payload)

        self.assertEqual(operator["mode"], "Local selected assets")
        self.assertEqual(operator["payload_material_count"], 2)
        self.assertEqual(operator["manifest_asset_count"], 0)
        self.assertEqual(operator["note"], "")

    def test_no_selected_assets_in_basic_form_spec(self):
        self.assertNotIn("selectedAssets", make_spec())


if __name__ == "__main__":
    unittest.main()
