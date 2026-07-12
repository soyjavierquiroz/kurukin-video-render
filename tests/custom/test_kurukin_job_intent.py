import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.custom.kurukin_job_intent import (
    MODE_AUDIO_TO_VIDEO,
    MODE_SPEAKER_VIDEO_TO_ENHANCED_VIDEO,
    MODE_TOPIC_TO_VIDEO,
    STATUS_NEEDS_INPUT,
    STATUS_NOT_READY,
    STATUS_READY_TO_SUBMIT,
    compile_job_intent_to_mpt_spec,
    normalize_job_intent,
    validate_job_intent,
)


class TestKurukinJobIntent(unittest.TestCase):
    def test_normalize_defaults(self):
        intent = normalize_job_intent(
            {
                "mode": MODE_TOPIC_TO_VIDEO,
                "topic": "5 errores al comprar una casa usada",
            }
        )

        self.assertEqual(intent["language"], "es")
        self.assertEqual(intent["duration_seconds"], 45)
        self.assertEqual(intent["format"], "vertical")
        self.assertEqual(intent["preset"], "educational")

    def test_topic_to_video_valid_with_topic_but_needs_local_inputs(self):
        result = compile_job_intent_to_mpt_spec(
            {
                "mode": MODE_TOPIC_TO_VIDEO,
                "topic": "5 errores al comprar una casa usada",
            }
        )

        self.assertEqual(result["status"], STATUS_NEEDS_INPUT)
        self.assertIn("needs_audio_or_tts", result["reasons"])
        self.assertIn("needs_local_visual_asset", result["reasons"])
        self.assertEqual(
            result["mpt_spec"]["mpt_params"]["video_subject"],
            "5 errores al comprar una casa usada",
        )

    def test_topic_to_video_without_topic_or_script_fails(self):
        result = validate_job_intent({"mode": MODE_TOPIC_TO_VIDEO})

        self.assertFalse(result["ok"])
        self.assertEqual(result["errors"][0]["field"], "topic")

    def test_audio_to_video_requires_audio_path(self):
        result = validate_job_intent({"mode": MODE_AUDIO_TO_VIDEO})

        self.assertFalse(result["ok"])
        self.assertEqual(result["errors"][0]["field"], "audio_path")

    def test_speaker_video_to_enhanced_video_requires_video_path(self):
        result = validate_job_intent(
            {"mode": MODE_SPEAKER_VIDEO_TO_ENHANCED_VIDEO}
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["errors"][0]["field"], "video_path")

    def test_rejects_urls_in_paths(self):
        result = validate_job_intent(
            {
                "mode": MODE_AUDIO_TO_VIDEO,
                "audio_path": "https://example.com/audio.mp3",
                "video_path": "http://example.com/video.mp4",
                "visual_path": "https://example.com/visual.png",
            }
        )

        fields = {error["field"] for error in result["errors"]}
        self.assertIn("audio_path", fields)
        self.assertIn("video_path", fields)
        self.assertIn("visual_path", fields)

    def test_duration_out_of_range_fails(self):
        low = validate_job_intent(
            {
                "mode": MODE_TOPIC_TO_VIDEO,
                "topic": "Casa usada",
                "duration_seconds": 3,
            }
        )
        high = validate_job_intent(
            {
                "mode": MODE_TOPIC_TO_VIDEO,
                "topic": "Casa usada",
                "duration_seconds": 301,
            }
        )

        self.assertFalse(low["ok"])
        self.assertFalse(high["ok"])
        self.assertEqual(low["errors"][0]["field"], "duration_seconds")
        self.assertEqual(high["errors"][0]["field"], "duration_seconds")

    def test_invalid_format_fails(self):
        result = validate_job_intent(
            {
                "mode": MODE_TOPIC_TO_VIDEO,
                "topic": "Casa usada",
                "format": "panorama",
            }
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["errors"][0]["field"], "format")

    def test_compile_vertical_to_mpt_spec_with_expected_aspect(self):
        result = compile_job_intent_to_mpt_spec(
            {
                "mode": MODE_AUDIO_TO_VIDEO,
                "topic": "Casa usada",
                "audio_path": "storage/local_audios/audio.mp3",
                "video_path": "storage/local_videos/visual.mp4",
                "format": "vertical",
                "task_id": "intent-ready-001",
            }
        )

        params = result["mpt_spec"]["mpt_params"]
        self.assertEqual(result["status"], STATUS_READY_TO_SUBMIT)
        self.assertTrue(result["ok"])
        self.assertEqual(params["video_aspect"], "9:16")
        self.assertEqual(params["video_source"], "local")
        self.assertEqual(params["custom_audio_file"], "storage/local_audios/audio.mp3")
        self.assertEqual(params["video_materials"][0]["url"], "storage/local_videos/visual.mp4")

    def test_visual_path_alias_can_supply_audio_to_video_visual(self):
        result = compile_job_intent_to_mpt_spec(
            {
                "mode": MODE_AUDIO_TO_VIDEO,
                "audio_path": "storage/local_audios/audio.mp3",
                "visual_path": "storage/local_videos/visual.mp4",
            }
        )

        params = result["mpt_spec"]["mpt_params"]
        self.assertEqual(result["status"], STATUS_READY_TO_SUBMIT)
        self.assertEqual(result["intent"]["video_path"], "storage/local_videos/visual.mp4")
        self.assertEqual(params["video_materials"][0]["url"], "storage/local_videos/visual.mp4")

    def test_audio_to_video_audio_only_autofills_existing_local_visual(self):
        with tempfile.TemporaryDirectory() as tmp:
            visual_dir = Path(tmp) / "storage" / "local_videos"
            visual_dir.mkdir(parents=True)
            (visual_dir / "auto_visual.mp4").write_bytes(b"local visual placeholder")

            result = compile_job_intent_to_mpt_spec(
                {
                    "mode": MODE_AUDIO_TO_VIDEO,
                    "audio_path": "storage/local_audios/audio.mp3",
                    "topic": "Audio only intent",
                },
                project_root=tmp,
            )

        params = result["mpt_spec"]["mpt_params"]
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], STATUS_READY_TO_SUBMIT)
        self.assertEqual(result["reasons"], [])
        self.assertEqual(result["intent"]["video_path"], "storage/local_videos/auto_visual.mp4")
        self.assertEqual(
            result["intent"]["visual_autofill"]["source"],
            "audio_to_video_local_autofill",
        )
        self.assertEqual(params["video_source"], "local")
        self.assertEqual(
            params["video_materials"][0]["url"],
            "storage/local_videos/auto_visual.mp4",
        )

    def test_audio_to_video_audio_only_needs_input_without_local_visual(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = compile_job_intent_to_mpt_spec(
                {
                    "mode": MODE_AUDIO_TO_VIDEO,
                    "audio_path": "storage/local_audios/audio.mp3",
                },
                project_root=tmp,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], STATUS_NEEDS_INPUT)
        self.assertIn("needs_local_visual_asset", result["reasons"])
        self.assertEqual(result["intent"]["video_path"], "")
        self.assertEqual(result["mpt_spec"]["mpt_params"]["video_materials"], [])

    def test_speaker_video_mode_is_not_ready_without_audio_extract(self):
        result = compile_job_intent_to_mpt_spec(
            {
                "mode": MODE_SPEAKER_VIDEO_TO_ENHANCED_VIDEO,
                "video_path": "storage/local_videos/speaker.mp4",
            }
        )

        self.assertEqual(result["status"], STATUS_NOT_READY)
        self.assertIn("needs_audio_extract", result["reasons"])
        self.assertFalse(result["ok"])

    def test_compile_does_not_call_network_or_external_providers(self):
        with mock.patch.object(socket, "create_connection") as create_connection:
            result = compile_job_intent_to_mpt_spec(
                {
                    "mode": MODE_AUDIO_TO_VIDEO,
                    "audio_path": "storage/local_audios/audio.mp3",
                    "video_path": "storage/local_videos/visual.mp4",
                }
            )

        self.assertEqual(result["status"], STATUS_READY_TO_SUBMIT)
        create_connection.assert_not_called()

    def test_helper_source_does_not_reference_external_provider_calls(self):
        source = Path("app/custom/kurukin_job_intent.py").read_text(encoding="utf-8")

        forbidden = (
            "openai",
            "elevenlabs",
            "voice.",
            "voice.create",
            "text_to_speech",
            "pexels",
            "pixabay",
            "coverr",
            "asset_hub_manifest",
            "load_asset_hub",
            "requests.",
            "urlopen",
            "/api/v1/videos",
        )
        for item in forbidden:
            self.assertNotIn(item, source.lower())


if __name__ == "__main__":
    unittest.main()
