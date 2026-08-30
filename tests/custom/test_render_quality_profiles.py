import sys
import unittest
from enum import Enum
from types import ModuleType, SimpleNamespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_INSERTED_NUMPY_STUB = False
_NUMPY_STUB = None

from scripts import local_job_wrapper


class _Logger:
    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass

    def debug(self, *args, **kwargs):
        pass


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
    for name in (
        "AudioFileClip",
        "ColorClip",
        "CompositeAudioClip",
        "CompositeVideoClip",
        "ImageClip",
        "TextClip",
        "VideoFileClip",
    ):
        setattr(moviepy, name, object)
    moviepy.afx = SimpleNamespace()
    sys.modules.setdefault("moviepy", moviepy)

    subtitles_module = ModuleType("moviepy.video.tools.subtitles")
    subtitles_module.SubtitlesClip = object
    sys.modules.setdefault("moviepy.video", ModuleType("moviepy.video"))
    sys.modules.setdefault("moviepy.video.tools", ModuleType("moviepy.video.tools"))
    sys.modules.setdefault("moviepy.video.tools.subtitles", subtitles_module)

    pil_module = ModuleType("PIL")
    sys.modules.setdefault("PIL", pil_module)
    sys.modules.setdefault("PIL.Image", ModuleType("PIL.Image"))
    sys.modules.setdefault("PIL.ImageDraw", ModuleType("PIL.ImageDraw"))
    sys.modules.setdefault("PIL.ImageFont", ModuleType("PIL.ImageFont"))

    sys.modules.setdefault("app.models.const", ModuleType("app.models.const"))
    sys.modules.setdefault(
        "app.services.utils.video_effects",
        ModuleType("app.services.utils.video_effects"),
    )
    file_security = ModuleType("app.utils.file_security")
    file_security.resolve_path_within_directory = lambda base, value: value
    sys.modules.setdefault("app.utils.file_security", file_security)


_install_video_import_stubs()


def tearDownModule() -> None:
    if _INSERTED_NUMPY_STUB and sys.modules.get("numpy") is _NUMPY_STUB:
        del sys.modules["numpy"]

from app.services.video import resolve_video_size


def make_spec():
    return {
        "job_id": "render-quality-test",
        "selectedAssets": [{"file": "clip-01.mp4"}],
        "video": {
            "video_subject": "Render quality test",
            "video_script": "Testing render quality profiles.",
            "video_aspect": "9:16",
        },
    }


class TestRenderQualityProfiles(unittest.TestCase):
    def build_pending_job(self, spec):
        return local_job_wrapper.build_pending_job(
            spec,
            [{"file": "clip-01.mp4"}],
        )

    def test_resolve_video_size_defaults_to_vertical_1080p(self):
        self.assertEqual(resolve_video_size("9:16", ""), (1080, 1920))

    def test_resolve_video_size_accepts_vertical_standard(self):
        self.assertEqual(resolve_video_size("9:16", "standard_1080p"), (1080, 1920))

    def test_resolve_video_size_accepts_vertical_720p_alias(self):
        self.assertEqual(resolve_video_size("9:16", "720p"), (720, 1280))

    def test_resolve_video_size_accepts_vertical_draft(self):
        self.assertEqual(resolve_video_size("9:16", "draft_720p"), (720, 1280))

    def test_resolve_video_size_accepts_vertical_2k_alias(self):
        self.assertEqual(resolve_video_size("9:16", "2k"), (1440, 2560))

    def test_resolve_video_size_accepts_vertical_premium(self):
        self.assertEqual(resolve_video_size("9:16", "premium_2k"), (1440, 2560))

    def test_resolve_video_size_defaults_to_landscape_1080p(self):
        self.assertEqual(resolve_video_size("16:9", ""), (1920, 1080))

    def test_resolve_video_size_accepts_landscape_720p_alias(self):
        self.assertEqual(resolve_video_size("16:9", "720p"), (1280, 720))

    def test_resolve_video_size_accepts_landscape_2k_alias(self):
        self.assertEqual(resolve_video_size("16:9", "2k"), (2560, 1440))

    def test_resolve_video_size_normalizes_case_and_spaces(self):
        self.assertEqual(resolve_video_size("9:16", "  PREMIUM_2K  "), (1440, 2560))

    def test_resolve_video_size_rejects_invalid_quality(self):
        with self.assertRaisesRegex(
            ValueError,
            "Unsupported video_resolution 'bad'. Expected 720p, 1080p, or 2k.",
        ):
            resolve_video_size("9:16", "bad")

    def test_wrapper_render_quality_maps_to_video_resolution(self):
        spec = make_spec()
        spec["render_quality"] = "standard_1080p"

        pending_job = self.build_pending_job(spec)

        self.assertEqual(pending_job["video_resolution"], "standard_1080p")

    def test_wrapper_render_quality_alias_maps_to_canonical_video_resolution(self):
        spec = make_spec()
        spec["render_quality"] = "2k"

        pending_job = self.build_pending_job(spec)

        self.assertEqual(pending_job["video_resolution"], "premium_2k")

    def test_wrapper_does_not_emit_root_render_quality(self):
        spec = make_spec()
        spec["render_quality"] = "standard_1080p"

        pending_job = self.build_pending_job(spec)

        self.assertNotIn("render_quality", pending_job)

    def test_wrapper_preserves_render_quality_metadata(self):
        spec = make_spec()
        spec["render_quality"] = "standard_1080p"

        pending_job = self.build_pending_job(spec)

        self.assertEqual(pending_job["runner"]["render_quality"], "standard_1080p")

    def test_wrapper_preserves_legacy_video_resolution(self):
        spec = make_spec()
        spec["video"]["video_resolution"] = "premium_2k"

        pending_job = self.build_pending_job(spec)

        self.assertEqual(pending_job["video_resolution"], "premium_2k")

    def test_wrapper_allows_equivalent_render_quality_and_legacy_video_resolution(self):
        spec = make_spec()
        spec["render_quality"] = "2k"
        spec["video"]["video_resolution"] = "premium_2k"

        pending_job = self.build_pending_job(spec)

        self.assertEqual(pending_job["video_resolution"], "premium_2k")
        self.assertEqual(pending_job["runner"]["render_quality"], "premium_2k")

    def test_wrapper_rejects_conflicting_render_quality_and_video_resolution(self):
        spec = make_spec()
        spec["render_quality"] = "720p"
        spec["video"]["video_resolution"] = "2k"

        with self.assertRaisesRegex(
            local_job_wrapper.LocalJobWrapperError,
            "render_quality conflicts with video.video_resolution",
        ):
            self.build_pending_job(spec)

    def test_wrapper_rejects_invalid_render_quality(self):
        spec = make_spec()
        spec["render_quality"] = "cinema"

        with self.assertRaisesRegex(
            local_job_wrapper.LocalJobWrapperError,
            "render_quality must be one of: 720p, 1080p, 2k",
        ):
            self.build_pending_job(spec)

    def test_wrapper_without_render_quality_preserves_previous_payload(self):
        spec = make_spec()

        pending_job = self.build_pending_job(spec)

        self.assertNotIn("video_resolution", pending_job)
        self.assertNotIn("render_quality", pending_job["runner"])


if __name__ == "__main__":
    unittest.main()
