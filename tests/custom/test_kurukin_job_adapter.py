import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.custom import kurukin_job_adapter as adapter
from scripts import local_job_wrapper


def make_spec(selected_assets=None):
    if selected_assets is None:
        selected_assets = [
            {"file": "clip-02.mp4", "label": "support", "order": 2},
            {"file": "clip-01.mp4", "label": "intro", "order": 1},
        ]
    return {
        "job_id": "kurukin-adapter-test",
        "description": "Adapter test",
        "selectedAssets": selected_assets,
        "video": {
            "video_subject": "Adapter test",
            "video_script": "Adapter test script.",
            "video_aspect": "9:16",
            "video_concat_mode": "sequential",
            "video_transition_mode": "None",
            "video_clip_duration": 4,
            "video_count": 1,
            "voice_name": "es-MX-DaliaNeural-Female",
            "voice_volume": 1.0,
            "voice_rate": 1.0,
            "bgm_type": "none",
            "subtitle_enabled": True,
            "n_threads": 2,
            "paragraph_number": 1,
        },
    }


class TestKurukinJobAdapter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tmp.name)
        self.videos_dir = self.base_dir / "storage" / "local_videos"
        self.audios_dir = self.base_dir / "storage" / "local_audios"
        self.subtitles_dir = self.base_dir / "storage" / "local_subtitles"
        self.asset_hub_base = self.base_dir / "job-assets"
        self.videos_dir.mkdir(parents=True)
        self.audios_dir.mkdir(parents=True)
        self.subtitles_dir.mkdir(parents=True)
        self.asset_hub_base.mkdir()
        for filename in ("clip-01.mp4", "clip-02.mp4", "clip.mp4", "photo.png"):
            (self.videos_dir / filename).write_text("dummy", encoding="utf-8")
        (self.audios_dir / "audio-prueba.mp3").write_text("dummy", encoding="utf-8")
        (self.subtitles_dir / "audio-prueba.srt").write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nHola mundo.\n\n",
            encoding="utf-8",
        )
        self.manifest_path = (
            self.asset_hub_base
            / "jab_test"
            / "manifests"
            / "renderer-manifest.json"
        )
        self.manifest_path.parent.mkdir(parents=True)
        self.manifest_path.write_text("{}", encoding="utf-8")
        self.original_project_root = adapter.PROJECT_ROOT
        self.original_wrapper_project_root = local_job_wrapper.PROJECT_ROOT
        self.original_asset_hub_base = adapter.DEFAULT_ASSET_HUB_JOB_ASSETS_DIR
        self.original_wrapper_asset_hub_base = (
            local_job_wrapper.DEFAULT_ASSET_HUB_JOB_ASSETS_DIR
        )
        adapter.PROJECT_ROOT = self.base_dir
        local_job_wrapper.PROJECT_ROOT = self.base_dir
        adapter.DEFAULT_ASSET_HUB_JOB_ASSETS_DIR = str(self.asset_hub_base)
        local_job_wrapper.DEFAULT_ASSET_HUB_JOB_ASSETS_DIR = str(self.asset_hub_base)

    def tearDown(self):
        adapter.PROJECT_ROOT = self.original_project_root
        local_job_wrapper.PROJECT_ROOT = self.original_wrapper_project_root
        adapter.DEFAULT_ASSET_HUB_JOB_ASSETS_DIR = self.original_asset_hub_base
        local_job_wrapper.DEFAULT_ASSET_HUB_JOB_ASSETS_DIR = (
            self.original_wrapper_asset_hub_base
        )
        self.tmp.cleanup()

    def write_spec(self, spec):
        path = self.base_dir / "spec.json"
        path.write_text(json.dumps(spec), encoding="utf-8")
        return path

    def build_payload(self, spec):
        return adapter.build_moneyprinter_payload(
            spec,
            local_videos_dir=self.videos_dir,
            local_audios_dir=self.audios_dir,
            local_subtitles_dir=self.subtitles_dir,
            media_probe=False,
        )

    def test_build_payload_with_local_selected_assets(self):
        payload = self.build_payload(make_spec())
        self.assertEqual(payload["video_source"], "local")
        self.assertEqual(payload["video_materials"][0]["provider"], "local")

    def test_selected_assets_do_not_remain_root(self):
        self.assertNotIn("selectedAssets", self.build_payload(make_spec()))

    def test_runner_selected_assets_exists(self):
        payload = self.build_payload(make_spec())
        self.assertIn("selectedAssets", payload["runner"])

    def test_video_materials_provider_local(self):
        payload = self.build_payload(make_spec())
        self.assertEqual(
            {material["provider"] for material in payload["video_materials"]},
            {"local"},
        )

    def test_order_by_order_is_preserved(self):
        payload = self.build_payload(make_spec())
        self.assertEqual(
            [material["url"] for material in payload["video_materials"]],
            ["clip-01.mp4", "clip-02.mp4"],
        )

    def test_selected_asset_path_traversal_fails(self):
        with self.assertRaises(adapter.KurukinJobAdapterError):
            self.build_payload(make_spec([{"file": "../clip-01.mp4"}]))

    def test_audio_file_generates_custom_audio_file(self):
        spec = make_spec()
        spec["audio"] = {"file": "audio-prueba.mp3"}
        payload = self.build_payload(spec)
        self.assertEqual(
            payload["custom_audio_file"],
            "storage/local_audios/audio-prueba.mp3",
        )

    def test_subtitles_none_disables_subtitles(self):
        spec = make_spec()
        spec["subtitles"] = {"mode": "none"}
        self.assertFalse(self.build_payload(spec)["subtitle_enabled"])

    def test_subtitles_whisper_sets_provider(self):
        spec = make_spec()
        spec["subtitles"] = {"mode": "whisper"}
        self.assertEqual(self.build_payload(spec)["subtitle_provider"], "whisper")

    def test_custom_srt_sets_custom_subtitle_file(self):
        spec = make_spec()
        spec["subtitles"] = {"mode": "custom_srt", "file": "audio-prueba.srt"}
        payload = self.build_payload(spec)
        self.assertEqual(
            payload["custom_subtitle_file"],
            "storage/local_subtitles/audio-prueba.srt",
        )

    def test_custom_srt_preserves_optimize_toggle(self):
        spec = make_spec()
        spec["subtitles"] = {
            "mode": "custom_srt",
            "file": "audio-prueba.srt",
            "optimize": False,
        }
        self.assertFalse(self.build_payload(spec)["subtitle_optimization_enabled"])

    def test_custom_audio_edge_subtitles_conflict_fails(self):
        spec = make_spec()
        spec["audio"] = {"file": "audio-prueba.mp3"}
        spec["subtitles"] = {"mode": "edge"}
        with self.assertRaisesRegex(
            adapter.KurukinJobAdapterError,
            "Edge subtitles require generated TTS audio",
        ):
            self.build_payload(spec)

    def test_render_quality_generates_video_resolution(self):
        spec = make_spec()
        spec["render_quality"] = "draft_720p"
        self.assertEqual(self.build_payload(spec)["video_resolution"], "draft_720p")

    def test_render_quality_alias_normalizes(self):
        spec = make_spec()
        spec["render_quality"] = "720p"
        self.assertEqual(self.build_payload(spec)["video_resolution"], "draft_720p")

    def test_render_quality_legacy_conflict_fails(self):
        spec = make_spec()
        spec["render_quality"] = "720p"
        spec["video"]["video_resolution"] = "2k"
        with self.assertRaises(adapter.KurukinJobAdapterError):
            self.build_payload(spec)

    def test_global_image_motion_generates_core_fields(self):
        spec = make_spec()
        spec["image_motion"] = {"enabled": True, "preset": "slow_zoom_in"}
        payload = self.build_payload(spec)
        self.assertTrue(payload["image_motion_enabled"])
        self.assertEqual(payload["image_motion_preset"], "slow_zoom_in")

    def test_image_motion_does_not_remain_root(self):
        spec = make_spec()
        spec["image_motion"] = {"enabled": True, "preset": "slow_zoom_in"}
        self.assertNotIn("image_motion", self.build_payload(spec))

    def test_runner_image_motion_exists(self):
        spec = make_spec()
        spec["image_motion"] = {"enabled": True, "preset": "slow_zoom_in"}
        self.assertIn("image_motion", self.build_payload(spec)["runner"])

    def test_image_asset_motion_generates_material_motion(self):
        spec = make_spec([{"file": "photo.png", "order": 1, "motion": "pan_up"}])
        self.assertEqual(self.build_payload(spec)["video_materials"][0]["motion"], "pan_up")

    def test_video_asset_motion_stays_runner_only(self):
        spec = make_spec([{"file": "clip.mp4", "order": 1, "motion": "pan_up"}])
        payload = self.build_payload(spec)
        self.assertNotIn("motion", payload["video_materials"][0])
        self.assertEqual(payload["runner"]["selectedAssets"][0]["motion"], "pan_up")

    def test_invalid_motion_fails(self):
        spec = make_spec([{"file": "photo.png", "motion": "spin"}])
        with self.assertRaises(adapter.KurukinJobAdapterError):
            self.build_payload(spec)

    def make_asset_hub_spec(self):
        return {
            "job_id": "asset-hub-adapter",
            "asset_hub": {
                "renderer_manifest_path": str(self.manifest_path),
                "bundle_uid": "jab_test",
                "scene_mode": "ordered",
                "strict": True,
            },
            "video": make_spec()["video"],
        }

    def test_asset_hub_generates_manifest_path(self):
        payload = self.build_payload(self.make_asset_hub_spec())
        self.assertEqual(
            payload["asset_hub_renderer_manifest_path"],
            str(self.manifest_path.resolve()),
        )

    def test_asset_hub_does_not_remain_root(self):
        self.assertNotIn("asset_hub", self.build_payload(self.make_asset_hub_spec()))

    def test_runner_asset_hub_exists(self):
        self.assertIn("asset_hub", self.build_payload(self.make_asset_hub_spec())["runner"])

    def test_selected_assets_not_required_with_asset_hub(self):
        payload = self.build_payload(self.make_asset_hub_spec())
        self.assertNotIn("video_materials", payload)

    def test_asset_hub_with_selected_assets_keeps_runner_metadata(self):
        spec = self.make_asset_hub_spec()
        spec["selectedAssets"] = [{"file": "clip-01.mp4"}]
        payload = self.build_payload(spec)
        self.assertIn("selectedAssets", payload["runner"])
        self.assertNotIn("selectedAssets", payload)
        self.assertNotIn("video_materials", payload)

    def test_asset_hub_invalid_scene_mode_fails(self):
        spec = self.make_asset_hub_spec()
        spec["asset_hub"]["scene_mode"] = "random"
        with self.assertRaises(adapter.KurukinJobAdapterError):
            self.build_payload(spec)

    def test_asset_hub_path_outside_base_fails(self):
        spec = self.make_asset_hub_spec()
        spec["asset_hub"]["renderer_manifest_path"] = str(self.base_dir / "manifest.json")
        with self.assertRaises(adapter.KurukinJobAdapterError):
            self.build_payload(spec)

    def test_legacy_video_asset_hub_fields_work(self):
        spec = self.make_asset_hub_spec()
        spec.pop("asset_hub")
        spec["video"]["asset_hub_renderer_manifest_path"] = str(self.manifest_path)
        spec["video"]["asset_hub_bundle_uid"] = "jab_test"
        payload = self.build_payload(spec)
        self.assertEqual(payload["asset_hub_bundle_uid"], "jab_test")

    def test_summarize_payload_is_not_full_payload(self):
        summary = adapter.summarize_payload(self.build_payload(make_spec()))
        self.assertNotIn("video_materials", summary)
        self.assertNotIn("runner", summary)

    def test_summarize_payload_includes_material_count(self):
        summary = adapter.summarize_payload(self.build_payload(make_spec()))
        self.assertEqual(summary["material_count"], 2)

    def test_summarize_payload_detects_custom_audio(self):
        spec = make_spec()
        spec["audio"] = {"file": "audio-prueba.mp3"}
        summary = adapter.summarize_payload(self.build_payload(spec))
        self.assertTrue(summary["has_custom_audio"])

    def test_wrapper_print_payload_matches_adapter_shape(self):
        spec = make_spec()
        spec_path = self.write_spec(spec)
        expected = self.build_payload(spec)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = local_job_wrapper.main(
                [
                    str(spec_path),
                    "--print-payload",
                    "--local-videos-dir",
                    str(self.videos_dir),
                    "--skip-media-probe",
                ]
            )
        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), expected)

    def test_wrapper_validate_only_works(self):
        spec_path = self.write_spec(make_spec())
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = local_job_wrapper.main(
                [
                    str(spec_path),
                    "--validate-only",
                    "--local-videos-dir",
                    str(self.videos_dir),
                    "--skip-media-probe",
                ]
            )
        self.assertEqual(exit_code, 0)
        self.assertIn("OK: job spec is valid", stdout.getvalue())

    def test_wrapper_enqueue_writes_pending_job(self):
        spec_path = self.write_spec(make_spec())
        queue_dir = self.base_dir / "queue"
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = local_job_wrapper.main(
                [
                    str(spec_path),
                    "--enqueue",
                    "--queue-dir",
                    str(queue_dir),
                    "--local-videos-dir",
                    str(self.videos_dir),
                    "--skip-media-probe",
                ]
            )
        pending_files = list((queue_dir / "pending").glob("*.json"))
        self.assertEqual(exit_code, 0)
        self.assertEqual(len(pending_files), 1)
        self.assertIn("enqueued:", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
