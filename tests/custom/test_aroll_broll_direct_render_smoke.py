import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts import aroll_broll_direct_render_smoke as smoke


class TestArollBrollDirectRenderSmoke(unittest.TestCase):
    def _make_project_files(self, root: Path) -> tuple[Path, Path]:
        aroll = root / "storage" / "local_videos" / "presenter.mp4"
        broll = root / "storage" / "local_assets" / "cutaway.mp4"
        for path in (aroll, broll):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"media")
        return aroll, broll

    def _argv(self, root: Path, aroll: Path, broll: Path, *extra: str) -> list[str]:
        return [
            "--a-roll",
            aroll.as_posix(),
            "--b-roll",
            broll.as_posix(),
            "--task-id",
            "aroll-broll-direct-smoke-001",
            "--project-root",
            root.as_posix(),
            *extra,
        ]

    def _execute_argv(self, root: Path, aroll: Path, broll: Path) -> list[str]:
        return self._argv(
            root,
            aroll,
            broll,
            "--a-roll-duration-seconds",
            "6",
            "--execute",
        )

    def _run_main(self, argv: list[str], *, runner=None) -> tuple[int, str, str]:
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = smoke.main(argv, runner=runner)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_dry_run_does_not_execute_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            aroll, broll = self._make_project_files(root)

            def runner(*args, **kwargs):
                raise AssertionError("runner should not execute in dry-run")

            code, stdout, stderr = self._run_main(
                self._argv(root, aroll, broll, "--dry-run"),
                runner=runner,
            )

        self.assertEqual(code, 0, stderr)
        payload = json.loads(stdout)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["dry_run"])

    def test_dry_run_does_not_create_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            aroll, broll = self._make_project_files(root)
            output = root / "storage" / "tasks" / "aroll-broll-direct-smoke-001"

            code, _, stderr = self._run_main(self._argv(root, aroll, broll))

            self.assertEqual(code, 0, stderr)
            self.assertFalse(output.exists())

    def test_dry_run_payload_includes_output_path_and_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            aroll, broll = self._make_project_files(root)

            code, stdout, stderr = self._run_main(self._argv(root, aroll, broll))

        self.assertEqual(code, 0, stderr)
        payload = json.loads(stdout)
        self.assertTrue(payload["output_path"].endswith("/final-1.mp4"))
        self.assertIsInstance(payload["command"], list)

    def test_dry_run_payload_includes_planned_aroll_duration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            aroll, broll = self._make_project_files(root)

            code, stdout, stderr = self._run_main(self._argv(root, aroll, broll))

        self.assertEqual(code, 0, stderr)
        payload = json.loads(stdout)
        self.assertEqual(payload["a_roll_duration_seconds"], 6.0)
        self.assertLessEqual(payload["timeline_duration_seconds"], 6.0)

    def test_dry_run_accepts_multiple_broll_arguments_and_reports_rotation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            aroll, first = self._make_project_files(root)
            second = root / "storage" / "local_assets" / "second.mp4"
            third = root / "storage" / "local_assets" / "third.mp4"
            second.write_bytes(b"media")
            third.write_bytes(b"media")
            argv = [
                "--a-roll",
                aroll.as_posix(),
                "--b-roll",
                first.as_posix(),
                "--b-roll",
                second.as_posix(),
                "--b-roll",
                third.as_posix(),
                "--task-id",
                "aroll-broll-direct-smoke-multiple",
                "--project-root",
                root.as_posix(),
                "--a-roll-duration-seconds",
                "50",
            ]

            code, stdout, stderr = self._run_main(argv)
            output_parent = (
                root
                / "storage"
                / "tasks"
                / "aroll-broll-direct-smoke-multiple"
            )
            output_parent_exists = output_parent.exists()

        self.assertEqual(code, 0, stderr)
        payload = json.loads(stdout)
        self.assertEqual(payload["b_roll_asset_count"], 3)
        self.assertEqual(
            payload["b_roll_assets"],
            [first.as_posix(), second.as_posix(), third.as_posix()],
        )
        broll_segments = [
            item for item in payload["timeline"] if item["visual"] == "b_roll"
        ]
        self.assertEqual(
            [item["broll_index"] for item in broll_segments[:4]],
            [0, 1, 2, 0],
        )
        self.assertFalse(output_parent_exists)

    def test_dry_run_command_contains_duration_limit_without_real_ffmpeg(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            aroll, broll = self._make_project_files(root)

            def runner(*args, **kwargs):
                raise AssertionError("runner should not execute in dry-run")

            code, stdout, stderr = self._run_main(
                self._argv(root, aroll, broll),
                runner=runner,
            )

        self.assertEqual(code, 0, stderr)
        payload = json.loads(stdout)
        command = payload["command"]
        self.assertIn("-t", command)
        self.assertEqual(command[command.index("-t") + 1], "6")
        self.assertLess(command.index("-t"), command.index(payload["output_path"]))

    def test_command_is_list_of_strings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            aroll, broll = self._make_project_files(root)

            code, stdout, stderr = self._run_main(self._argv(root, aroll, broll))

        self.assertEqual(code, 0, stderr)
        command = json.loads(stdout)["command"]
        self.assertIsInstance(command, list)
        self.assertTrue(all(isinstance(item, str) for item in command))

    def test_execute_requires_env_to_execute(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            aroll, broll = self._make_project_files(root)
            calls = []

            def runner(command, cwd, timeout):
                calls.append((command, cwd, timeout))
                return {"returncode": 0, "stdout": "ok", "stderr": ""}

            with mock.patch.dict(
                os.environ,
                {smoke.DIRECT_RENDER_ENV: "1"},
                clear=True,
            ):
                code, stdout, stderr = self._run_main(
                    self._execute_argv(root, aroll, broll),
                    runner=runner,
                )

        self.assertEqual(code, 0, stderr)
        self.assertEqual(len(calls), 1)
        self.assertFalse(json.loads(stdout)["dry_run"])

    def test_execute_fake_runner_failure_includes_stderr_and_returncode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            aroll, broll = self._make_project_files(root)

            def runner(command, cwd, timeout):
                return {"returncode": 1, "stdout": "partial", "stderr": "ffmpeg failed"}

            with mock.patch.dict(
                os.environ,
                {smoke.DIRECT_RENDER_ENV: "1"},
                clear=True,
            ):
                code, stdout, stderr = self._run_main(
                    self._execute_argv(root, aroll, broll),
                    runner=runner,
                )

        self.assertEqual(code, 0, stderr)
        payload = json.loads(stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["returncode"], 1)
        self.assertEqual(payload["stdout"], "partial")
        self.assertEqual(payload["stderr"], "ffmpeg failed")

    def test_execute_without_env_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            aroll, broll = self._make_project_files(root)
            calls = []

            def runner(command, cwd, timeout):
                calls.append((command, cwd, timeout))
                return {"returncode": 0, "stdout": "ok", "stderr": ""}

            with mock.patch.dict(os.environ, {}, clear=True):
                code, stdout, stderr = self._run_main(
                    self._execute_argv(root, aroll, broll),
                    runner=runner,
                )

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(calls, [])
        self.assertIn(
            "Direct A-roll/B-roll render execution is disabled",
            stderr,
        )

    def test_output_path_stays_under_storage_tasks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            aroll, broll = self._make_project_files(root)

            code, stdout, stderr = self._run_main(self._argv(root, aroll, broll))

        self.assertEqual(code, 0, stderr)
        output_path = Path(json.loads(stdout)["output_path"])
        self.assertEqual(
            output_path,
            root / "storage" / "tasks" / "aroll-broll-direct-smoke-001" / "final-1.mp4",
        )

    def test_rejects_path_outside_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            aroll, broll = self._make_project_files(root)
            outside = root / "outside.mp4"
            outside.write_bytes(b"media")

            code, stdout, stderr = self._run_main(self._argv(root, outside, broll))

            self.assertEqual(code, 2)
            self.assertEqual(stdout, "")
            self.assertIn("path must stay under allowed roots", stderr)
            self.assertTrue(aroll.exists())

    def test_dry_run_does_not_create_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            aroll, broll = self._make_project_files(root)
            pending = root / "storage" / "nightly_jobs" / "pending"

            code, _, stderr = self._run_main(self._argv(root, aroll, broll))

            self.assertEqual(code, 0, stderr)
            self.assertFalse(pending.exists())


if __name__ == "__main__":
    unittest.main()
