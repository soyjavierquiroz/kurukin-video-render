import contextlib
import importlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from enum import Enum
from types import ModuleType, SimpleNamespace
from pathlib import Path
from unittest.mock import patch

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


def _task_import_stubs() -> dict[str, ModuleType]:
    loguru = ModuleType("loguru")
    loguru.logger = _Logger()
    stubs = {"loguru": loguru}

    config_module = ModuleType("app.config")
    config_module.config = SimpleNamespace(
        app={"subtitle_provider": "edge"},
        ui={"subtitle_position": "bottom"},
    )
    stubs["app.config"] = config_module

    schema_module = ModuleType("app.models.schema")

    class VideoConcatMode(str, Enum):
        random = "random"
        sequential = "sequential"

    class VideoParams(SimpleNamespace):
        pass

    schema_module.VideoConcatMode = VideoConcatMode
    schema_module.VideoParams = VideoParams
    schema_module.MaterialInfo = SimpleNamespace
    stubs["app.models.schema"] = schema_module

    for module_name in ("llm", "material", "twelvelabs", "video"):
        stubs[f"app.services.{module_name}"] = ModuleType(f"app.services.{module_name}")

    subtitle_module = ModuleType("app.services.subtitle")

    def file_to_subtitles(subtitle_path):
        return Path(subtitle_path).read_text(encoding="utf-8").strip().splitlines()

    subtitle_module.file_to_subtitles = file_to_subtitles
    subtitle_module.create = lambda *args, **kwargs: None
    subtitle_module.correct = lambda *args, **kwargs: None
    stubs["app.services.subtitle"] = subtitle_module

    voice_module = ModuleType("app.services.voice")
    voice_module.create_subtitle = lambda *args, **kwargs: None
    voice_module.get_audio_duration = lambda *args, **kwargs: 1
    voice_module.parse_voice_name = lambda value: value
    voice_module.tts = lambda *args, **kwargs: object()
    stubs["app.services.voice"] = voice_module

    upload_post_module = ModuleType("app.services.upload_post")
    upload_post_module.upload_post_service = SimpleNamespace(
        is_configured=lambda: False,
        auto_upload=False,
        platforms=[],
    )
    upload_post_module.cross_post_video = lambda *args, **kwargs: {}
    stubs["app.services.upload_post"] = upload_post_module

    state_module = ModuleType("app.services.state")
    state_module.state = SimpleNamespace(update_task=lambda *args, **kwargs: None)
    stubs["app.services.state"] = state_module
    return stubs


def _load_task_service_with_scoped_stubs() -> ModuleType:
    """Load the isolated task module without leaking fake service modules."""
    import app
    import app.models as models
    import app.services as services

    stubs = _task_import_stubs()
    parent_names = {
        app: ("config",),
        models: ("schema",),
        services: ("task", "llm", "material", "twelvelabs", "video", "subtitle", "voice", "upload_post", "state"),
    }
    missing = object()
    parent_attributes = {
        parent: {name: parent.__dict__.get(name, missing) for name in names}
        for parent, names in parent_names.items()
    }
    try:
        with patch.dict(sys.modules, stubs, clear=False):
            previous_task = sys.modules.pop("app.services.task", None)
            try:
                task_module = importlib.import_module("app.services.task")
            finally:
                if previous_task is not None:
                    sys.modules["app.services.task"] = previous_task
                else:
                    sys.modules.pop("app.services.task", None)
    finally:
        for parent, names in parent_names.items():
            for name in names:
                original = parent_attributes[parent][name]
                if original is missing:
                    parent.__dict__.pop(name, None)
                else:
                    setattr(parent, name, original)
    return task_module


task_service = _load_task_service_with_scoped_stubs()
from app.utils import utils


def make_spec() -> dict:
    return {
        "job_id": "custom-audio-subtitle-contract",
        "selectedAssets": [{"file": "clip-01.mp4"}],
        "video": {
            "video_subject": "Audio propio",
            "video_script": "Placeholder de prueba.",
            "video_aspect": "9:16",
            "video_concat_mode": "sequential",
            "video_transition_mode": "None",
            "video_clip_duration": 3,
            "video_count": 1,
            "voice_name": "es-MX-DaliaNeural-Female",
            "bgm_type": "none",
            "subtitle_enabled": True,
            "subtitle_position": "bottom",
            "font_size": 60,
            "stroke_color": "#000000",
            "stroke_width": 1.5,
            "n_threads": 2,
            "paragraph_number": 1,
        },
    }


