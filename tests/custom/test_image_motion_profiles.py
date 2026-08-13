import contextlib
import io
import json
import sys
import tempfile
import unittest
from enum import Enum
from pathlib import Path
from types import ModuleType, SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_INSERTED_NUMPY_STUB = False
_NUMPY_STUB = None


class _Logger:
    def info(self, *args, **kwargs):
        pass

    def success(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass

    def debug(self, *args, **kwargs):
        pass


class _FakeImage:
    size = (600, 800)
    mode = "RGB"

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def load(self):
        return None

    def getdata(self):
        return []

    def save(self, path):
        Path(path).write_text("fake", encoding="utf-8")


class _FakeImageModule(ModuleType):
    def open(self, path):
        return _FakeImage()

    def new(self, mode, size, color=None):
        image = _FakeImage()
        image.size = size
        return image


class _FakeClip:
    def __init__(self, source=None, size=(600, 800), transparent=False):
        self.source = source
        self.size = size
        self.w, self.h = size
        self.duration = 0
        self.audio = None
        self.mask = None
        self.clips = []
        self.reader = None

    def with_duration(self, duration):
        self.duration = duration
        return self

    def resized(self, *args, **kwargs):
        new_size = kwargs.get("new_size")
        if new_size is not None:
            self.size = new_size
            self.w, self.h = new_size
        return self

    def with_position(self, position):
        self.position = position
        return self


class _FakeComposite(_FakeClip):
    def __init__(self, clips, size=None, **kwargs):
        super().__init__(size=size or (600, 800))
        self.clips = clips


def _install_video_import_stubs() -> None:
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
    moviepy.AudioFileClip = _FakeClip
    moviepy.ColorClip = _FakeClip
    moviepy.CompositeAudioClip = _FakeComposite
    moviepy.CompositeVideoClip = _FakeComposite
    moviepy.ImageClip = _FakeClip
    moviepy.TextClip = _FakeClip
    moviepy.VideoFileClip = _FakeClip
    moviepy.Clip = _FakeClip
    moviepy.afx = SimpleNamespace()
    moviepy.vfx = SimpleNamespace()
    sys.modules.setdefault("moviepy", moviepy)

    subtitles_module = ModuleType("moviepy.video.tools.subtitles")
    subtitles_module.SubtitlesClip = _FakeClip
    sys.modules.setdefault("moviepy.video", ModuleType("moviepy.video"))
    sys.modules.setdefault("moviepy.video.tools", ModuleType("moviepy.video.tools"))
    sys.modules.setdefault("moviepy.video.tools.subtitles", subtitles_module)

    pil_module = ModuleType("PIL")
    image_module = _FakeImageModule("PIL.Image")
    image_draw_module = ModuleType("PIL.ImageDraw")
    image_font_module = ModuleType("PIL.ImageFont")
    image_font_module.truetype = lambda *args, **kwargs: object()
    pil_module.Image = image_module
    pil_module.ImageDraw = image_draw_module
    pil_module.ImageFont = image_font_module
    sys.modules.setdefault("PIL", pil_module)
    sys.modules.setdefault("PIL.Image", image_module)
    sys.modules.setdefault("PIL.ImageDraw", image_draw_module)
    sys.modules.setdefault("PIL.ImageFont", image_font_module)

    file_security = ModuleType("app.utils.file_security")
    file_security.resolve_path_within_directory = lambda base, value: str(Path(base) / value)
    sys.modules.setdefault("app.utils.file_security", file_security)


_install_video_import_stubs()


def tearDownModule() -> None:
    if _INSERTED_NUMPY_STUB and sys.modules.get("numpy") is _NUMPY_STUB:
        del sys.modules["numpy"]

from app.services.video import (  # noqa: E402
    clamp_image_motion_intensity,
    create_image_motion_clip,
    is_image_file,
    normalize_image_motion_preset,
)
from scripts import local_job_wrapper  # noqa: E402


def make_spec(asset_files=None):
    if asset_files is None:
        asset_files = [
            {"file": "photo.png", "label": "intro", "order": 1},
            {"file": "clip.mp4", "label": "body", "order": 2},
        ]
    return {
        "job_id": "image-motion-test",
        "selectedAssets": asset_files,
        "video": {
            "video_subject": "Image motion test",
            "video_script": "Testing image motion.",
            "video_aspect": "9:16",
        },
    }


class TestImageMotionCoreHelpers(unittest.TestCase):
    def test_normalize_empty_preset_returns_none(self):
        self.assertEqual(normalize_image_motion_preset(""), "none")

    def test_normalize_zoom_in_alias(self):
        self.assertEqual(normalize_image_motion_preset("zoom_in"), "slow_zoom_in")

    def test_normalize_ken_burns_alias(self):
        self.assertEqual(normalize_image_motion_preset("ken_burns"), "slow_zoom_in")

    def test_normalize_handheld_alias(self):
        self.assertEqual(normalize_image_motion_preset("handheld"), "handheld_soft")

    def test_invalid_preset_raises_value_error(self):
        with self.assertRaisesRegex(
            ValueError,
            "Unsupported image motion preset 'crazy_spin'.",
        ):
            normalize_image_motion_preset("crazy_spin")

    def test_clamp_intensity_default(self):
        self.assertEqual(clamp_image_motion_intensity(None), 0.06)

    def test_clamp_intensity_accepts_valid_value(self):
        self.assertEqual(clamp_image_motion_intensity(0.06), 0.06)

    def test_clamp_intensity_rejects_too_large_value(self):
        with self.assertRaisesRegex(ValueError, "between 0.0 and 0.20"):
            clamp_image_motion_intensity(0.21)

    def test_clamp_intensity_rejects_negative_value(self):
        with self.assertRaisesRegex(ValueError, "between 0.0 and 0.20"):
            clamp_image_motion_intensity(-0.01)

    def test_is_image_file_detects_supported_images(self):
        self.assertTrue(is_image_file("one.jpg"))
        self.assertTrue(is_image_file("two.jpeg"))
        self.assertTrue(is_image_file("three.png"))

    def test_is_image_file_rejects_mp4(self):
        self.assertFalse(is_image_file("clip.mp4"))

    def test_create_static_image_motion_clip_has_target_size(self):
        clip = create_image_motion_clip(
            "photo.png",
            duration=1,
            target_size=(720, 1280),
            motion_preset="none",
        )
        self.assertEqual(clip.size, (720, 1280))
        self.assertEqual(clip.duration, 1)

    def test_create_zoom_image_motion_clip_has_target_size(self):
        clip = create_image_motion_clip(
            "photo.png",
            duration=1,
            target_size=(720, 1280),
            motion_preset="slow_zoom_in",
        )
        self.assertEqual(clip.size, (720, 1280))

    def test_create_pan_image_motion_clip_has_target_size(self):
        clip = create_image_motion_clip(
            "photo.png",
            duration=1,
            target_size=(720, 1280),
            motion_preset="pan_up",
        )
        self.assertEqual(clip.size, (720, 1280))


class TestImageMotionWrapper(unittest.TestCase):
    def write_spec(self, directory, spec):
        spec_path = Path(directory) / "job.json"
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        return spec_path

    def write_assets(self, directory, filenames):
        assets_dir = Path(directory) / "local_videos"
        assets_dir.mkdir()
        for filename in filenames:
            (assets_dir / filename).write_text("dummy", encoding="utf-8")
        return assets_dir

    def build_from_spec(self, spec):
        with tempfile.TemporaryDirectory() as tmp_dir:
            assets_dir = self.write_assets(
                tmp_dir,
                [asset["file"] for asset in spec["selectedAssets"]],
            )
            ordered_assets = local_job_wrapper.validate_job_spec(
                spec,
                local_videos_dir=assets_dir,
                skip_media_probe=True,
            )
            return local_job_wrapper.build_pending_job(spec, ordered_assets)

    def test_global_image_motion_enabled_generates_core_field(self):
        spec = make_spec()
        spec["image_motion"] = {"enabled": True, "preset": "slow_zoom_in"}
        payload = self.build_from_spec(spec)
        self.assertTrue(payload["image_motion_enabled"])

    def test_global_image_motion_alias_is_normalized(self):
        spec = make_spec()
        spec["image_motion"] = {"enabled": True, "preset": "ken_burns"}
        payload = self.build_from_spec(spec)
        self.assertEqual(payload["image_motion_preset"], "slow_zoom_in")

    def test_global_image_motion_original_key_is_not_root(self):
        spec = make_spec()
        spec["image_motion"] = {"enabled": True, "preset": "slow_zoom_in"}
        payload = self.build_from_spec(spec)
        self.assertNotIn("image_motion", payload)

    def test_runner_image_motion_metadata_exists(self):
        spec = make_spec()
        spec["image_motion"] = {
            "enabled": True,
            "preset": "slow_zoom_in",
            "intensity": 0.06,
        }
        payload = self.build_from_spec(spec)
        self.assertEqual(
            payload["runner"]["image_motion"],
            {"enabled": True, "preset": "slow_zoom_in", "intensity": 0.06},
        )

    def test_image_asset_motion_generates_material_motion(self):
        spec = make_spec(
            [{"file": "photo.png", "order": 1, "motion": "pan_up"}]
        )
        payload = self.build_from_spec(spec)
        self.assertEqual(payload["video_materials"][0]["motion"], "pan_up")

    def test_image_asset_motion_intensity_generates_material_intensity(self):
        spec = make_spec(
            [
                {
                    "file": "photo.png",
                    "order": 1,
                    "motion": "pan_up",
                    "motion_intensity": 0.05,
                }
            ]
        )
        payload = self.build_from_spec(spec)
        self.assertEqual(payload["video_materials"][0]["motion_intensity"], 0.05)

    def test_invalid_global_preset_fails(self):
        spec = make_spec()
        spec["image_motion"] = {"enabled": True, "preset": "crazy_spin"}
        with self.assertRaises(local_job_wrapper.LocalJobWrapperError):
            self.build_from_spec(spec)

    def test_invalid_asset_preset_fails(self):
        spec = make_spec(
            [{"file": "photo.png", "order": 1, "motion": "crazy_spin"}]
        )
        with self.assertRaises(local_job_wrapper.LocalJobWrapperError):
            self.build_from_spec(spec)

    def test_invalid_global_intensity_fails(self):
        spec = make_spec()
        spec["image_motion"] = {
            "enabled": True,
            "preset": "slow_zoom_in",
            "intensity": 0.30,
        }
        with self.assertRaises(local_job_wrapper.LocalJobWrapperError):
            self.build_from_spec(spec)

    def test_invalid_asset_intensity_fails(self):
        spec = make_spec(
            [
                {
                    "file": "photo.png",
                    "order": 1,
                    "motion": "pan_up",
                    "motion_intensity": -0.01,
                }
            ]
        )
        with self.assertRaises(local_job_wrapper.LocalJobWrapperError):
            self.build_from_spec(spec)

    def test_video_asset_with_motion_does_not_emit_core_material_motion(self):
        spec = make_spec(
            [{"file": "clip.mp4", "order": 1, "motion": "pan_up"}]
        )
        payload = self.build_from_spec(spec)
        self.assertNotIn("motion", payload["video_materials"][0])
        self.assertEqual(payload["runner"]["selectedAssets"][0]["motion"], "pan_up")

    def test_without_image_motion_does_not_add_core_fields(self):
        payload = self.build_from_spec(make_spec())
        self.assertNotIn("image_motion_enabled", payload)
        self.assertNotIn("image_motion_preset", payload)
        self.assertNotIn("image_motion_intensity", payload)

    def test_mixed_image_and_video_preserves_order(self):
        payload = self.build_from_spec(make_spec())
        self.assertEqual(
            [item["url"] for item in payload["video_materials"]],
            ["photo.png", "clip.mp4"],
        )

    def test_print_payload_with_image_motion_is_valid_json(self):
        spec = make_spec()
        spec["image_motion"] = {
            "enabled": True,
            "preset": "zoom_in",
            "intensity": 0.06,
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            assets_dir = self.write_assets(tmp_dir, ["photo.png", "clip.mp4"])
            spec_path = self.write_spec(tmp_dir, spec)
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = local_job_wrapper.main(
                    [
                        str(spec_path),
                        "--print-payload",
                        "--local-videos-dir",
                        str(assets_dir),
                        "--skip-media-probe",
                    ]
                )
            payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["image_motion_preset"], "slow_zoom_in")


if __name__ == "__main__":
    unittest.main()
