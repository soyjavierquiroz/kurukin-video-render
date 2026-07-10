import builtins
import os
import socket
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.custom.mpt_engine_submitter import (
    MPT_ENGINE_SUBMIT_FLAG,
    build_mpt_engine_submit_plan,
    submit_mpt_engine_plan,
    summarize_mpt_engine_submit_plan,
    validate_mpt_engine_submit_plan,
)


VIDEO_PARAMS_TEST_FIELDS = {
    "video_subject",
    "video_script",
    "video_terms",
    "video_aspect",
    "video_resolution",
    "video_concat_mode",
    "video_clip_duration",
    "match_materials_to_script",
    "video_count",
    "video_source",
    "video_materials",
    "asset_hub_renderer_manifest_path",
    "asset_hub_bundle_uid",
    "asset_hub_scene_mode",
    "custom_audio_file",
    "custom_subtitle_file",
    "subtitle_provider",
    "subtitle_correction_enabled",
    "subtitle_optimization_enabled",
    "video_language",
    "voice_name",
    "voice_volume",
    "voice_rate",
    "bgm_type",
    "bgm_file",
    "bgm_volume",
    "subtitle_enabled",
}


class FakeValidationError(Exception):
    def __init__(self, errors):
        super().__init__("fake validation error")
        self._errors = errors

    def errors(self):
        return self._errors


class FakeVideoParams:
    model_fields = {field: object() for field in VIDEO_PARAMS_TEST_FIELDS}
    calls = []

    @classmethod
    def model_validate(cls, spec):
        cls.calls.append(spec)
        if spec.get("video_subject") == "invalid-secret":
            raise FakeValidationError(
                [
                    {
                        "loc": ("video_subject",),
                        "msg": "bad password=super-secret token:abc123",
                        "type": "value_error",
                    }
                ]
            )
        return cls()


def fake_schema_module():
    module = types.ModuleType("app.models.schema")
    module.VideoParams = FakeVideoParams
    return module


