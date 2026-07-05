import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.custom.kurukin_job_queue import (
    KurukinJobQueueError,
    build_pending_job_filename,
    enqueue_moneyprinter_payload,
    get_storage_summary,
    list_nightly_queue,
    list_render_tasks,
    sanitize_job_id,
)


class TestKurukinJobQueue(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
