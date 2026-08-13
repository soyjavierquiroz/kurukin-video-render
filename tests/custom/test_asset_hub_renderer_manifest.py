import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from enum import Enum
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_INSERTED_NUMPY_STUB = False
_NUMPY_STUB = None

from app.custom import asset_hub_manifest
from scripts import local_job_wrapper, nightly_runner


class _Logger:
    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass

    def debug(self, *args, **kwargs):
        pass

    def success(self, *args, **kwargs):
        pass


class _FakeClip:
    def __init__(self, size=(600, 800)):
        self.size = size
        self.reader = None
        self.audio = None
        self.mask = None
        self.clips = []


def _install_import_stubs() -> None:
    global _INSERTED_NUMPY_STUB, _NUMPY_STUB
    loguru = ModuleType("loguru")
    loguru.logger = _Logger()
    sys.modules.setdefault("loguru", loguru)

    config_module = ModuleType("app.config")
    config_module.config = SimpleNamespace(app={}, ui={"subtitle_position": "bottom"})
    sys.modules.setdefault("app.config", config_module)

    schema_module = ModuleType("app.models.schema")

    class VideoAspect(str, Enum):
        landscape = "16:9"
        portrait = "9:16"
        square = "1:1"

        def to_resolution(self):
            if self == VideoAspect.landscape:
                return 1920, 1080
            if self == VideoAspect.portrait:
                return 1080, 1920
            if self == VideoAspect.square:
                return 1080, 1080
            raise ValueError(f"unsupported video aspect: {self}")

    class VideoConcatMode(str, Enum):
        random = "random"
        sequential = "sequential"

    class VideoTransitionMode(str, Enum):
        none = None
        shuffle = "Shuffle"
        fade_in = "FadeIn"
        fade_out = "FadeOut"
        slide_in = "SlideIn"
        slide_out = "SlideOut"

    schema_module.MaterialInfo = SimpleNamespace
    schema_module.VideoAspect = VideoAspect
    schema_module.VideoConcatMode = VideoConcatMode
    schema_module.VideoParams = SimpleNamespace
    schema_module.VideoTransitionMode = VideoTransitionMode
    sys.modules.setdefault("app.models.schema", schema_module)

    numpy_module = ModuleType("numpy")
    numpy_module.where = lambda *args, **kwargs: ([], [])
    numpy_module.array = lambda value, *args, **kwargs: value
    if "numpy" not in sys.modules:
        sys.modules["numpy"] = numpy_module
        _INSERTED_NUMPY_STUB = True
        _NUMPY_STUB = numpy_module

    moviepy = ModuleType("moviepy")
    for name in (
        "AudioFileClip",
        "ColorClip",
        "CompositeAudioClip",
        "CompositeVideoClip",
        "ImageClip",
        "TextClip",
        "VideoFileClip",
        "Clip",
    ):
        setattr(moviepy, name, _FakeClip)
    moviepy.afx = SimpleNamespace()
    moviepy.vfx = SimpleNamespace()
    sys.modules.setdefault("moviepy", moviepy)

    subtitles_module = ModuleType("moviepy.video.tools.subtitles")
    subtitles_module.SubtitlesClip = _FakeClip
    sys.modules.setdefault("moviepy.video", ModuleType("moviepy.video"))
    sys.modules.setdefault("moviepy.video.tools", ModuleType("moviepy.video.tools"))
    sys.modules.setdefault("moviepy.video.tools.subtitles", subtitles_module)

    pil_module = ModuleType("PIL")
    sys.modules.setdefault("PIL", pil_module)
    sys.modules.setdefault("PIL.Image", ModuleType("PIL.Image"))
    sys.modules.setdefault("PIL.ImageDraw", ModuleType("PIL.ImageDraw"))
    sys.modules.setdefault("PIL.ImageFont", ModuleType("PIL.ImageFont"))
    sys.modules.setdefault(
        "app.services.utils.video_effects",
        ModuleType("app.services.utils.video_effects"),
    )

    for module_name in ("llm", "material", "twelvelabs"):
        sys.modules.setdefault(
            f"app.services.{module_name}",
            ModuleType(f"app.services.{module_name}"),
        )

    subtitle_module = ModuleType("app.services.subtitle")
    subtitle_module.file_to_subtitles = lambda *args, **kwargs: ["ok"]
    subtitle_module.create = lambda *args, **kwargs: None
    subtitle_module.correct = lambda *args, **kwargs: None
    sys.modules.setdefault("app.services.subtitle", subtitle_module)

    voice_module = ModuleType("app.services.voice")
    voice_module.create_subtitle = lambda *args, **kwargs: None
    voice_module.get_audio_duration = lambda *args, **kwargs: 1
    voice_module.parse_voice_name = lambda value: value
    voice_module.tts = lambda *args, **kwargs: object()
    sys.modules.setdefault("app.services.voice", voice_module)

    upload_post_module = ModuleType("app.services.upload_post")
    upload_post_module.upload_post_service = SimpleNamespace(
        is_configured=lambda: False,
        auto_upload=False,
        platforms=[],
    )
    upload_post_module.cross_post_video = lambda *args, **kwargs: {}
    sys.modules.setdefault("app.services.upload_post", upload_post_module)

    state_module = ModuleType("app.services.state")
    state_module.state = SimpleNamespace(update_task=lambda *args, **kwargs: None)
    sys.modules.setdefault("app.services.state", state_module)