class TestMptEngineSubmitter(unittest.TestCase):
    def setUp(self):
        FakeVideoParams.calls = []
        os.environ.pop(MPT_ENGINE_SUBMIT_FLAG, None)

    def tearDown(self):
        os.environ.pop(MPT_ENGINE_SUBMIT_FLAG, None)

    def _with_fake_schema(self):
        return mock.patch.dict(
            sys.modules,
            {"app.models.schema": fake_schema_module()},
        )

    def test_build_plan_ok_for_normal_open_stock_job(self):
        with self._with_fake_schema():
            plan = build_mpt_engine_submit_plan(
                {
                    "job_id": "normal-open-001",
                    "video_subject": "Cafe launch",
                    "video_script": "A concise launch script.",
                    "stock_source": "pexels",
                    "video_terms": ["coffee", "barista"],
                    "asset_policy": {"mode": "open_sources"},
                }
            )

        self.assertTrue(plan["ok"], plan)
        self.assertEqual(plan["execution"], "dry_run")
        self.assertEqual(plan["validated_model"], "FakeVideoParams")
        self.assertEqual(plan["mpt_params"]["video_source"], "pexels")
        self.assertEqual(plan["submit_target"]["api_path"], "/api/v1/videos")
        self.assertEqual(
            plan["submit_target"]["service_path"], "app.services.task.start"
        )
        self.assertTrue(plan["guardrails"]["dry_run_required_by_default"])
        self.assertTrue(
            plan["guardrails"]["real_submit_requires_explicit_authorization"]
        )
        self.assertTrue(validate_mpt_engine_submit_plan(plan)["ok"])

    def test_build_plan_ok_for_aroll_broll_local_job(self):
        with self._with_fake_schema():
            plan = build_mpt_engine_submit_plan(
                {
                    "job_id": "aroll-broll-local-001",
                    "render_mode": "aroll_broll",
                    "video_subject": "Presenter edit",
                    "video_script": "Presenter transcript.",
                    "asset_policy": {"mode": "local_only"},
                    "a_roll": {
                        "path": "storage/local_videos/presenter.mp4",
                        "audio_path": "storage/local_audios/presenter.wav",
                    },
                    "b_roll": {
                        "assets": ["storage/local_assets/cutaway.mp4"],
                        "audio_policy": "muted",
                    },
                }
            )

        self.assertTrue(plan["ok"], plan)
        self.assertEqual(plan["kurukin_metadata"]["render_mode"], "aroll_broll")
        self.assertEqual(plan["mpt_params"]["video_source"], "local")
        self.assertEqual(len(plan["mpt_params"]["video_materials"]), 2)
        self.assertEqual(
            plan["kurukin_metadata"]["aroll_broll"]["support_visuals"][
                "asset_count"
            ],
            1,
        )

    def test_plan_mpt_params_are_filtered_to_video_params(self):
        with self._with_fake_schema():
            plan = build_mpt_engine_submit_plan(
                {
                    "job_id": "filtered-001",
                    "video_subject": "Filtered params",
                    "asset_policy": {"mode": "open_sources"},
                    "stock_source": "pixabay",
                    "provider_response": {"secret": "do-not-keep"},
                    "render_mode": "custom_extra_mode",
                }
            )

        self.assertTrue(plan["ok"], plan)
        self.assertEqual(set(plan["mpt_params"]).intersection(VIDEO_PARAMS_TEST_FIELDS), set(plan["mpt_params"]))
        self.assertNotIn("provider_response", plan["mpt_params"])
        self.assertNotIn("render_mode", plan["mpt_params"])

    def test_plan_preserves_kurukin_metadata(self):
        with self._with_fake_schema():
            plan = build_mpt_engine_submit_plan(
                {
                    "job_id": "metadata-001",
                    "video_subject": "Metadata",
                    "asset_policy": {"mode": "open_sources"},
                    "metadata": {"campaign": "spring"},
                    "brand_policy": {"voice": "plain"},
                }
            )

        self.assertTrue(plan["ok"], plan)
        self.assertEqual(plan["kurukin_metadata"]["job_id"], "metadata-001")
        self.assertEqual(plan["kurukin_metadata"]["metadata"]["campaign"], "spring")
        self.assertEqual(plan["kurukin_metadata"]["brand_policy"]["voice"], "plain")

    def test_submit_dry_run_does_not_call_executor(self):
        with self._with_fake_schema():
            plan = build_mpt_engine_submit_plan(
                {"video_subject": "No execution", "asset_policy": {"mode": "open_sources"}}
            )
        executor = mock.Mock(return_value={"ok": True})

        result = submit_mpt_engine_plan(plan, executor=executor, dry_run=True)

        self.assertTrue(result["ok"], result)
        self.assertFalse(result["submitted"])
        self.assertTrue(result["dry_run"])
        executor.assert_not_called()

    def test_dry_run_false_without_flag_fails_safely(self):
        with self._with_fake_schema():
            plan = build_mpt_engine_submit_plan(
                {"video_subject": "Blocked submit", "asset_policy": {"mode": "open_sources"}}
            )
        executor = mock.Mock(return_value={"ok": True})

        result = submit_mpt_engine_plan(plan, executor=executor, dry_run=False)

        self.assertFalse(result["ok"])
        self.assertFalse(result["submitted"])
        self.assertFalse(result["dry_run"])
        self.assertIn(MPT_ENGINE_SUBMIT_FLAG, result["errors"][0]["message"])
        executor.assert_not_called()

    def test_dry_run_false_with_flag_and_fake_executor_calls_fake_once(self):
        with self._with_fake_schema():
            plan = build_mpt_engine_submit_plan(
                {"video_subject": "Fake submit", "asset_policy": {"mode": "open_sources"}}
            )
        executor = mock.Mock(return_value={"conceptual_submit": True})
        os.environ[MPT_ENGINE_SUBMIT_FLAG] = "1"

        result = submit_mpt_engine_plan(plan, executor=executor, dry_run=False)

        self.assertTrue(result["ok"], result)
        self.assertTrue(result["submitted"])
        self.assertFalse(result["dry_run"])
        self.assertEqual(result["executor_result"], {"conceptual_submit": True})
        executor.assert_called_once()

    def test_fake_executor_does_not_create_pending_task_or_storage(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)

            def fake_executor(plan):
                self.assertNotIn("pending_path", plan)
                self.assertNotIn("task_id", plan)
                self.assertEqual(plan["submit_target"]["api_path"], "/api/v1/videos")
                return {"checked": True}

            with self._with_fake_schema():
                plan = build_mpt_engine_submit_plan(
                    {
                        "video_subject": "No files",
                        "asset_policy": {"mode": "open_sources"},
                    }
                )
            os.environ[MPT_ENGINE_SUBMIT_FLAG] = "1"

            result = submit_mpt_engine_plan(plan, executor=fake_executor, dry_run=False)

            self.assertTrue(result["ok"], result)
            self.assertFalse((root / "storage").exists())
            self.assertFalse((root / "pending").exists())
            self.assertFalse((root / "tasks").exists())

    def test_errors_do_not_contain_secrets(self):
        with self._with_fake_schema():
            plan = build_mpt_engine_submit_plan(
                {
                    "video_subject": "invalid-secret",
                    "asset_policy": {"mode": "open_sources"},
                }
            )

        self.assertFalse(plan["ok"])
        serialized = repr(plan["errors"])
        self.assertNotIn("super-secret", serialized)
        self.assertNotIn("abc123", serialized)
        self.assertNotIn("token", serialized.lower())
        self.assertNotIn("password", serialized.lower())

    def test_no_api_provider_download_render_or_runner_calls(self):
        real_import = builtins.__import__

        forbidden_import_prefixes = (
            "app.services",
            "requests",
            "httpx",
            "urllib.request",
            "app.custom.kurukin_job_queue",
            "app.custom.aroll_broll_renderer",
        )

        def guarded_import(name, *args, **kwargs):
            if name.startswith(forbidden_import_prefixes):
                raise AssertionError(f"forbidden import: {name}")
            return real_import(name, *args, **kwargs)

        with self._with_fake_schema(), mock.patch(
            "builtins.__import__", side_effect=guarded_import
        ), mock.patch.object(socket, "create_connection") as create_connection, mock.patch.object(
            subprocess, "run"
        ) as run:
            plan = build_mpt_engine_submit_plan(
                {
                    "video_subject": "No side effects",
                    "stock_source": "coverr",
                    "asset_policy": {"mode": "open_sources"},
                }
            )
            result = submit_mpt_engine_plan(plan, dry_run=True)

        self.assertTrue(plan["ok"], plan)
        self.assertTrue(result["ok"], result)
        create_connection.assert_not_called()
        run.assert_not_called()
        self.assertEqual(plan["submit_target"]["api_path"], "/api/v1/videos")
        self.assertEqual(plan["submit_target"]["service_path"], "app.services.task.start")

    def test_summary_reports_dry_run_plan(self):
        with self._with_fake_schema():
            plan = build_mpt_engine_submit_plan(
                {"video_subject": "Summary", "asset_policy": {"mode": "open_sources"}}
            )

        summary = summarize_mpt_engine_submit_plan(plan)

        self.assertTrue(summary["ok"], summary)
        self.assertEqual(summary["execution"], "dry_run")
        self.assertFalse(summary["submitted"])
        self.assertTrue(summary["dry_run"])
        self.assertEqual(summary["video_subject"], "Summary")


if __name__ == "__main__":
    unittest.main()