class TestCustomAudioSubtitleContract(unittest.TestCase):
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
        assets = local_job_wrapper.validate_job_spec(
            spec,
            local_videos_dir=self.videos_dir,
            skip_media_probe=True,
        )
        return local_job_wrapper.build_pending_job(
            spec,
            assets,
            local_audios_dir=self.audios_dir,
            local_subtitles_dir=self.subtitles_dir,
        )

    def test_audio_file_generates_relative_custom_audio_file(self):
        spec = make_spec()
        spec["audio"] = {"file": "audio-prueba.mp3"}

        payload = self.build_payload(spec)

        self.assertEqual(
            payload["custom_audio_file"],
            "storage/local_audios/audio-prueba.mp3",
        )
        self.assertNotIn("audio", payload)
        self.assertEqual(payload["runner"]["audio"]["file"], "audio-prueba.mp3")

    def test_whisper_defaults_disable_correction_and_enable_optimization(self):
        spec = make_spec()
        spec["subtitles"] = {"mode": "whisper"}

        payload = self.build_payload(spec)

        self.assertTrue(payload["subtitle_enabled"])
        self.assertFalse(payload["subtitle_correction_enabled"])
        self.assertTrue(payload["subtitle_optimization_enabled"])
        self.assertNotIn("subtitles", payload)
        self.assertEqual(payload["runner"]["subtitles"]["mode"], "whisper")

    def test_whisper_respects_explicit_correction_enabled(self):
        spec = make_spec()
        spec["subtitles"] = {
            "mode": "whisper",
            "correction_enabled": True,
        }

        payload = self.build_payload(spec)

        self.assertTrue(payload["subtitle_correction_enabled"])

    def test_whisper_respects_optimize_false(self):
        spec = make_spec()
        spec["subtitles"] = {"mode": "whisper", "optimize": False}

        payload = self.build_payload(spec)

        self.assertFalse(payload["subtitle_optimization_enabled"])

    def test_custom_srt_defaults_to_optimized_without_correction(self):
        spec = make_spec()
        spec["subtitles"] = {
            "mode": "custom_srt",
            "file": "audio-prueba.srt",
        }

        payload = self.build_payload(spec)

        self.assertTrue(payload["subtitle_enabled"])
        self.assertEqual(
            payload["custom_subtitle_file"],
            "storage/local_subtitles/audio-prueba.srt",
        )
        self.assertFalse(payload["subtitle_correction_enabled"])
        self.assertTrue(payload["subtitle_optimization_enabled"])

    def test_custom_srt_respects_optimize_false(self):
        spec = make_spec()
        spec["subtitles"] = {
            "mode": "custom_srt",
            "file": "audio-prueba.srt",
            "optimize": False,
        }

        payload = self.build_payload(spec)

        self.assertFalse(payload["subtitle_optimization_enabled"])

    def test_subtitles_none_disables_subtitles(self):
        spec = make_spec()
        spec["subtitles"] = {"mode": "none"}

        payload = self.build_payload(spec)

        self.assertFalse(payload["subtitle_enabled"])

    def test_audio_path_traversal_fails(self):
        spec = make_spec()
        spec["audio"] = {"file": "../audio-prueba.mp3"}

        with self.assertRaises(local_job_wrapper.LocalJobWrapperError):
            self.build_payload(spec)

    def test_subtitle_path_traversal_fails(self):
        spec = make_spec()
        spec["subtitles"] = {
            "mode": "custom_srt",
            "file": "../audio-prueba.srt",
        }

        with self.assertRaises(local_job_wrapper.LocalJobWrapperError):
            self.build_payload(spec)

    def test_invalid_audio_extension_fails(self):
        spec = make_spec()
        spec["audio"] = {"file": "audio-prueba.txt"}

        with self.assertRaises(local_job_wrapper.LocalJobWrapperError):
            self.build_payload(spec)

    def test_invalid_subtitle_extension_fails(self):
        spec = make_spec()
        spec["subtitles"] = {
            "mode": "custom_srt",
            "file": "audio-prueba.vtt",
        }

        with self.assertRaises(local_job_wrapper.LocalJobWrapperError):
            self.build_payload(spec)

    def test_audio_top_level_conflict_with_legacy_field_fails(self):
        spec = make_spec()
        spec["audio"] = {"file": "audio-prueba.mp3"}
        spec["video"]["custom_audio_file"] = "storage/local_audios/other.mp3"

        with self.assertRaises(local_job_wrapper.LocalJobWrapperError):
            self.build_payload(spec)

    def test_subtitle_top_level_conflict_with_legacy_field_fails(self):
        spec = make_spec()
        spec["subtitles"] = {
            "mode": "custom_srt",
            "file": "audio-prueba.srt",
        }
        spec["video"]["custom_subtitle_file"] = "storage/local_subtitles/other.srt"

        with self.assertRaises(local_job_wrapper.LocalJobWrapperError):
            self.build_payload(spec)

    def test_without_audio_or_subtitles_preserves_previous_payload_shape(self):
        payload = self.build_payload(make_spec())

        self.assertNotIn("custom_audio_file", payload)
        self.assertNotIn("custom_subtitle_file", payload)
        self.assertNotIn("subtitle_correction_enabled", payload)
        self.assertNotIn("subtitle_optimization_enabled", payload)
        self.assertNotIn("audio", payload["runner"])
        self.assertNotIn("subtitles", payload["runner"])

    def test_resolve_custom_subtitle_file_accepts_repo_relative_srt(self):
        subtitle_file = Path(utils.root_dir()) / "storage" / "local_subtitles" / "test.srt"
        subtitle_file.parent.mkdir(parents=True, exist_ok=True)
        subtitle_file.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nHola.\n\n",
            encoding="utf-8",
        )
        task_dir = utils.task_dir("test-custom-subtitle-resolve")
        try:
            resolved = task_service.resolve_custom_subtitle_file(
                "storage/local_subtitles/test.srt",
                task_dir,
            )
        finally:
            subtitle_file.unlink(missing_ok=True)
            shutil.rmtree(task_dir, ignore_errors=True)

        self.assertEqual(resolved, os.path.realpath(subtitle_file))

    def test_resolve_custom_subtitle_file_rejects_escape_outside_repo(self):
        task_dir = utils.task_dir("test-custom-subtitle-escape")
        with tempfile.NamedTemporaryFile(suffix=".srt") as outside_file:
            try:
                with self.assertRaises(ValueError):
                    task_service.resolve_custom_subtitle_file(
                        outside_file.name,
                        task_dir,
                    )
            finally:
                shutil.rmtree(task_dir, ignore_errors=True)

    def test_resolve_custom_subtitle_file_rejects_non_srt_extension(self):
        task_dir = utils.task_dir("test-custom-subtitle-extension")
        txt_file = Path(task_dir) / "subtitle.txt"
        txt_file.write_text("not srt", encoding="utf-8")
        try:
            with self.assertRaises(ValueError):
                task_service.resolve_custom_subtitle_file("subtitle.txt", task_dir)
        finally:
            shutil.rmtree(task_dir, ignore_errors=True)

    def test_optimize_subtitle_if_enabled_skips_when_disabled(self):
        params = SimpleNamespace(
            video_subject="test",
            subtitle_optimization_enabled=False,
        )

        with patch("app.custom.subtitle_optimizer.optimize_srt_file") as optimize:
            result = task_service.optimize_subtitle_if_enabled("/tmp/subtitle.srt", params)

        self.assertIsNone(result)
        optimize.assert_not_called()

    def test_optimize_subtitle_if_enabled_runs_when_enabled(self):
        params = SimpleNamespace(
            video_subject="test",
            subtitle_optimization_enabled=True,
            video_aspect="9:16",
        )

        with patch(
            "app.custom.subtitle_optimizer.optimize_srt_file",
            return_value={"changed": False},
        ) as optimize:
            result = task_service.optimize_subtitle_if_enabled("/tmp/subtitle.srt", params)

        self.assertEqual(result, {"changed": False})
        optimize.assert_called_once_with(
            subtitle_path="/tmp/subtitle.srt",
            aspect="9:16",
        )

    def test_custom_srt_generation_does_not_require_subtitle_provider(self):
        task_id = "test-custom-srt-no-provider"
        task_dir = utils.task_dir(task_id)
        custom_srt = Path(task_dir) / "provided.srt"
        custom_srt.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nSRT propio.\n\n",
            encoding="utf-8",
        )
        params = SimpleNamespace(
            video_subject="test",
            custom_subtitle_file="provided.srt",
            subtitle_enabled=True,
            subtitle_optimization_enabled=False,
        )

        try:
            with (
                patch.object(task_service.subtitle, "create") as create,
                patch.object(task_service.subtitle, "correct") as correct,
                patch.object(task_service.voice, "create_subtitle") as edge,
            ):
                subtitle_path = task_service.generate_subtitle(
                    task_id,
                    params,
                    video_script="Placeholder",
                    sub_maker=None,
                    audio_file="/tmp/audio.mp3",
                )
        finally:
            shutil.rmtree(task_dir, ignore_errors=True)

        self.assertTrue(subtitle_path.endswith("subtitle.srt"))
        create.assert_not_called()
        correct.assert_not_called()
        edge.assert_not_called()

    def test_print_payload_keeps_stdout_json_only_with_new_contract(self):
        spec = make_spec()
        spec["audio"] = {"file": "audio-prueba.mp3"}
        spec["subtitles"] = {"mode": "whisper"}
        spec_path = self.base_dir / "job.json"
        spec_path.write_text(json.dumps(spec), encoding="utf-8")

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = local_job_wrapper.main(
                [
                    str(spec_path),
                    "--print-payload",
                    "--local-videos-dir",
                    str(self.videos_dir),
                    "--local-audios-dir",
                    str(self.audios_dir),
                    "--local-subtitles-dir",
                    str(self.subtitles_dir),
                    "--skip-media-probe",
                ]
            )
        payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            payload["custom_audio_file"],
            "storage/local_audios/audio-prueba.mp3",
        )


if __name__ == "__main__":
    unittest.main()
