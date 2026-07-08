import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.custom.kurukin_job_queue import (
    CONTAINER_API_BASE_URL,
    CONTAINER_NIGHTLY_QUEUE_DIR,
    KurukinJobQueueError,
    build_pending_job_filename,
    build_safe_runner_command,
    enqueue_moneyprinter_payload,
    find_result_for_job,
    get_recommended_result,
    get_storage_summary,
    is_aroll_broll_queue_enabled,
    is_aroll_broll_renderer_enabled,
    list_completed_render_jobs,
    list_rendered_videos,
    list_nightly_queue,
    list_render_tasks,
    read_video_bytes_for_download,
    sanitize_job_id,
    summarize_pending_job,
)


class TestKurukinJobQueue(unittest.TestCase):
    def _write_completed_job(
        self,
        base: Path,
        *,
        job_id: str = "job-results-001",
        task_id: str = "task-results-001",
        final_bytes: bytes | None = b"final",
        combined_bytes: bytes | None = None,
        final_task_payload: dict | None = None,
    ) -> tuple[Path, Path, Path]:
        completed_dir = base / "storage" / "nightly_jobs" / "completed" / f"done-{job_id}"
        tasks_dir = base / "storage" / "tasks"
        task_dir = tasks_dir / task_id
        completed_dir.mkdir(parents=True)
        task_dir.mkdir(parents=True)
        (completed_dir / "job.json").write_text(
            json.dumps({"job_id": job_id}),
            encoding="utf-8",
        )
        (completed_dir / "submit-response.json").write_text(
            json.dumps({"data": {"task_id": task_id}, "status": 200}),
            encoding="utf-8",
        )
        payload = final_task_payload or {
            "data": {
                "state": "completed",
                "progress": 100,
                "task_id": task_id,
                "videos": [f"/tasks/{task_id}/final-1.mp4"],
            }
        }
        (completed_dir / "final-task.json").write_text(
            json.dumps(payload),
            encoding="utf-8",
        )
        if final_bytes is not None:
            (task_dir / "final-1.mp4").write_bytes(final_bytes)
        if combined_bytes is not None:
            (task_dir / "combined-1.mp4").write_bytes(combined_bytes)
        return completed_dir, tasks_dir, task_dir

    def test_sanitize_job_id_normal(self):
        self.assertEqual(sanitize_job_id("render_001-abc"), "render_001-abc")

    def test_sanitize_job_id_with_spaces(self):
        self.assertEqual(sanitize_job_id("render console 001"), "render-console-001")

    def test_sanitize_job_id_removes_path_traversal(self):
        safe = sanitize_job_id("../../render console")
        self.assertEqual(safe, "render-console")
        self.assertNotIn("..", safe)
        self.assertNotIn("/", safe)

    def test_build_pending_job_filename_with_fixed_now(self):
        now = datetime(2026, 7, 5, 12, 34, 56, tzinfo=timezone.utc)
        self.assertEqual(
            build_pending_job_filename("render console", now=now),
            "20260705-123456-render-console.json",
        )

    def test_enqueue_moneyprinter_payload_writes_json(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            queue_dir = Path(tmp_dir) / "pending"
            path = enqueue_moneyprinter_payload(
                {"job_id": "render-001", "runner": {}},
                queue_dir=queue_dir,
                now=datetime(2026, 7, 5, 12, 0, 0, tzinfo=timezone.utc),
            )
            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(payload["job_id"], "render-001")
        self.assertTrue(path.name.endswith("render-001.json"))

    def test_enqueue_stays_inside_queue_dir(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            queue_dir = Path(tmp_dir) / "pending"
            path = enqueue_moneyprinter_payload(
                {"job_id": "../escape", "runner": {}},
                queue_dir=queue_dir,
            )

            path.resolve().relative_to(queue_dir.resolve())

    def test_enqueue_rejects_empty_payload(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            with self.assertRaises(KurukinJobQueueError):
                enqueue_moneyprinter_payload({}, queue_dir=Path(tmp_dir) / "pending")

    def test_aroll_broll_queue_flag_defaults_to_false(self):
        self.assertFalse(is_aroll_broll_queue_enabled({}))

    def test_aroll_broll_renderer_flag_defaults_to_false(self):
        self.assertFalse(is_aroll_broll_renderer_enabled({}))

    def test_aroll_broll_queue_flag_accepts_enabled_values(self):
        for value in ("1", "true", "TRUE", "yes", "on"):
            with self.subTest(value=value):
                self.assertTrue(
                    is_aroll_broll_queue_enabled(
                        {"KURUKIN_ENABLE_AROLL_BROLL_QUEUE": value}
                    )
                )

    def test_summarize_pending_job_labels_aroll_broll_payload(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            pending_path = Path(tmp_dir) / "20260708-120000-aroll-broll.json"
            pending_path.write_text(
                json.dumps(
                    {
                        "job_id": "aroll-broll-001",
                        "video_subject": "Presenter edit",
                        "render_mode": "aroll_broll",
                        "video_resolution": "draft_720p",
                        "aroll_broll": {
                            "subtitles": {"source": "custom_srt"},
                        },
                        "runner": {"job_id": "aroll-broll-001"},
                    }
                ),
                encoding="utf-8",
            )

            summary = summarize_pending_job(pending_path)

        self.assertTrue(summary["valid_json"])
        self.assertEqual(summary["asset_source"], "A-roll/B-roll")
        self.assertEqual(summary["subtitles"], "SRT propio")

    def test_list_nightly_queue_counts_groups(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            for group in ("pending", "processing", "completed", "failed", "logs"):
                group_dir = base / group
                group_dir.mkdir()
                (group_dir / f"{group}.json").write_text("{}", encoding="utf-8")

            queue = list_nightly_queue(base)

        self.assertEqual(set(queue), {"pending", "processing", "completed", "failed", "logs"})
        self.assertEqual(len(queue["pending"]), 1)
        self.assertEqual(len(queue["logs"]), 1)

    def test_list_render_tasks_detects_final_video(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            task_dir = Path(tmp_dir) / "task-001"
            task_dir.mkdir()
            (task_dir / "final-1.mp4").write_bytes(b"video")

            tasks = list_render_tasks(tmp_dir)

        self.assertEqual(tasks[0]["task_id"], "task-001")
        self.assertTrue(tasks[0]["has_final_video"])
        self.assertEqual(tasks[0]["final_video_size_bytes"], 5)

    def test_list_rendered_videos_empty_when_tasks_dir_missing(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tasks_dir = Path(tmp_dir) / "storage" / "tasks"

            videos = list_rendered_videos(tasks_dir)

        self.assertEqual(videos, [])
        self.assertFalse(tasks_dir.exists())

    def test_list_rendered_videos_detects_final_mp4(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            task_dir = Path(tmp_dir) / "storage" / "tasks" / "task-001"
            task_dir.mkdir(parents=True)
            (task_dir / "final-1.mp4").write_bytes(b"video")

            videos = list_rendered_videos(Path(tmp_dir) / "storage" / "tasks")

        self.assertEqual(len(videos), 1)
        self.assertEqual(videos[0]["task_id"], "task-001")
        self.assertEqual(videos[0]["file_name"], "final-1.mp4")
        self.assertEqual(videos[0]["relative_path"], "task-001/final-1.mp4")
        self.assertEqual(videos[0]["kind"], "final")
        self.assertEqual(videos[0]["size_bytes"], 5)
        self.assertEqual(videos[0]["size_label"], "5 B")
        self.assertTrue(videos[0]["is_previewable"])

    def test_list_rendered_videos_detects_combined_but_prioritizes_final(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tasks_dir = Path(tmp_dir) / "storage" / "tasks"
            final_dir = tasks_dir / "task-final"
            combined_dir = tasks_dir / "task-combined"
            final_dir.mkdir(parents=True)
            combined_dir.mkdir(parents=True)
            (final_dir / "final-1.mp4").write_bytes(b"final")
            (combined_dir / "combined-1.mp4").write_bytes(b"combined")

            videos = list_rendered_videos(tasks_dir)

        self.assertEqual([video["kind"] for video in videos], ["final", "combined"])

    def test_list_rendered_videos_sorts_same_kind_by_mtime_descending(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tasks_dir = Path(tmp_dir) / "storage" / "tasks"
            old_dir = tasks_dir / "task-old"
            new_dir = tasks_dir / "task-new"
            old_dir.mkdir(parents=True)
            new_dir.mkdir(parents=True)
            old_video = old_dir / "final-1.mp4"
            new_video = new_dir / "final-1.mp4"
            old_video.write_bytes(b"old")
            new_video.write_bytes(b"new")
            os.utime(old_video, (100, 100))
            os.utime(new_video, (200, 200))

            videos = list_rendered_videos(tasks_dir)

        self.assertEqual([video["task_id"] for video in videos], ["task-new", "task-old"])

    def test_list_rendered_videos_calculates_size_label(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tasks_dir = Path(tmp_dir) / "storage" / "tasks"
            task_dir = tasks_dir / "task-001"
            task_dir.mkdir(parents=True)
            (task_dir / "final-1.mp4").write_bytes(b"x" * 2048)

            videos = list_rendered_videos(tasks_dir)

        self.assertEqual(videos[0]["size_label"], "2.0 KB")

    def test_list_rendered_videos_ignores_symlink_that_escapes_tasks(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            tasks_dir = root / "storage" / "tasks"
            task_dir = tasks_dir / "task-001"
            outside_dir = root / "outside"
            task_dir.mkdir(parents=True)
            outside_dir.mkdir()
            outside_video = outside_dir / "final-1.mp4"
            outside_video.write_bytes(b"outside")
            try:
                (task_dir / "final-1.mp4").symlink_to(outside_video)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink not available: {exc}")

            videos = list_rendered_videos(tasks_dir)

        self.assertEqual(videos, [])

    def test_list_rendered_videos_ignores_non_mp4(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tasks_dir = Path(tmp_dir) / "storage" / "tasks"
            task_dir = tasks_dir / "task-001"
            task_dir.mkdir(parents=True)
            (task_dir / "final-1.txt").write_text("not video", encoding="utf-8")

            videos = list_rendered_videos(tasks_dir)

        self.assertEqual(videos, [])

    def test_read_video_bytes_for_download_reads_detected_video(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tasks_dir = Path(tmp_dir) / "storage" / "tasks"
            task_dir = tasks_dir / "task-001"
            task_dir.mkdir(parents=True)
            (task_dir / "final-1.mp4").write_bytes(b"video bytes")
            video = list_rendered_videos(tasks_dir)[0]

            data = read_video_bytes_for_download(video, tasks_dir=tasks_dir)

        self.assertEqual(data, b"video bytes")

    def test_read_video_bytes_for_download_returns_none_when_path_escapes_tasks(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            tasks_dir = root / "storage" / "tasks"
            outside_video = root / "outside.mp4"
            tasks_dir.mkdir(parents=True)
            outside_video.write_bytes(b"outside")
            video = {
                "absolute_path": outside_video.as_posix(),
                "relative_path": "task-001/final-1.mp4",
            }

            data = read_video_bytes_for_download(video, tasks_dir=tasks_dir)

        self.assertIsNone(data)

    def test_list_completed_render_jobs_links_job_to_task_and_final_video(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            completed_dir, tasks_dir, _ = self._write_completed_job(base)

            jobs = list_completed_render_jobs(
                completed_dir.parent,
                tasks_dir=tasks_dir,
            )

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["job_id"], "job-results-001")
        self.assertEqual(jobs[0]["task_id"], "task-results-001")
        self.assertEqual(jobs[0]["completed_dir"], completed_dir.name)
        self.assertEqual(jobs[0]["state"], "completed")
        self.assertEqual(jobs[0]["progress"], 100)
        self.assertEqual(jobs[0]["final_video_paths"], ["task-results-001/final-1.mp4"])

    def test_find_result_for_job_prefers_final_before_combined(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            completed_dir, tasks_dir, _ = self._write_completed_job(
                base,
                final_bytes=b"final",
                combined_bytes=b"combined",
            )

            result = find_result_for_job(
                "job-results-001",
                completed_dir=completed_dir.parent,
                tasks_dir=tasks_dir,
            )

        self.assertIsNotNone(result)
        self.assertEqual(result["kind"], "final")
        self.assertEqual(result["file_name"], "final-1.mp4")
        self.assertEqual(result["completed_job_id"], "job-results-001")

    def test_find_result_for_job_returns_none_without_mp4(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            completed_dir, tasks_dir, _ = self._write_completed_job(
                base,
                final_bytes=None,
                combined_bytes=None,
            )

            result = find_result_for_job(
                "job-results-001",
                completed_dir=completed_dir.parent,
                tasks_dir=tasks_dir,
            )

        self.assertIsNone(result)

    def test_get_recommended_result_uses_last_job_match(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            completed_dir, tasks_dir, task_dir = self._write_completed_job(
                base,
                job_id="job-new",
                task_id="task-new",
            )
            older_task = tasks_dir / "task-older"
            older_task.mkdir()
            old_video = older_task / "final-1.mp4"
            old_video.write_bytes(b"older")
            os.utime(old_video, (300, 300))
            os.utime(task_dir / "final-1.mp4", (100, 100))

            result = get_recommended_result(
                last_job_id="job-new",
                completed_dir=completed_dir.parent,
                tasks_dir=tasks_dir,
            )

        self.assertIsNotNone(result)
        self.assertEqual(result["task_id"], "task-new")
        self.assertEqual(result["recommendation"], "last_job")

    def test_get_recommended_result_falls_back_to_latest_final_video(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tasks_dir = Path(tmp_dir) / "storage" / "tasks"
            old_task = tasks_dir / "task-old"
            new_task = tasks_dir / "task-new"
            old_task.mkdir(parents=True)
            new_task.mkdir(parents=True)
            old_video = old_task / "final-1.mp4"
            new_video = new_task / "final-1.mp4"
            old_video.write_bytes(b"old")
            new_video.write_bytes(b"new")
            os.utime(old_video, (100, 100))
            os.utime(new_video, (200, 200))

            result = get_recommended_result(
                last_job_id="missing-job",
                completed_dir=Path(tmp_dir) / "storage" / "nightly_jobs" / "completed",
                tasks_dir=tasks_dir,
            )

        self.assertIsNotNone(result)
        self.assertEqual(result["task_id"], "task-new")
        self.assertEqual(result["recommendation"], "latest_final")

    def test_find_result_for_job_ignores_video_refs_outside_storage_tasks(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            completed_dir = base / "storage" / "nightly_jobs" / "completed" / "done-job"
            tasks_dir = base / "storage" / "tasks"
            outside_video = base / "outside" / "final-1.mp4"
            completed_dir.mkdir(parents=True)
            tasks_dir.mkdir(parents=True)
            outside_video.parent.mkdir()
            outside_video.write_bytes(b"outside")
            (completed_dir / "job.json").write_text(
                json.dumps({"job_id": "job-outside"}),
                encoding="utf-8",
            )
            (completed_dir / "final-task.json").write_text(
                json.dumps({"data": {"videos": [outside_video.as_posix()]}}),
                encoding="utf-8",
            )

            result = find_result_for_job(
                "job-outside",
                completed_dir=completed_dir.parent,
                tasks_dir=tasks_dir,
            )

        self.assertIsNone(result)

    def test_completed_render_job_helpers_handle_invalid_json_without_crash(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            completed_dir, tasks_dir, _ = self._write_completed_job(base)
            (completed_dir / "final-task.json").write_text(
                "{not-json",
                encoding="utf-8",
            )

            jobs = list_completed_render_jobs(
                completed_dir.parent,
                tasks_dir=tasks_dir,
            )
            result = find_result_for_job(
                "job-results-001",
                completed_dir=completed_dir.parent,
                tasks_dir=tasks_dir,
            )

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["task_id"], "task-results-001")
        self.assertIsNotNone(result)
        self.assertEqual(result["file_name"], "final-1.mp4")

    def test_get_storage_summary_returns_subdirs(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            storage = Path(tmp_dir)
            subdir = storage / "tasks"
            subdir.mkdir()
            (subdir / "artifact.txt").write_text("abc", encoding="utf-8")

            summary = get_storage_summary(storage)

        self.assertEqual(summary["path"], storage.as_posix())
        self.assertGreaterEqual(summary["size_bytes"], 3)
        self.assertEqual(summary["total_size_bytes"], summary["size_bytes"])
        self.assertEqual(summary["subdirs"][0]["name"], "tasks")

    def test_manual_runner_command_uses_fixed_container_api_url(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "scripts").mkdir()
            (root / "scripts" / "nightly_runner.py").write_text(
                "raise RuntimeError('must not execute')\n",
                encoding="utf-8",
            )

            command = build_safe_runner_command(
                root,
                manual_override=True,
                max_jobs=1,
            )

        self.assertIsInstance(command["command"], list)
        self.assertIn("--max-jobs", command["command"])
        self.assertIn("1", command["command"])
        self.assertIn("--ignore-window", command["command"])
        self.assertIn("--queue-dir", command["command"])
        self.assertIn(CONTAINER_NIGHTLY_QUEUE_DIR, command["command"])
        self.assertIn("--api-base-url", command["command"])
        api_url = command["command"][command["command"].index("--api-base-url") + 1]
        self.assertEqual(api_url, CONTAINER_API_BASE_URL)
        self.assertEqual(command["api_base_url"], CONTAINER_API_BASE_URL)
        self.assertNotIn("127.0.0.1:18080", api_url)


if __name__ == "__main__":
    unittest.main()
