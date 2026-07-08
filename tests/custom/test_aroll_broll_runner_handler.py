import argparse
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


def load_nightly_runner_module():
    spec = importlib.util.spec_from_file_location(
        "nightly_runner_for_aroll_broll_handler_tests",
        Path("scripts/nightly_runner.py"),
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class TestArollBrollRunnerHandler(unittest.TestCase):
    def _args(self, project_root: Path) -> argparse.Namespace:
        return argparse.Namespace(
            api_base_url="http://example.invalid/api/v1",
            dry_run=False,
            poll_seconds=1,
            task_timeout_seconds=1,
            no_progress_timeout_seconds=1,
            project_root=project_root,
        )

    def _write_media(self, root: Path) -> tuple[Path, Path]:
        aroll = root / "storage" / "local_videos" / "presenter.mp4"
        broll = root / "storage" / "local_assets" / "cutaway.mp4"
        aroll.parent.mkdir(parents=True, exist_ok=True)
        broll.parent.mkdir(parents=True, exist_ok=True)
        aroll.write_bytes(b"aroll")
        broll.write_bytes(b"broll")
        return aroll, broll

    def _aroll_broll_job(self, root: Path, *, task_id: str = "task-handler-001"):
        self._write_media(root)
        return {
            "job_id": "job-handler-001",
            "task_id": task_id,
            "video_subject": "Presenter edit",
            "video_aspect": "9:16",
            "render_mode": "aroll_broll",
            "aroll_broll": {
                "render_mode": "aroll_broll",
                "a_roll": {
                    "path": "storage/local_videos/presenter.mp4",
                    "audio_policy": "original",
                },
                "b_roll": {
                    "source": "local_assets",
                    "assets": ["storage/local_assets/cutaway.mp4"],
                    "clip_seconds": 4,
                    "frequency": "medium",
                    "audio_policy": "muted",
                },
                "layout": {
                    "preset": "alternating_fullscreen",
                    "aspect_ratio": "9:16",
                },
                "subtitles": {"source": "none"},
            },
            "runner": {"job_id": "job-handler-001"},
        }

    def _write_pending(self, paths: dict[str, Path], payload: dict) -> Path:
        pending = paths["pending"] / "20260708-120000-aroll-broll.json"
        pending.write_text(json.dumps(payload), encoding="utf-8")
        return pending

    def _duration_runner(self, command, cwd, timeout):
        self.assertIsInstance(command, list)
        return {"returncode": 0, "stdout": "8.0\n", "stderr": ""}

    def test_normal_job_does_not_enter_aroll_broll_handler(self):
        runner = load_nightly_runner_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = runner.ensure_queue_dirs(root / "queue")
            pending = paths["pending"] / "20260708-120000-normal.json"
            pending.write_text(
                json.dumps(
                    {
                        "job_id": "normal-001",
                        "video_subject": "Normal job",
                        "video_aspect": "9:16",
                        "video_source": "local",
                        "video_materials": ["storage/local_videos/example.mp4"],
                    }
                ),
                encoding="utf-8",
            )
            logger = runner.Logger(paths["logs"] / "test.log")

            def fake_handler(*args, **kwargs):
                raise AssertionError("A-roll/B-roll handler must not run")

            with mock.patch.object(runner, "handle_aroll_broll_job", fake_handler):
                completed = runner.process_one_job(
                    pending,
                    paths,
                    argparse.Namespace(
                        api_base_url="http://example.invalid/api/v1",
                        dry_run=True,
                        poll_seconds=1,
                        task_timeout_seconds=1,
                        no_progress_timeout_seconds=1,
                    ),
                    logger,
                )

        self.assertEqual(completed.parent.name, "completed")

    def test_flag_off_rejects_before_api_and_renderer(self):
        runner = load_nightly_runner_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = runner.ensure_queue_dirs(root / "queue")
            pending = self._write_pending(paths, self._aroll_broll_job(root))
            logger = runner.Logger(paths["logs"] / "test.log")
            api_calls = []
            renderer_calls = []

            def fake_api_json(*args, **kwargs):
                api_calls.append((args, kwargs))
                raise AssertionError("api_json must not be called")

            def fake_renderer(plan):
                renderer_calls.append(plan)
                raise AssertionError("renderer must not be called")

            with mock.patch.dict(os.environ, {}, clear=True):
                with mock.patch.object(runner, "api_json", fake_api_json):
                    failed = runner.process_one_job(
                        pending,
                        paths,
                        self._args(root),
                        logger,
                        renderer_runner=fake_renderer,
                        duration_runner=self._duration_runner,
                    )

            error_payload = json.loads((failed / "error.json").read_text())

        self.assertEqual(failed.parent.name, "failed")
        self.assertEqual(api_calls, [])
        self.assertEqual(renderer_calls, [])
        self.assertEqual(
            error_payload["error"],
            "A-roll/B-roll renderer execution is disabled",
        )

    def test_flag_on_success_uses_fake_renderer_without_api(self):
        runner = load_nightly_runner_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = runner.ensure_queue_dirs(root / "queue")
            pending = self._write_pending(paths, self._aroll_broll_job(root))
            logger = runner.Logger(paths["logs"] / "test.log")
            api_calls = []
            renderer_plans = []

            def fake_api_json(*args, **kwargs):
                api_calls.append((args, kwargs))
                raise AssertionError("api_json must not be called")

            def fake_renderer(plan):
                renderer_plans.append(plan)
                return {
                    "ok": True,
                    "command": ["ffmpeg", "-i", plan.a_roll_path.as_posix()],
                    "output_path": plan.output_path.as_posix(),
                    "returncode": 0,
                    "stdout": "render ok",
                    "stderr": "",
                    "warnings": [],
                    "dry_run": False,
                }

            with mock.patch.dict(
                os.environ,
                {"KURUKIN_ENABLE_AROLL_BROLL_RENDERER": "1"},
                clear=True,
            ):
                with mock.patch.object(runner, "api_json", fake_api_json):
                    completed = runner.process_one_job(
                        pending,
                        paths,
                        self._args(root),
                        logger,
                        renderer_runner=fake_renderer,
                        duration_runner=self._duration_runner,
                    )

            submit = json.loads((completed / "submit-response.json").read_text())
            final_task = json.loads((completed / "final-task.json").read_text())
            render_result = json.loads((completed / "render-result.json").read_text())

        self.assertEqual(completed.parent.name, "completed")
        self.assertEqual(api_calls, [])
        self.assertEqual(len(renderer_plans), 1)
        self.assertEqual(submit["status"], 200)
        self.assertEqual(submit["message"], "success")
        self.assertEqual(submit["data"]["task_id"], "task-handler-001")
        self.assertEqual(submit["render_mode"], "aroll_broll")
        self.assertEqual(final_task["data"]["state"], 1)
        self.assertEqual(final_task["data"]["progress"], 100)
        self.assertEqual(
            final_task["data"]["videos"],
            ["/tasks/task-handler-001/final-1.mp4"],
        )
        self.assertEqual(final_task["data"]["render_mode"], "aroll_broll")
        self.assertEqual(render_result["returncode"], 0)

    def test_flag_on_failure_moves_to_failed_with_process_evidence(self):
        runner = load_nightly_runner_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = runner.ensure_queue_dirs(root / "queue")
            pending = self._write_pending(paths, self._aroll_broll_job(root))
            logger = runner.Logger(paths["logs"] / "test.log")

            def fake_renderer(plan):
                return {
                    "ok": False,
                    "command": ["ffmpeg", "-i", plan.a_roll_path.as_posix()],
                    "output_path": plan.output_path.as_posix(),
                    "returncode": 77,
                    "stdout": "partial output",
                    "stderr": "renderer failed",
                    "warnings": [],
                    "dry_run": False,
                }

            with mock.patch.dict(
                os.environ,
                {"KURUKIN_ENABLE_AROLL_BROLL_RENDERER": "1"},
                clear=True,
            ):
                failed = runner.process_one_job(
                    pending,
                    paths,
                    self._args(root),
                    logger,
                    renderer_runner=fake_renderer,
                    duration_runner=self._duration_runner,
                )

            error_payload = json.loads((failed / "error.json").read_text())
            render_result = json.loads((failed / "render-result.json").read_text())

        self.assertEqual(failed.parent.name, "failed")
        self.assertEqual(error_payload["render_mode"], "aroll_broll")
        self.assertEqual(error_payload["returncode"], 77)
        self.assertEqual(error_payload["stdout"], "partial output")
        self.assertEqual(error_payload["stderr"], "renderer failed")
        self.assertEqual(render_result["returncode"], 77)

    def test_output_path_stays_under_tempfile_storage_tasks(self):
        runner = load_nightly_runner_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job = self._aroll_broll_job(root, task_id="task-output-001")
            reserved_dir = root / "queue" / "processing" / "reserved"
            reserved_dir.mkdir(parents=True)

            with mock.patch.dict(
                os.environ,
                {"KURUKIN_ENABLE_AROLL_BROLL_RENDERER": "1"},
                clear=True,
            ):
                result = runner.handle_aroll_broll_job(
                    job,
                    reserved_dir,
                    root,
                    renderer_runner=lambda plan: {
                        "ok": True,
                        "command": ["ffmpeg"],
                        "output_path": plan.output_path.as_posix(),
                        "returncode": 0,
                        "stdout": "",
                        "stderr": "",
                    },
                    duration_runner=self._duration_runner,
                )

        self.assertEqual(
            result["output_path"],
            (root / "storage" / "tasks" / "task-output-001" / "final-1.mp4")
            .resolve(strict=False)
            .as_posix(),
        )

    def test_runner_source_does_not_use_shell_true(self):
        source = Path("scripts/nightly_runner.py").read_text(encoding="utf-8")

        self.assertNotIn("shell=True", source)


if __name__ == "__main__":
    unittest.main()
