import builtins
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.custom.mpt_engine_bridge import (
    build_mpt_aroll_broll_task_spec,
    build_mpt_video_task_from_kurukin_job,
    discover_mpt_engine_capabilities,
    summarize_mpt_task_spec,
    validate_mpt_task_spec,
)


class TestMptEngineBridge(unittest.TestCase):
    def test_capabilities_returns_dict_without_network(self):
        with mock.patch("socket.create_connection") as create_connection:
            capabilities = discover_mpt_engine_capabilities()

        self.assertIsInstance(capabilities, dict)
        self.assertTrue(capabilities["network_free"])
        self.assertIn("pexels", capabilities["sourcing"]["native_video_sources"])
        self.assertIn("pixabay", capabilities["sourcing"]["native_video_sources"])
        self.assertIn("coverr", capabilities["sourcing"]["native_video_sources"])
        create_connection.assert_not_called()

    def test_bridge_generates_generic_mpt_spec(self):
        spec = build_mpt_video_task_from_kurukin_job(
            {
                "job_id": "generic-001",
                "video_subject": "Cafe launch",
                "video_script": "A concise launch script.",
                "stock_source": "pixabay",
                "video_terms": ["coffee shop", "barista"],
                "asset_policy": {"mode": "open_sources"},
                "render_quality": "draft_720p",
            }
        )

        self.assertEqual(spec["kind"], "mpt_video_task_spec")
        self.assertEqual(spec["execution"], "spec_only")
        self.assertEqual(spec["mpt_params"]["video_source"], "pixabay")
        self.assertEqual(spec["mpt_params"]["video_resolution"], "720p")
        self.assertEqual(validate_mpt_task_spec(spec), [])

    def test_bridge_preserves_aroll_broll_render_mode_and_policy(self):
        spec = build_mpt_aroll_broll_task_spec(
            {
                "job_id": "aroll-broll-001",
                "render_mode": "aroll_broll",
                "video_subject": "Presenter edit",
                "video_script": "Presenter transcript.",
                "asset_policy": {"mode": "local_only"},
                "a_roll": {
                    "path": "storage/local_videos/presenter.mp4",
                    "audio_policy": "original",
                    "audio_path": "storage/local_audios/presenter.wav",
                },
                "b_roll": {
                    "assets": ["storage/local_videos/cutaway.mp4"],
                    "audio_policy": "muted",
                    "query": "coffee shop b roll",
                },
                "subtitles": {
                    "source": "custom_srt",
                    "custom_srt_path": "storage/local_subtitles/presenter.srt",
                },
            }
        )

        metadata = spec["kurukin_metadata"]
        self.assertEqual(metadata["render_mode"], "aroll_broll")
        self.assertEqual(metadata["asset_policy"]["mode"], "local_only")
        self.assertEqual(
            metadata["aroll_broll"]["primary_media"]["path"],
            "storage/local_videos/presenter.mp4",
        )
        self.assertEqual(
            metadata["aroll_broll"]["support_visuals"]["assets"][0]["url"],
            "storage/local_videos/cutaway.mp4",
        )
        self.assertEqual(
            metadata["aroll_broll"]["original_audio_policy"],
            "a_roll_original",
        )
        self.assertEqual(
            metadata["aroll_broll"]["subtitles_policy"]["custom_srt_path"],
            "storage/local_subtitles/presenter.srt",
        )
        self.assertEqual(
            spec["mpt_params"]["custom_audio_file"],
            "storage/local_audios/presenter.wav",
        )
        self.assertEqual(validate_mpt_task_spec(spec), [])

    def test_bridge_does_not_call_providers_or_create_pending_task(self):
        real_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            fromlist = args[2] if len(args) > 2 else kwargs.get("fromlist", ())
            if name == "app.services.material" or (
                name == "app.services" and "material" in (fromlist or ())
            ):
                raise AssertionError("provider module must not be imported")
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=guarded_import):
            spec = build_mpt_video_task_from_kurukin_job(
                {
                    "video_subject": "No execution",
                    "stock_source": "pexels",
                    "asset_policy": {"mode": "open_sources"},
                }
            )

        self.assertEqual(spec["execution"], "spec_only")
        self.assertNotIn("pending_path", spec)
        self.assertNotIn("created_task", spec)
        self.assertNotIn("task_id", spec)

    def test_aroll_broll_without_audio_path_documents_gap(self):
        spec = build_mpt_aroll_broll_task_spec(
            {
                "video_subject": "A-roll gap",
                "a_roll": {"path": "storage/local_videos/presenter.mp4"},
                "b_roll": {"assets": ["storage/local_videos/cutaway.mp4"]},
            }
        )

        self.assertTrue(spec["gaps"])
        self.assertIn("custom_audio_file", spec["gaps"][0])
        self.assertEqual(validate_mpt_task_spec(spec), [])

    def test_validate_detects_missing_fields(self):
        spec = {
            "kind": "mpt_video_task_spec",
            "execution": "spec_only",
            "safe_to_build_without_side_effects": True,
            "mpt_params": {"video_source": "local", "video_subject": ""},
            "kurukin_metadata": {
                "render_mode": "normal",
                "asset_policy": {"mode": "local_only"},
            },
        }

        errors = validate_mpt_task_spec(spec)

        self.assertIn("mpt_params.video_subject or video_script is required", errors)
        self.assertIn(
            "mpt_params.video_materials is required when video_source is local",
            errors,
        )

    def test_summary_is_human_readable(self):
        spec = build_mpt_video_task_from_kurukin_job(
            {
                "video_subject": "Summary",
                "video_materials": [
                    {"provider": "local", "url": "storage/local_videos/one.mp4"}
                ],
                "asset_policy": {"mode": "local_only"},
                "custom_audio_file": "storage/local_audios/audio.mp3",
            }
        )

        summary = summarize_mpt_task_spec(spec)

        self.assertTrue(summary["valid"], summary)
        self.assertEqual(summary["engine"], "moneyprinterturbo")
        self.assertEqual(summary["video_source"], "local")
        self.assertEqual(summary["material_count"], 1)
        self.assertTrue(summary["custom_audio"])


if __name__ == "__main__":
    unittest.main()
