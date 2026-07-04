import sys
import tempfile
import unittest
from enum import Enum
from pathlib import Path
from types import ModuleType, SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

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

    def success(self, *args, **kwargs):
        pass


def _install_task_import_stubs() -> None:
    loguru = ModuleType("loguru")
    loguru.logger = _Logger()
    sys.modules.setdefault("loguru", loguru)

    config_module = ModuleType("app.config")
    config_module.config = SimpleNamespace(
        app={"subtitle_provider": "edge"},
        ui={"subtitle_position": "bottom"},
    )
    sys.modules.setdefault("app.config", config_module)

    schema_module = ModuleType("app.models.schema")

    class VideoConcatMode(str, Enum):
        random = "random"
        sequential = "sequential"

    class VideoParams(SimpleNamespace):
        pass

    schema_module.VideoConcatMode = VideoConcatMode
    schema_module.VideoParams = VideoParams
    sys.modules.setdefault("app.models.schema", schema_module)

    for module_name in ("llm", "material", "twelvelabs", "video"):
        sys.modules.setdefault(
            f"app.services.{module_name}", ModuleType(f"app.services.{module_name}")
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


_install_task_import_stubs()

from app.services import task as task_service


def make_spec() -> dict:
    return {
        "job_id": "subtitle-provider-per-job",
        "selectedAssets": [{"file": "clip-01.mp4"}],
        "video": {
            "video_subject": "Provider por job",
            "video_script": "Placeholder de prueba.",
            "video_aspect": "9:16",
            "video_concat_mode": "sequential",
            "video_transition_mode": "None",
            "video_clip_duration": 3,
            "video_count": 1,
            "voice_name": "es-MX-DaliaNeural-Female",
            "bgm_type": "none",
            "subtitle_enabled": True,
            "n_threads": 2,
            "paragraph_number": 1,
        },
    }


class TestSubtitleProviderPerJob(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tmp.name)
        self.original_project_root = local_job_wrapper.PROJECT_ROOT
        local_job_wrapper.PROJECT_ROOT = self.base_dir
        self.videos_dir = self.base_dir / "storage" / "local_videos"
        self.audios_dir = self.base_dir / "storage" / "local_audios"
        self.subtitles_dir = self.base_dir / "storage" / "local_subtitles"
        self.videos_dir.mkdir(parents=True)
        self.audios_dir.mkdir(parents=True)
        self.subtitles_dir.mkdir(parents=True)
        (self.videos_dir / "clip-01.mp4").write_text("dummy", encoding="utf-8")
        (self.audios_dir / "audio-prueba.mp3").write_text("dummy", encoding="utf-8")
        (self.subtitles_dir / "audio-prueba.srt").write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nHola mundo.\n\n",
            encoding="utf-8",
        )

    def tearDown(self):
        local_job_wrapper.PROJECT_ROOT = self.original_project_root
        self.tmp.cleanup()

    def build_payload(self, spec: dict) -> dict:
        return local_job_wrapper.build_pending_job(
            spec,
            [{"file": "clip-01.mp4"}],
            local_audios_dir=self.audios_dir,
            local_subtitles_dir=self.subtitles_dir,
        )

    def test_core_uses_config_fallback_when_request_field_is_missing(self):
        original = task_service.config.app.get("subtitle_provider")
        task_service.config.app["subtitle_provider"] = "whisper"
        try:
            provider = task_service.resolve_subtitle_provider(SimpleNamespace())
        finally:
            task_service.config.app["subtitle_provider"] = original

        self.assertEqual(provider, "whisper")

    def test_core_accepts_whisper_request_provider(self):
        provider = task_service.resolve_subtitle_provider(
            SimpleNamespace(subtitle_provider="whisper")
        )

        self.assertEqual(provider, "whisper")

    def test_core_accepts_edge_request_provider(self):
        provider = task_service.resolve_subtitle_provider(
            SimpleNamespace(subtitle_provider="edge")
        )

        self.assertEqual(provider, "edge")

    def test_core_normalizes_request_provider(self):
        provider = task_service.resolve_subtitle_provider(
            SimpleNamespace(subtitle_provider="  WHISPER  ")
        )

        self.assertEqual(provider, "whisper")

    def test_core_rejects_invalid_provider(self):
        with self.assertRaisesRegex(
            ValueError,
            "Unsupported subtitle_provider 'azure'. Expected 'edge' or 'whisper'.",
        ):
            task_service.resolve_subtitle_provider(
                SimpleNamespace(subtitle_provider="azure")
            )

    def test_wrapper_mode_whisper_sets_provider(self):
        spec = make_spec()
        spec["subtitles"] = {"mode": "whisper"}

        payload = self.build_payload(spec)

        self.assertEqual(payload["subtitle_provider"], "whisper")

    def test_wrapper_provider_whisper_without_mode_sets_provider(self):
        spec = make_spec()
        spec["subtitles"] = {"provider": "whisper"}

        payload = self.build_payload(spec)

        self.assertEqual(payload["subtitle_provider"], "whisper")

    def test_wrapper_mode_edge_sets_provider(self):
        spec = make_spec()
        spec["subtitles"] = {"mode": "edge"}

        payload = self.build_payload(spec)

        self.assertEqual(payload["subtitle_provider"], "edge")

    def test_wrapper_mode_and_provider_conflict_fails(self):
        spec = make_spec()
        spec["subtitles"] = {"mode": "edge", "provider": "whisper"}

        with self.assertRaisesRegex(
            local_job_wrapper.LocalJobWrapperError,
            "conflicts with subtitles.provider",
        ):
            self.build_payload(spec)

    def test_wrapper_preserves_legacy_video_provider(self):
        spec = make_spec()
        spec["video"]["subtitle_provider"] = "whisper"

        payload = self.build_payload(spec)

        self.assertEqual(payload["subtitle_provider"], "whisper")

    def test_wrapper_legacy_provider_conflict_fails(self):
        spec = make_spec()
        spec["video"]["subtitle_provider"] = "edge"
        spec["subtitles"] = {"mode": "whisper"}

        with self.assertRaisesRegex(
            local_job_wrapper.LocalJobWrapperError,
            "conflicts with video.subtitle_provider",
        ):
            self.build_payload(spec)

    def test_wrapper_custom_audio_with_edge_fails(self):
        spec = make_spec()
        spec["audio"] = {"file": "audio-prueba.mp3"}
        spec["subtitles"] = {"mode": "edge"}

        with self.assertRaisesRegex(
            local_job_wrapper.LocalJobWrapperError,
            "Edge subtitles require generated TTS audio",
        ):
            self.build_payload(spec)

    def test_wrapper_custom_audio_with_whisper_sets_audio_and_provider(self):
        spec = make_spec()
        spec["audio"] = {"file": "audio-prueba.mp3"}
        spec["subtitles"] = {"mode": "whisper"}

        payload = self.build_payload(spec)

        self.assertEqual(
            payload["custom_audio_file"],
            "storage/local_audios/audio-prueba.mp3",
        )
        self.assertEqual(payload["subtitle_provider"], "whisper")

    def test_wrapper_custom_srt_does_not_require_provider(self):
        spec = make_spec()
        spec["subtitles"] = {
            "mode": "custom_srt",
            "file": "audio-prueba.srt",
        }

        payload = self.build_payload(spec)

        self.assertEqual(
            payload["custom_subtitle_file"],
            "storage/local_subtitles/audio-prueba.srt",
        )
        self.assertNotIn("subtitle_provider", payload)

    def test_wrapper_mode_none_does_not_generate_provider(self):
        spec = make_spec()
        spec["subtitles"] = {"mode": "none"}

        payload = self.build_payload(spec)

        self.assertFalse(payload["subtitle_enabled"])
        self.assertNotIn("subtitle_provider", payload)

    def test_wrapper_without_subtitles_does_not_add_provider(self):
        payload = self.build_payload(make_spec())

        self.assertNotIn("subtitle_provider", payload)

    def test_wrapper_invalid_provider_fails(self):
        spec = make_spec()
        spec["subtitles"] = {"provider": "azure"}

        with self.assertRaisesRegex(
            local_job_wrapper.LocalJobWrapperError,
            "subtitles.provider must be one of: edge, whisper",
        ):
            self.build_payload(spec)


if __name__ == "__main__":
    unittest.main()
