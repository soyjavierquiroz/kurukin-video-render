import argparse
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import app.custom.aroll_broll_mode as aroll_broll_mode
from app.custom.aroll_broll_mode import (
    AROLL_BROLL_QUEUE_GUARD,
    RENDER_MODE_AROLL_BROLL,
    build_aroll_broll_queue_payload,
    build_default_aroll_broll_config,
)
from app.custom.kurukin_job_queue import (
    enqueue_moneyprinter_payload,
    is_aroll_broll_queue_enabled,
    is_aroll_broll_renderer_enabled,
)


def load_nightly_runner_module():
    spec = importlib.util.spec_from_file_location(
        "nightly_runner_for_aroll_broll_queue_tests",
        Path("scripts/nightly_runner.py"),
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class TestArollBrollQueueIntegration(unittest.TestCase):
    def _valid_config(self, root: Path) -> dict:
        video = root / "storage" / "local_videos" / "presenter.mp4"
        video.parent.mkdir(parents=True)
        video.write_bytes(b"video")

        asset_root = root / "data" / "job-assets"
        manifest = (
            asset_root
            / "jab_queue_test"
            / "manifests"
            / "renderer-manifest.json"
        )
        manifest.parent.mkdir(parents=True)
        manifest.write_text('{"assets": []}', encoding="utf-8")

        config = build_default_aroll_broll_config()
        config["a_roll"]["path"] = "storage/local_videos/presenter.mp4"
        config["b_roll"]["bundle_uid"] = "jab_queue_test"
        return config

    def _valid_payload(self, root: Path) -> dict:
        config = self._valid_config(root)
        asset_root = root / "data" / "job-assets"
        with mock.patch.object(
            aroll_broll_mode,
            "DEFAULT_ASSET_HUB_JOB_ASSETS_DIR",
            asset_root,
        ):
            return build_aroll_broll_queue_payload(
                config,
                job_id="aroll-broll-queue-001",
                project_root=root,
                title="Presenter queue test",
            )

    def test_queue_flag_false_by_default(self):
        self.assertFalse(is_aroll_broll_queue_enabled({}))

    def test_queue_flag_true_with_env(self):
        for value in ("1", "true", "TRUE", "yes", "on"):
            with self.subTest(value=value):
                self.assertTrue(
                    is_aroll_broll_queue_enabled(
                        {"KURUKIN_ENABLE_AROLL_BROLL_QUEUE": value}
                    )
                )

    def test_renderer_flag_false_by_default(self):
        self.assertFalse(is_aroll_broll_renderer_enabled({}))

    def test_build_pending_job_marks_aroll_broll_render_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._valid_payload(Path(tmp))

        self.assertEqual(payload["render_mode"], RENDER_MODE_AROLL_BROLL)
        self.assertEqual(payload["runner"]["render_mode"], RENDER_MODE_AROLL_BROLL)

    def test_pending_job_includes_runner_execution_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._valid_payload(Path(tmp))

        self.assertEqual(payload["runner"]["execution_guard"], AROLL_BROLL_QUEUE_GUARD)

    def test_enqueue_aroll_broll_writes_pending_only_in_tempfile(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue_dir = root / "pending"
            payload = self._valid_payload(root)
            pending_path = enqueue_moneyprinter_payload(
                payload,
                queue_dir=queue_dir,
                now=datetime(2026, 7, 8, 12, 0, 0, tzinfo=timezone.utc),
            )

            written = json.loads(pending_path.read_text(encoding="utf-8"))

        self.assertEqual(written["render_mode"], RENDER_MODE_AROLL_BROLL)
        self.assertIn("pending", pending_path.parts)

    def test_enqueue_build_fails_when_strict_validation_fails(self):
        config = build_default_aroll_broll_config()

        with self.assertRaises(ValueError) as raised:
            build_aroll_broll_queue_payload(
                config,
                job_id="aroll-broll-invalid",
                strict=True,
            )

        self.assertIn("A-roll/B-roll config is not valid", str(raised.exception))

    def test_ui_queue_flag_off_keeps_button_disabled_and_creates_no_pending(self):
        try:
            from streamlit.testing.v1 import AppTest
        except ModuleNotFoundError:
            self.skipTest("streamlit is not installed in this Python environment")

        original_queue_flag = os.environ.pop("KURUKIN_ENABLE_AROLL_BROLL_QUEUE", None)
        original_runner_flag = os.environ.pop("KURUKIN_ENABLE_UI_RUNNER", None)
        original_cwd = Path.cwd()
        page_path = original_cwd / "webui/pages/Kurukin_Render_Console.py"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                os.chdir(tmp)
                at = AppTest.from_file(str(page_path))
                at.run(timeout=30)
                at.selectbox(key="video_type_label").set_value("Presentador + B-roll")
                at.run(timeout=30)
                at.button(key="aroll_broll_validate").click()
                at.run(timeout=30)
                pending_dir_exists = (
                    tmp_path / "storage" / "nightly_jobs" / "pending"
                ).exists()
        finally:
            os.chdir(original_cwd)
            if original_queue_flag is not None:
                os.environ["KURUKIN_ENABLE_AROLL_BROLL_QUEUE"] = original_queue_flag
            if original_runner_flag is not None:
                os.environ["KURUKIN_ENABLE_UI_RUNNER"] = original_runner_flag

        rendered_text = "\n".join(
            str(getattr(item, "value", getattr(item, "label", item)))
            for collection in (
                at.info,
                at.warning,
                at.caption,
                at.button,
            )
            for item in collection
        )
        self.assertEqual(len(at.exception), 0)
        self.assertIn("Cola A-roll/B-roll: protegida", rendered_text)
        self.assertIn("KURUKIN_ENABLE_AROLL_BROLL_QUEUE=<unset>", rendered_text)
        self.assertFalse(pending_dir_exists)
        self.assertTrue(at.button(key="aroll_broll_enqueue_disabled").disabled)

    def test_runner_guard_rejects_aroll_broll_with_renderer_flag_off(self):
        runner = load_nightly_runner_module()

        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(runner.RunnerError) as raised:
                runner.validate_job(
                    {
                        "job_id": "aroll-broll-001",
                        "video_subject": "Presenter edit",
                        "video_aspect": "9:16",
                        "render_mode": RENDER_MODE_AROLL_BROLL,
                        "aroll_broll": {},
                        "runner": {"job_id": "aroll-broll-001"},
                    }
                )

        self.assertEqual(
            str(raised.exception),
            "A-roll/B-roll renderer execution is disabled",
        )

    def test_runner_guard_does_not_call_api_with_renderer_flag_off(self):
        runner = load_nightly_runner_module()

        with tempfile.TemporaryDirectory() as tmp:
            queue_dir = Path(tmp) / "queue"
            paths = runner.ensure_queue_dirs(queue_dir)
            pending_file = paths["pending"] / "20260708-120000-aroll-broll.json"
            pending_file.write_text(
                json.dumps(
                    {
                        "job_id": "aroll-broll-001",
                        "video_subject": "Presenter edit",
                        "video_aspect": "9:16",
                        "render_mode": RENDER_MODE_AROLL_BROLL,
                        "aroll_broll": {},
                        "runner": {"job_id": "aroll-broll-001"},
                    }
                ),
                encoding="utf-8",
            )
            logger = runner.Logger(paths["logs"] / "test.log")
            calls = []

            def fake_api_json(*args, **kwargs):
                calls.append((args, kwargs))
                raise AssertionError("api_json must not be called")

            args = argparse.Namespace(
                api_base_url="http://example.invalid/api/v1",
                dry_run=False,
                poll_seconds=1,
                task_timeout_seconds=1,
                no_progress_timeout_seconds=1,
            )
            with mock.patch.dict(os.environ, {}, clear=True):
                with mock.patch.object(runner, "api_json", fake_api_json):
                    failed_path = runner.process_one_job(pending_file, paths, args, logger)

            error_payload = json.loads(
                (failed_path / "error.json").read_text(encoding="utf-8")
            )

        self.assertEqual(calls, [])
        self.assertEqual(
            error_payload["error"],
            "A-roll/B-roll renderer execution is disabled",
        )

    def test_normal_runner_validation_flow_is_unchanged(self):
        runner = load_nightly_runner_module()

        payload = runner.validate_job(
            {
                "job_id": "normal-001",
                "video_subject": "Normal job",
                "video_aspect": "9:16",
                "video_source": "local",
                "video_materials": ["storage/local_videos/example.mp4"],
                "runner": {"job_id": "normal-001"},
            }
        )

        self.assertEqual(payload["video_subject"], "Normal job")
        self.assertNotIn("runner", payload)
        self.assertNotIn("job_id", payload)


if __name__ == "__main__":
    unittest.main()
