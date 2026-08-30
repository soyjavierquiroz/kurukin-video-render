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
from app.custom.kurukin_local_visual_picker import (
    discover_local_visual_candidates,
    is_safe_local_visual_path,
    pick_local_visual_for_intent,
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
        self.assertNotIn("needs_local_visual_asset", result["reasons"])
        self.assertEqual(
            result["mpt_spec"]["mpt_params"]["video_subject"],
            "5 errores al comprar una casa usada",
        )
        self.assertIn("5 errores al comprar una casa usada", result["intent"]["script"])
        self.assertGreaterEqual(len(result["intent"]["scenes"]), 3)
        self.assertIn(
            "5 errores al comprar una casa usada",
            result["intent"]["visual_keywords"],
        )
        self.assertEqual(result["intent"]["topic_plan"]["status"], "NEEDS_AUDIO")
        self.assertEqual(result["intent"]["topic_plan"]["reason"], "needs_audio_or_tts")

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
                "resolved_visual_path": "https://example.com/resolved.png",
            }
        )

        fields = {error["field"] for error in result["errors"]}
        self.assertIn("audio_path", fields)
        self.assertIn("video_path", fields)
        self.assertIn("visual_path", fields)
        self.assertIn("resolved_visual_path", fields)

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

    def test_local_visual_picker_detects_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "storage" / "local_videos" / "topic-short.mp4"
            image = root / "storage" / "local_images" / "topic.png"
            video.parent.mkdir(parents=True)
            image.parent.mkdir(parents=True)
            video.write_bytes(b"video")
            image.write_bytes(b"image")

            candidates = discover_local_visual_candidates(project_root=root)

        self.assertEqual(
            [candidate["path"] for candidate in candidates],
            [
                "storage/local_images/topic.png",
                "storage/local_videos/topic-short.mp4",
            ],
        )
        self.assertEqual({candidate["source"] for candidate in candidates}, {"local_picker_v1"})

    def test_local_visual_picker_ignores_urls_hidden_and_outside_paths(self):
        self.assertFalse(is_safe_local_visual_path("https://example.com/video.mp4"))
        self.assertFalse(is_safe_local_visual_path("http://example.com/video.mp4"))
        self.assertFalse(is_safe_local_visual_path("../storage/local_videos/video.mp4"))
        self.assertFalse(is_safe_local_visual_path("tmp/video.mp4"))
        self.assertFalse(is_safe_local_visual_path("storage/local_videos/.hidden.mp4"))
        self.assertTrue(is_safe_local_visual_path("storage/local_videos/video.mp4"))

    def test_local_visual_picker_rejects_review_and_test_artifacts(self):
        self.assertFalse(is_safe_local_visual_path(
            "storage/local_videos/human-review/test-flip-False/001_segment-001_primary_asset-1.mp4"
        ))
        self.assertFalse(is_safe_local_visual_path("tests/fixtures/production-looking.mp4"))
        self.assertTrue(is_safe_local_visual_path("storage/local_assets/approved/production-clip.mp4"))

    def test_local_visual_picker_prefers_video_over_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "storage" / "local_images" / "casa-vertical.png"
            video = root / "storage" / "local_videos" / "generic.mp4"
            image.parent.mkdir(parents=True)
            video.parent.mkdir(parents=True)
            image.write_bytes(b"image")
            video.write_bytes(b"video")

            picked = pick_local_visual_for_intent(
                {"topic": "casa"},
                project_root=root,
            )

        self.assertEqual(picked["path"], "storage/local_videos/generic.mp4")
        self.assertEqual(picked["type"], "video")

    def test_local_visual_picker_prefers_topic_name_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            generic = root / "storage" / "local_videos" / "generic.mp4"
            topic = root / "storage" / "local_videos" / "casa-usada-9x16.mp4"
            generic.parent.mkdir(parents=True)
            generic.write_bytes(b"video")
            topic.write_bytes(b"video")

            picked = pick_local_visual_for_intent(
                {"topic": "casa usada", "preset": "educational"},
                project_root=root,
            )

        self.assertEqual(picked["path"], "storage/local_videos/casa-usada-9x16.mp4")
        self.assertEqual(picked["source"], "local_picker_v1")

    def test_local_visual_picker_reports_relevance_confidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            visual = root / "storage" / "local_videos" / "casa-usada-vertical.mp4"
            visual.parent.mkdir(parents=True)
            visual.write_bytes(b"video")

            picked = pick_local_visual_for_intent(
                {
                    "topic": "5 errores al comprar una casa usada",
                    "topic_plan": {
                        "visual_keywords": [
                            "5 errores al comprar una casa usada",
                            "checklist",
                        ]
                    },
                    "format": "vertical",
                },
                project_root=root,
            )

        self.assertEqual(picked["visual_relevance_confidence"], "high")
        self.assertGreaterEqual(picked["visual_relevance_score"], 60)
        self.assertIn("casa", picked["visual_relevance_matches"])

    def test_local_visual_picker_reports_low_relevance_without_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            visual = root / "storage" / "local_videos" / "workspace-mistico.mp4"
            visual.parent.mkdir(parents=True)
            visual.write_bytes(b"video")

            picked = pick_local_visual_for_intent(
                {
                    "topic": "5 errores al comprar una casa usada",
                    "topic_plan": {
                        "visual_keywords": [
                            "5 errores al comprar una casa usada",
                            "checklist",
                        ]
                    },
                },
                project_root=root,
            )

        self.assertEqual(picked["visual_relevance_confidence"], "low")
        self.assertLess(picked["visual_relevance_score"], 0)
        self.assertIn("no_topic_keyword_match", picked["visual_relevance_reason"])

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
            result["intent"]["resolved_visual_path"],
            "storage/local_videos/auto_visual.mp4",
        )
        self.assertEqual(
            result["intent"]["visual_autofill_source"],
            "local_picker_v1",
        )
        self.assertEqual(
            result["intent"]["visual_autofill"]["source"],
            "local_picker_v1",
        )
        self.assertEqual(params["video_source"], "local")
        self.assertEqual(
            params["video_materials"][0]["url"],
            "storage/local_videos/auto_visual.mp4",
        )

    def test_topic_to_video_with_audio_autofills_local_visual_and_is_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            visual_dir = Path(tmp) / "storage" / "local_videos"
            visual_dir.mkdir(parents=True)
            (visual_dir / "casa-usada-vertical.mp4").write_bytes(b"visual")

            result = compile_job_intent_to_mpt_spec(
                {
                    "mode": MODE_TOPIC_TO_VIDEO,
                    "topic": "casa usada",
                    "audio_path": "storage/local_audios/audio.mp3",
                    "duration_seconds": 45,
                    "preset": "educational",
                },
                project_root=tmp,
            )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], STATUS_READY_TO_SUBMIT)
        self.assertEqual(result["reasons"], [])
        self.assertIn("casa usada", result["intent"]["script"])
        self.assertEqual(
            result["intent"]["resolved_visual_path"],
            "storage/local_videos/casa-usada-vertical.mp4",
        )
        self.assertEqual(result["intent"]["visual_autofill_source"], "local_picker_v1")
        self.assertEqual(result["intent"]["visual_relevance_confidence"], "high")
        self.assertGreaterEqual(result["intent"]["visual_relevance_score"], 60)
        self.assertEqual(
            result["mpt_spec"]["mpt_params"]["custom_audio_file"],
            "storage/local_audios/audio.mp3",
        )
        self.assertEqual(
            result["mpt_spec"]["mpt_params"]["video_materials"][0]["url"],
            "storage/local_videos/casa-usada-vertical.mp4",
        )
        metadata = result["mpt_spec"]["kurukin_metadata"]["metadata"]["job_intent"]
        self.assertEqual(metadata["mode"], MODE_TOPIC_TO_VIDEO)
        self.assertEqual(metadata["topic"], "casa usada")
        self.assertEqual(metadata["audio_path"], "storage/local_audios/audio.mp3")
        self.assertIn("casa usada", metadata["script"])
        self.assertEqual(metadata["topic_plan_summary"]["scene_count"], len(result["intent"]["scenes"]))

    def test_topic_to_video_preserves_provided_script(self):
        provided_script = "Hook propio.\nPunto propio.\nCierre propio."

        with tempfile.TemporaryDirectory() as tmp:
            visual_dir = Path(tmp) / "storage" / "local_videos"
            visual_dir.mkdir(parents=True)
            (visual_dir / "casa-usada-propio-vertical.mp4").write_bytes(b"visual")

            result = compile_job_intent_to_mpt_spec(
                {
                    "mode": MODE_TOPIC_TO_VIDEO,
                    "topic": "casa usada",
                    "script": provided_script,
                    "audio_path": "storage/local_audios/audio.mp3",
                    "preset": "educational",
                },
                project_root=tmp,
            )

        self.assertEqual(result["status"], STATUS_READY_TO_SUBMIT)
        self.assertEqual(result["intent"]["script"], provided_script)
        self.assertEqual(result["intent"]["topic_plan"]["script"], provided_script)
        self.assertEqual(
            result["mpt_spec"]["mpt_params"]["video_script"],
            provided_script,
        )

    def test_topic_to_video_low_relevance_visual_needs_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            visual_dir = Path(tmp) / "storage" / "local_videos"
            visual_dir.mkdir(parents=True)
            (visual_dir / "workspace-mistico-velas.mp4").write_bytes(b"visual")

            result = compile_job_intent_to_mpt_spec(
                {
                    "mode": MODE_TOPIC_TO_VIDEO,
                    "topic": "5 errores al comprar una casa usada",
                    "audio_path": "storage/local_audios/audio.mp3",
                    "duration_seconds": 4,
                    "preset": "educational",
                },
                project_root=tmp,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], STATUS_NEEDS_INPUT)
        self.assertIn("needs_relevant_local_visual_asset", result["reasons"])
        self.assertEqual(result["intent"]["visual_relevance_confidence"], "low")
        self.assertIn("visual_relevance_score", result["intent"])

    def test_topic_to_video_low_relevance_visual_can_be_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            visual_dir = Path(tmp) / "storage" / "local_videos"
            visual_dir.mkdir(parents=True)
            (visual_dir / "workspace-mistico-velas.mp4").write_bytes(b"visual")

            result = compile_job_intent_to_mpt_spec(
                {
                    "mode": MODE_TOPIC_TO_VIDEO,
                    "topic": "5 errores al comprar una casa usada",
                    "audio_path": "storage/local_audios/audio.mp3",
                    "allow_low_relevance_visual": True,
                    "duration_seconds": 4,
                    "preset": "educational",
                },
                project_root=tmp,
            )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], STATUS_READY_TO_SUBMIT)
        self.assertEqual(result["intent"]["visual_relevance_confidence"], "low")
        self.assertEqual(
            result["intent"]["visual_relevance_warning"],
            "low_relevance_visual_allowed",
        )
        self.assertIn("low_relevance_visual_allowed", result["mpt_spec"]["warnings"])

    def test_topic_to_video_low_relevance_can_prepare_mpt_stock_spec(self):
        with tempfile.TemporaryDirectory() as tmp:
            visual_dir = Path(tmp) / "storage" / "local_videos"
            visual_dir.mkdir(parents=True)
            (visual_dir / "workspace-mistico-velas.mp4").write_bytes(b"visual")

            result = compile_job_intent_to_mpt_spec(
                {
                    "mode": MODE_TOPIC_TO_VIDEO,
                    "topic": "5 errores al comprar una casa usada",
                    "audio_path": "storage/local_audios/audio.mp3",
                    "allow_mpt_stock_visuals": True,
                    "preferred_stock_source": "pexels",
                    "duration_seconds": 4,
                    "preset": "educational",
                },
                project_root=tmp,
            )

        params = result["mpt_spec"]["mpt_params"]
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], STATUS_READY_TO_SUBMIT)
        self.assertEqual(result["intent"]["visual_source_mode"], "mpt_stock")
        self.assertEqual(result["intent"]["stock_source"], "pexels")
        self.assertEqual(params["video_source"], "pexels")
        self.assertEqual(params["video_materials"], [])
        self.assertIn("5 errores al comprar una casa usada", params["video_terms"])
        self.assertIn("checklist", params["video_terms"])
        self.assertIn(
            "mpt_stock_visuals_allowed_provider_on_render",
            result["mpt_spec"]["warnings"],
        )

    def test_topic_to_video_without_local_visual_can_prepare_mpt_stock_spec(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = compile_job_intent_to_mpt_spec(
                {
                    "mode": MODE_TOPIC_TO_VIDEO,
                    "topic": "5 errores al comprar una casa usada",
                    "audio_path": "storage/local_audios/audio.mp3",
                    "allow_mpt_stock_visuals": True,
                    "preferred_stock_source": "pixabay",
                    "duration_seconds": 4,
                    "preset": "educational",
                },
                project_root=tmp,
            )

        params = result["mpt_spec"]["mpt_params"]
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["intent"]["visual_source_mode"], "mpt_stock")
        self.assertEqual(result["intent"]["stock_source"], "pixabay")
        self.assertEqual(params["video_source"], "pixabay")
        self.assertEqual(params["video_materials"], [])

    def test_topic_to_video_mpt_stock_respects_manual_stock_terms(self):
        with tempfile.TemporaryDirectory() as tmp:
            visual_dir = Path(tmp) / "storage" / "local_videos"
            visual_dir.mkdir(parents=True)
            (visual_dir / "workspace-mistico-velas.mp4").write_bytes(b"visual")

            result = compile_job_intent_to_mpt_spec(
                {
                    "mode": MODE_TOPIC_TO_VIDEO,
                    "topic": "5 errores al comprar una casa usada",
                    "audio_path": "storage/local_audios/audio.mp3",
                    "allow_mpt_stock_visuals": True,
                    "preferred_stock_source": "pexels",
                    "stock_terms": "real estate inspection, home checklist",
                    "duration_seconds": 4,
                    "preset": "educational",
                },
                project_root=tmp,
            )

        terms = result["mpt_spec"]["mpt_params"]["video_terms"]
        self.assertEqual(terms[0], "real estate inspection")
        self.assertEqual(terms[1], "home checklist")

    def test_topic_to_video_local_picker_uses_topic_plan_keywords(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            generic = root / "storage" / "local_videos" / "generic.mp4"
            keyword = root / "storage" / "local_videos" / "checklist-detalle.mp4"
            generic.parent.mkdir(parents=True)
            generic.write_bytes(b"visual")
            keyword.write_bytes(b"visual")

            result = compile_job_intent_to_mpt_spec(
                {
                    "mode": MODE_TOPIC_TO_VIDEO,
                    "topic": "casa usada",
                    "audio_path": "storage/local_audios/audio.mp3",
                    "preset": "educational",
                },
                project_root=root,
            )

        self.assertEqual(
            result["intent"]["resolved_visual_path"],
            "storage/local_videos/checklist-detalle.mp4",
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
        self.assertEqual(result["intent"]["resolved_visual_path"], "")
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
        source = "\n".join(
            [
                Path("app/custom/kurukin_job_intent.py").read_text(encoding="utf-8"),
                Path("app/custom/kurukin_local_visual_picker.py").read_text(
                    encoding="utf-8"
                ),
                Path("app/custom/kurukin_topic_planner.py").read_text(
                    encoding="utf-8"
                ),
            ]
        )

        forbidden = (
            "openai",
            "elevenlabs",
            "voice.",
            "voice.create",
            "text_to_speech",
            "search_videos_pexels",
            "search_videos_pixabay",
            "search_videos_coverr",
            "create_pexels_downloader",
            "asset_hub_manifest",
            "load_asset_hub",
            "requests.",
            "urlopen",
            "download",
            "/api/v1/videos",
        )
        for item in forbidden:
            self.assertNotIn(item, source.lower())


if __name__ == "__main__":
    unittest.main()