_install_import_stubs()


def tearDownModule() -> None:
    if _INSERTED_NUMPY_STUB and sys.modules.get("numpy") is _NUMPY_STUB:
        del sys.modules["numpy"]

from app.services import task as task_service  # noqa: E402
from app.services import video as video_service  # noqa: E402


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


class AssetHubFixtureMixin:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tmp.name) / "job-assets"
        self.bundle_dir = self.base_dir / "jab_test"
        self.manifest_path = self.bundle_dir / "manifests" / "renderer-manifest.json"
        self.video_a = self.bundle_dir / "scene-00" / "clip-a.mp4"
        self.video_b = self.bundle_dir / "scene-01" / "clip-b.mp4"
        self.image_a = self.bundle_dir / "scene-01" / "still-a.png"
        for asset_path in (self.video_a, self.video_b, self.image_a):
            asset_path.parent.mkdir(parents=True, exist_ok=True)
            asset_path.write_text("dummy", encoding="utf-8")
        self.original_env = os.environ.get("ASSET_HUB_JOB_ASSETS_DIR")
        self.original_wrapper_asset_hub_base = (
            local_job_wrapper.DEFAULT_ASSET_HUB_JOB_ASSETS_DIR
        )
        os.environ["ASSET_HUB_JOB_ASSETS_DIR"] = str(self.base_dir)
        local_job_wrapper.DEFAULT_ASSET_HUB_JOB_ASSETS_DIR = str(self.base_dir)
        _write_json(self.manifest_path, self.make_manifest())

    def tearDown(self):
        if self.original_env is None:
            os.environ.pop("ASSET_HUB_JOB_ASSETS_DIR", None)
        else:
            os.environ["ASSET_HUB_JOB_ASSETS_DIR"] = self.original_env
        local_job_wrapper.DEFAULT_ASSET_HUB_JOB_ASSETS_DIR = (
            self.original_wrapper_asset_hub_base
        )
        self.tmp.cleanup()

    def make_manifest(self, **overrides):
        manifest = {
            "manifest_version": "1.0",
            "generated_by": "kurukin-asset-hub",
            "bundle_uid": "jab_test",
            "job_id": "job-123",
            "scenes": [
                {
                    "scene_id": "scene-1",
                    "scene_index": 1,
                    "assets": [
                        {
                            "asset_uid": "image-a",
                            "asset_id": "image-a",
                            "status": "ready",
                            "type": "image",
                            "filename": "still-a.png",
                            "local_path": str(self.image_a),
                            "duration_seconds": 3,
                            "safe_for_subtitles": False,
                        },
                        {
                            "asset_uid": "video-b",
                            "asset_id": "video-b",
                            "status": "ready",
                            "type": "video",
                            "filename": "clip-b.mp4",
                            "local_path": str(self.video_b),
                            "duration_seconds": 4,
                            "needs_human_review": True,
                        },
                    ],
                },
                {
                    "scene_id": "scene-0",
                    "scene_index": 0,
                    "assets": [
                        {
                            "asset_uid": "video-a",
                            "asset_id": "video-a",
                            "status": "ready",
                            "type": "video",
                            "filename": "clip-a.mp4",
                            "local_path": str(self.video_a),
                            "duration_seconds": 5,
                            "rank": 1,
                        }
                    ],
                },
            ],
        }
        manifest.update(overrides)
        return manifest


class TestAssetHubRendererManifest(AssetHubFixtureMixin, unittest.TestCase):
    def test_loads_valid_manifest_v1(self):
        manifest = asset_hub_manifest.load_asset_hub_renderer_manifest(
            str(self.manifest_path)
        )
        self.assertEqual(manifest["bundle_uid"], "jab_test")

    def test_rejects_invalid_manifest_version(self):
        manifest = self.make_manifest(manifest_version="2.0")
        with self.assertRaises(ValueError):
            asset_hub_manifest.validate_asset_hub_renderer_manifest(manifest)

    def test_rejects_invalid_generated_by(self):
        manifest = self.make_manifest(generated_by="other")
        with self.assertRaises(ValueError):
            asset_hub_manifest.validate_asset_hub_renderer_manifest(manifest)

    def test_rejects_missing_scenes(self):
        manifest = self.make_manifest(scenes=[])
        with self.assertRaises(ValueError):
            asset_hub_manifest.validate_asset_hub_renderer_manifest(manifest)

    def test_extracts_assets_by_scene_index_and_preserves_scene_order(self):
        assets = asset_hub_manifest.extract_asset_hub_local_assets(
            self.make_manifest()
        )
        self.assertEqual(
            [asset["asset_id"] for asset in assets],
            ["video-a", "image-a", "video-b"],
        )

    def test_rejects_local_path_outside_base_dir(self):
        outside = Path(self.tmp.name) / "outside.mp4"
        outside.write_text("dummy", encoding="utf-8")
        manifest = self.make_manifest()
        manifest["scenes"][0]["assets"][0]["local_path"] = str(outside)
        with self.assertRaises(ValueError):
            asset_hub_manifest.extract_asset_hub_local_assets(manifest)

    def test_strict_true_fails_missing_file(self):
        self.video_a.unlink()
        with self.assertRaises(ValueError):
            asset_hub_manifest.extract_asset_hub_local_assets(
                self.make_manifest(),
                strict=True,
            )

    def test_strict_false_skips_missing_file(self):
        self.video_a.unlink()
        assets = asset_hub_manifest.extract_asset_hub_local_assets(
            self.make_manifest(),
            strict=False,
        )
        self.assertNotIn("video-a", [asset["asset_id"] for asset in assets])

    def test_accepts_video_and_image_assets(self):
        assets = asset_hub_manifest.extract_asset_hub_local_assets(
            self.make_manifest()
        )
        self.assertEqual([asset["type"] for asset in assets], ["video", "image", "video"])

    def test_local_path_without_status_is_not_consumed(self):
        manifest = self.make_manifest(
            scenes=[
                {
                    "scene_id": "scene-0",
                    "scene_index": 0,
                    "assets": [
                        {
                            "asset_uid": "video-a",
                            "asset_id": "video-a",
                            "type": "video",
                            "filename": "clip-a.mp4",
                            "local_path": str(self.video_a),
                        }
                    ],
                }
            ]
        )

        with self.assertRaises(ValueError):
            asset_hub_manifest.extract_asset_hub_local_assets(manifest)

    def test_manifest_asset_without_asset_uid_fails(self):
        manifest = self.make_manifest()
        manifest["scenes"][0]["assets"][0].pop("asset_uid")
        manifest["scenes"][0]["assets"][0]["asset_id"] = "123"

        with self.assertRaises(ValueError):
            asset_hub_manifest.extract_asset_hub_local_assets(manifest)

    def test_unsupported_type_fails_or_skips_by_strict(self):
        manifest = self.make_manifest()
        manifest["scenes"][0]["assets"][0]["type"] = "audio"
        with self.assertRaises(ValueError):
            asset_hub_manifest.extract_asset_hub_local_assets(manifest, strict=True)
        assets = asset_hub_manifest.extract_asset_hub_local_assets(
            manifest,
            strict=False,
        )
        self.assertNotIn("image-a", [asset["asset_id"] for asset in assets])

    def test_collect_warnings_does_not_fail(self):
        warnings = asset_hub_manifest.collect_asset_hub_render_warnings(
            self.make_manifest()
        )
        self.assertTrue(any("safe_for_subtitles=false" in item for item in warnings))

    def test_summary_is_safe_and_compact(self):
        summary = asset_hub_manifest.summarize_asset_hub_manifest(self.make_manifest())
        self.assertEqual(summary["bundle_uid"], "jab_test")
        self.assertEqual(summary["total_scenes"], 2)
        self.assertNotIn("scenes", summary)

    def test_converts_to_asset_hub_material_info(self):
        materials = asset_hub_manifest.convert_asset_hub_manifest_to_materials(
            self.make_manifest()
        )
        self.assertEqual(materials[0].provider, "asset_hub")
        self.assertEqual(materials[0].url, str(self.video_a.resolve()))
        self.assertEqual(materials[0].duration, 5)
        self.assertEqual(materials[0].motion, "")

    def test_apply_manifest_replaces_materials_and_sets_local_source(self):
        params = SimpleNamespace(
            asset_hub_renderer_manifest_path=str(self.manifest_path),
            asset_hub_bundle_uid="jab_test",
            asset_hub_scene_mode="ordered",
            asset_hub_strict=True,
            video_source="pexels",
            video_materials=[SimpleNamespace(provider="local", url="old.mp4")],
            video_terms=["old"],
        )
        summary = task_service.apply_asset_hub_renderer_manifest(params)
        self.assertEqual(summary["bundle_uid"], "jab_test")
        self.assertEqual(params.video_source, "local")
        self.assertEqual(params.video_terms, [])
        self.assertEqual(params.video_materials[0].provider, "asset_hub")

    def test_apply_manifest_bundle_mismatch_fails(self):
        params = SimpleNamespace(
            asset_hub_renderer_manifest_path=str(self.manifest_path),
            asset_hub_bundle_uid="other",
            asset_hub_scene_mode="ordered",
            asset_hub_strict=True,
            video_source="pexels",
            video_materials=[],
        )
        with self.assertRaises(ValueError):
            task_service.apply_asset_hub_renderer_manifest(params)

    def test_apply_manifest_invalid_scene_mode_fails(self):
        params = SimpleNamespace(
            asset_hub_renderer_manifest_path=str(self.manifest_path),
            asset_hub_bundle_uid="jab_test",
            asset_hub_scene_mode="random",
            asset_hub_strict=True,
            video_source="pexels",
            video_materials=[],
        )
        with self.assertRaises(ValueError):
            task_service.apply_asset_hub_renderer_manifest(params)

    def test_apply_manifest_absent_does_not_change_params(self):
        params = SimpleNamespace(
            asset_hub_renderer_manifest_path="",
            video_source="pexels",
            video_materials=["keep"],
        )
        self.assertIsNone(task_service.apply_asset_hub_renderer_manifest(params))
        self.assertEqual(params.video_source, "pexels")
        self.assertEqual(params.video_materials, ["keep"])

    def test_video_provider_asset_hub_allows_base_dir_file(self):
        material = SimpleNamespace(
            provider="asset_hub",
            url=str(self.video_a),
            duration=0,
        )
        with patch.object(
            video_service,
            "_open_video_clip_quietly",
            return_value=_FakeClip(),
        ):
            result = video_service.preprocess_video([material])
        self.assertEqual(result[0].url, str(self.video_a.resolve()))

    def test_video_provider_asset_hub_rejects_outside_base_dir(self):
        outside = Path(self.tmp.name) / "outside.mp4"
        outside.write_text("dummy", encoding="utf-8")
        material = SimpleNamespace(
            provider="asset_hub",
            url=str(outside),
            duration=0,
        )
        self.assertEqual(video_service.preprocess_video([material]), [])

    def test_video_provider_local_still_restricts_to_local_videos(self):
        material = SimpleNamespace(
            provider="local",
            url=str(self.video_a),
            duration=0,
        )
        self.assertEqual(video_service.preprocess_video([material]), [])

    def test_wrapper_asset_hub_top_level_maps_to_payload(self):
        spec = self.make_wrapper_spec(include_selected_assets=False)
        assets = local_job_wrapper.validate_job_spec(spec, skip_media_probe=True)
        payload = local_job_wrapper.build_pending_job(spec, assets)
        self.assertEqual(
            payload["asset_hub_renderer_manifest_path"],
            str(self.manifest_path.resolve()),
        )
        self.assertNotIn("asset_hub", payload)
        self.assertIn("asset_hub", payload["runner"])
        self.assertNotIn("video_materials", payload)

    def test_wrapper_does_not_require_selected_assets_with_asset_hub(self):
        spec = self.make_wrapper_spec(include_selected_assets=False)
        assets = local_job_wrapper.validate_job_spec(spec, skip_media_probe=True)
        self.assertEqual(assets, [])

    def test_wrapper_selected_assets_with_asset_hub_stay_runner_only(self):
        spec = self.make_wrapper_spec(include_selected_assets=True)
        assets_dir = Path(self.tmp.name) / "local_videos"
        assets_dir.mkdir()
        (assets_dir / "clip-local.mp4").write_text("dummy", encoding="utf-8")
        assets = local_job_wrapper.validate_job_spec(
            spec,
            local_videos_dir=assets_dir,
            skip_media_probe=True,
        )
        payload = local_job_wrapper.build_pending_job(spec, assets)
        self.assertIn("selectedAssets", payload["runner"])
        self.assertNotIn("selectedAssets", payload)
        self.assertNotIn("video_materials", payload)

    def test_wrapper_invalid_scene_mode_fails(self):
        spec = self.make_wrapper_spec(include_selected_assets=False)
        spec["asset_hub"]["scene_mode"] = "random"
        assets = local_job_wrapper.validate_job_spec(spec, skip_media_probe=True)
        with self.assertRaises(local_job_wrapper.LocalJobWrapperError):
            local_job_wrapper.build_pending_job(spec, assets)

    def test_wrapper_path_outside_job_assets_fails(self):
        spec = self.make_wrapper_spec(include_selected_assets=False)
        spec["asset_hub"]["renderer_manifest_path"] = "/tmp/renderer-manifest.json"
        assets = local_job_wrapper.validate_job_spec(spec, skip_media_probe=True)
        with self.assertRaises(local_job_wrapper.LocalJobWrapperError):
            local_job_wrapper.build_pending_job(spec, assets)

    def test_wrapper_legacy_direct_video_fields_are_preserved(self):
        spec = self.make_wrapper_spec(include_selected_assets=False)
        spec.pop("asset_hub")
        spec["video"]["asset_hub_renderer_manifest_path"] = str(self.manifest_path)
        spec["video"]["asset_hub_bundle_uid"] = "jab_test"
        spec["video"]["asset_hub_scene_mode"] = "ordered"
        spec["video"]["asset_hub_strict"] = False
        assets = local_job_wrapper.validate_job_spec(spec, skip_media_probe=True)
        payload = local_job_wrapper.build_pending_job(spec, assets)
        self.assertEqual(payload["asset_hub_bundle_uid"], "jab_test")
        self.assertFalse(payload["asset_hub_strict"])

    def test_wrapper_print_payload_keeps_asset_hub_under_runner(self):
        spec = self.make_wrapper_spec(include_selected_assets=False)
        spec_path = Path(self.tmp.name) / "job.json"
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = local_job_wrapper.main(
                [str(spec_path), "--print-payload", "--skip-media-probe"]
            )
        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertIn("asset_hub", payload["runner"])
        self.assertNotIn("asset_hub", payload)

    def test_nightly_runner_allows_asset_hub_manifest_without_video_materials(self):
        payload = nightly_runner.validate_job(
            {
                "job_id": "asset-hub-runner",
                "video_subject": "Asset Hub runner",
                "video_aspect": "9:16",
                "video_source": "local",
                "asset_hub_renderer_manifest_path": str(self.manifest_path),
            }
        )
        self.assertIn("asset_hub_renderer_manifest_path", payload)
        self.assertNotIn("job_id", payload)

    def make_wrapper_spec(self, *, include_selected_assets: bool) -> dict:
        spec = {
            "job_id": "asset-hub-wrapper-test",
            "asset_hub": {
                "renderer_manifest_path": str(self.manifest_path),
                "bundle_uid": "jab_test",
                "scene_mode": "ordered",
                "strict": True,
            },
            "video": {
                "video_subject": "Asset Hub smoke",
                "video_script": "Testing Asset Hub manifest.",
                "video_aspect": "9:16",
            },
        }
        if include_selected_assets:
            spec["selectedAssets"] = [{"file": "clip-local.mp4"}]
        return spec


if __name__ == "__main__":
    unittest.main()
