import json
import inspect
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
    JOB_INTENT_QUEUE_SOURCE,
    KurukinJobQueueError,
    build_pending_job_filename,
    build_safe_runner_command,
    enqueue_job_intent,
    detect_render_mode_for_job,
    enqueue_moneyprinter_payload,
    find_result_for_job,
    get_recommended_result,
    get_storage_summary,
    is_aroll_broll_queue_enabled,
    is_aroll_broll_renderer_enabled,
    list_intent_results,
    list_intent_queue_items,
    list_completed_render_jobs,
    list_rendered_videos,
    list_nightly_queue,
    list_render_tasks,
    read_video_bytes_for_download,
    resolve_task_output_path,
    sanitize_job_id,
    summarize_intent_job_item,
    summarize_render_mode,
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
        job_payload: dict | None = None,
        submit_payload: dict | None = None,
        final_task_payload: dict | None = None,
    ) -> tuple[Path, Path, Path]:
        completed_dir = base / "storage" / "nightly_jobs" / "completed" / f"done-{job_id}"
        tasks_dir = base / "storage" / "tasks"
        task_dir = tasks_dir / task_id
        completed_dir.mkdir(parents=True)
        task_dir.mkdir(parents=True)
        (completed_dir / "job.json").write_text(
            json.dumps(job_payload or {"job_id": job_id}),
            encoding="utf-8",
        )
        (completed_dir / "submit-response.json").write_text(
            json.dumps(submit_payload or {"data": {"task_id": task_id}, "status": 200}),
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

    def _write_intent_queue_item(
        self,
        pending_dir: Path,
        *,
        task_id: str = "intent-results-001",
        status: str = "QUEUED",
        source: str = JOB_INTENT_QUEUE_SOURCE,
        filename: str | None = None,
        error: str = "",
    ) -> Path:
        pending_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "task_id": task_id,
            "status": status,
            "source": source,
            "mode": "audio_to_video",
            "original_intent": {
                "mode": "audio_to_video",
                "task_id": task_id,
                "audio_path": "storage/local_audios/example.mp3",
                "topic": "intent results test",
            },
            "normalized_intent": {
                "mode": "audio_to_video",
                "task_id": task_id,
                "audio_path": "storage/local_audios/example.mp3",
            },
            "compiled_mpt_spec": {
                "task_id": task_id,
                "params": {"custom_audio_file": "storage/local_audios/example.mp3"},
            },
            "resolved_visual_path": "storage/local_videos/example.mp4",
            "visual_autofill_source": "local_picker_v1",
            "error": error,
        }
        path = pending_dir / (filename or f"20260712-120000-{task_id}.json")
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

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

    def test_enqueue_job_intent_ready_to_submit_writes_queue_item(self):
        intent = {
            "mode": "audio_to_video",
            "task_id": "intent-queue-001",
            "audio_path": "storage/local_audios/audio.mp3",
            "topic": "Casa usada",
            "video_path": "storage/local_videos/casa.mp4",
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            queue_dir = Path(tmp_dir) / "pending"
            result = enqueue_job_intent(intent, queue_dir=queue_dir)
            payload = json.loads(Path(result["pending_path"]).read_text(encoding="utf-8"))

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "QUEUED")
        self.assertEqual(payload["source"], JOB_INTENT_QUEUE_SOURCE)
        self.assertEqual(payload["status"], "QUEUED")
        self.assertEqual(payload["task_id"], "intent-queue-001")
        self.assertEqual(payload["original_intent"], intent)
        self.assertEqual(payload["normalized_intent"]["video_path"], "storage/local_videos/casa.mp4")
        self.assertIn("compiled_mpt_spec", payload)
        self.assertFalse(payload["guardrails"]["real_render_started"])

    def test_enqueue_job_intent_audio_only_uses_local_picker(self):
        intent = {
            "mode": "audio_to_video",
            "task_id": "intent-queue-picker",
            "audio_path": "storage/local_audios/audio.mp3",
            "topic": "vertical reel smoke",
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            visual = root / "storage" / "local_videos" / "vertical-reel-smoke.mp4"
            visual.parent.mkdir(parents=True)
            visual.write_bytes(b"visual")
            queue_dir = root / "pending"
            result = enqueue_job_intent(
                intent,
                queue_dir=queue_dir,
                project_root=root,
            )
            payload = json.loads(Path(result["pending_path"]).read_text(encoding="utf-8"))

        self.assertTrue(result["ok"], result)
        self.assertEqual(
            payload["resolved_visual_path"],
            "storage/local_videos/vertical-reel-smoke.mp4",
        )
        self.assertEqual(payload["visual_autofill_source"], "local_picker_v1")
        self.assertEqual(
            payload["normalized_intent"]["resolved_visual_path"],
            "storage/local_videos/vertical-reel-smoke.mp4",
        )

    def test_enqueue_job_intent_needs_input_does_not_write_queue_item(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            queue_dir = root / "pending"
            result = enqueue_job_intent(
                {
                    "mode": "audio_to_video",
                    "task_id": "intent-queue-missing",
                    "audio_path": "storage/local_audios/audio.mp3",
                },
                queue_dir=queue_dir,
                project_root=root,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "NEEDS_INPUT")
        self.assertIn("needs_local_visual_asset", result["reasons"])
        self.assertFalse(queue_dir.exists())

    def test_enqueue_topic_to_video_without_audio_keeps_draft_out_of_queue(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            queue_dir = root / "pending"
            result = enqueue_job_intent(
                {
                    "mode": "topic_to_video",
                    "task_id": "topic-plan-draft",
                    "topic": "5 errores al comprar una casa usada",
                    "duration_seconds": 45,
                    "preset": "educational",
                },
                queue_dir=queue_dir,
                project_root=root,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "NEEDS_INPUT")
        self.assertEqual(result["reasons"], ["needs_audio_or_tts"])
        self.assertIn("script", result["compiled"]["intent"])
        self.assertGreaterEqual(len(result["compiled"]["intent"]["scenes"]), 3)
        self.assertFalse(queue_dir.exists())

    def test_enqueue_job_intent_does_not_call_render_or_external_surfaces(self):
        source = Path("app/custom/kurukin_job_queue.py").read_text(encoding="utf-8")

        for forbidden in (
            "openai",
            "text_to_speech",
            "requests.",
            "urlopen",
            "task.start",
            "/api/v1/videos",
        ):
            self.assertNotIn(forbidden, source.lower())

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
                            "b_roll": {
                                "assets": [
                                    "storage/local_assets/one.mp4",
                                    "storage/local_assets/two.mp4",
                                ]
                            },
                        },
                        "runner": {"job_id": "aroll-broll-001"},
                    }
                ),
                encoding="utf-8",
            )

            summary = summarize_pending_job(pending_path)

        self.assertTrue(summary["valid_json"])
        self.assertEqual(summary["render_mode"], "aroll_broll")
        self.assertEqual(summary["render_mode_label"], "Presentador + B-roll")
        self.assertEqual(summary["layout_preset"], "alternating_fullscreen")
        self.assertEqual(summary["audio_summary"], "A-roll original")
        self.assertEqual(summary["broll_summary"], "B-roll muted")
        self.assertEqual(summary["b_roll_asset_count"], 2)
        self.assertEqual(summary["asset_source"], "A-roll/B-roll")
        self.assertEqual(summary["subtitles"], "SRT propio")

    def test_summarize_pending_job_shows_open_asset_policy(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            pending_path = Path(tmp_dir) / "20260709-120000-aroll-broll.json"
            pending_path.write_text(
                json.dumps(
                    {
                        "job_id": "aroll-broll-open",
                        "render_mode": "aroll_broll",
                        "aroll_broll": {
                            "asset_policy": {"mode": "open_sources"},
                        },
                        "runner": {"job_id": "aroll-broll-open"},
                    }
                ),
                encoding="utf-8",
            )

            summary = summarize_pending_job(pending_path)

        self.assertEqual(summary["asset_policy_label"], "Open sources")
        self.assertEqual(summary["asset_policy_short_label"], "Fuentes: abiertas")

    def test_summarize_pending_job_shows_exclusive_brand_asset_policy(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            pending_path = Path(tmp_dir) / "20260709-121000-aroll-broll.json"
            pending_path.write_text(
                json.dumps(
                    {
                        "job_id": "aroll-broll-exclusive",
                        "render_mode": "aroll_broll",
                        "asset_policy": {
                            "mode": "exclusive_brand_assets",
                            "brand_asset_bundle_uid": "jab_test",
                        },
                        "runner": {"job_id": "aroll-broll-exclusive"},
                    }
                ),
                encoding="utf-8",
            )

            summary = summarize_pending_job(pending_path)

        self.assertEqual(summary["asset_policy_label"], "Exclusive brand assets")
        self.assertEqual(
            summary["asset_policy_short_label"],
            "Fuentes: marca exclusiva",
        )

    def test_summarize_pending_job_shows_asset_materialization_metadata(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            pending_path = Path(tmp_dir) / "20260709-122000-aroll-broll.json"
            pending_path.write_text(
                json.dumps(
                    {
                        "job_id": "aroll-broll-materialized",
                        "render_mode": "aroll_broll",
                        "asset_materialization": {
                            "source_provider": "pexels",
                            "query": "city walking",
                            "b_roll_asset_count": 3,
                        },
                        "runner": {"job_id": "aroll-broll-materialized"},
                    }
                ),
                encoding="utf-8",
            )

            summary = summarize_pending_job(pending_path)

        self.assertEqual(summary["asset_materialization_source_label"], "Pexels")
        self.assertEqual(summary["asset_materialization_query"], "city walking")
        self.assertEqual(summary["b_roll_asset_count"], 3)

    def test_summarize_render_mode_returns_human_labels(self):
        self.assertEqual(summarize_render_mode("normal"), "Video normal")
        self.assertEqual(summarize_render_mode("aroll_broll"), "Presentador + B-roll")

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
        self.assertEqual(jobs[0]["render_mode"], "normal")
        self.assertEqual(jobs[0]["render_mode_label"], "Video normal")
        self.assertEqual(jobs[0]["final_video_paths"], ["task-results-001/final-1.mp4"])

    def test_list_completed_render_jobs_detects_aroll_broll_from_job_json(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            completed_dir, tasks_dir, _ = self._write_completed_job(
                base,
                job_id="aroll-job-json",
                task_id="task-job-json",
                job_payload={
                    "job_id": "aroll-job-json",
                    "render_mode": "aroll_broll",
                    "aroll_broll": {"layout": {"preset": "alternating_fullscreen"}},
                },
            )

            jobs = list_completed_render_jobs(completed_dir.parent, tasks_dir=tasks_dir)

        self.assertEqual(jobs[0]["render_mode"], "aroll_broll")
        self.assertEqual(jobs[0]["render_mode_label"], "Presentador + B-roll")
        self.assertEqual(jobs[0]["layout_preset"], "alternating_fullscreen")
        self.assertEqual(jobs[0]["audio_summary"], "A-roll original")
        self.assertEqual(jobs[0]["broll_summary"], "B-roll muted")

    def test_list_completed_render_jobs_detects_aroll_broll_from_final_task_data(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            completed_dir, tasks_dir, _ = self._write_completed_job(
                base,
                job_id="job-final-render-mode",
                task_id="task-final-render-mode",
                final_task_payload={
                    "data": {
                        "state": "completed",
                        "progress": 100,
                        "task_id": "task-final-render-mode",
                        "render_mode": "aroll_broll",
                        "layout_preset": "alternating_fullscreen",
                        "videos": ["/tasks/task-final-render-mode/final-1.mp4"],
                    }
                },
            )

            jobs = list_completed_render_jobs(completed_dir.parent, tasks_dir=tasks_dir)

        self.assertEqual(jobs[0]["render_mode"], "aroll_broll")
        self.assertEqual(jobs[0]["render_mode_label"], "Presentador + B-roll")

    def test_list_completed_render_jobs_detects_aroll_broll_from_submit_response(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            completed_dir, tasks_dir, _ = self._write_completed_job(
                base,
                job_id="job-submit-render-mode",
                task_id="task-submit-render-mode",
                submit_payload={
                    "status": 200,
                    "render_mode": "aroll_broll",
                    "data": {"task_id": "task-submit-render-mode"},
                },
            )

            jobs = list_completed_render_jobs(completed_dir.parent, tasks_dir=tasks_dir)

        self.assertEqual(jobs[0]["render_mode"], "aroll_broll")
        self.assertEqual(jobs[0]["render_mode_label"], "Presentador + B-roll")

    def test_list_completed_render_jobs_falls_back_to_aroll_broll_task_id(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            completed_dir, tasks_dir, _ = self._write_completed_job(
                base,
                job_id="job-fallback",
                task_id="aroll-broll-runner-smoke-003",
            )

            jobs = list_completed_render_jobs(completed_dir.parent, tasks_dir=tasks_dir)
            detected_mode = detect_render_mode_for_job(completed_dir)

        self.assertEqual(jobs[0]["task_id"], "aroll-broll-runner-smoke-003")
        self.assertEqual(jobs[0]["render_mode"], "aroll_broll")
        self.assertEqual(jobs[0]["render_mode_label"], "Presentador + B-roll")
        self.assertEqual(detected_mode, "aroll_broll")

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

    def test_find_result_for_job_preserves_aroll_broll_metadata(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            completed_dir, tasks_dir, _ = self._write_completed_job(
                base,
                job_id="job-aroll-result",
                task_id="aroll-broll-result-001",
                job_payload={"job_id": "job-aroll-result", "render_mode": "aroll_broll"},
            )

            result = find_result_for_job(
                "job-aroll-result",
                completed_dir=completed_dir.parent,
                tasks_dir=tasks_dir,
            )

        self.assertIsNotNone(result)
        self.assertEqual(result["render_mode"], "aroll_broll")
        self.assertEqual(result["render_mode_label"], "Presentador + B-roll")
        self.assertEqual(result["layout_preset"], "alternating_fullscreen")
        self.assertEqual(result["audio_summary"], "A-roll original")
        self.assertEqual(result["broll_summary"], "B-roll muted")
        self.assertNotIn("asset_policy_short_label", result)

    def test_find_result_for_job_preserves_asset_policy_metadata(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            completed_dir, tasks_dir, _ = self._write_completed_job(
                base,
                job_id="job-aroll-result",
                task_id="aroll-broll-result-001",
                job_payload={
                    "job_id": "job-aroll-result",
                    "render_mode": "aroll_broll",
                    "asset_policy": {"mode": "local_only"},
                },
            )

            result = find_result_for_job(
                "job-aroll-result",
                completed_dir=completed_dir.parent,
                tasks_dir=tasks_dir,
            )

        self.assertIsNotNone(result)
        self.assertEqual(result["asset_policy_label"], "Local only")
        self.assertEqual(result["asset_policy_short_label"], "Fuentes: locales")

    def test_find_result_for_job_preserves_asset_materialization_metadata(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            completed_dir, tasks_dir, _ = self._write_completed_job(
                base,
                job_id="job-aroll-materialized",
                task_id="aroll-broll-materialized-001",
                job_payload={
                    "job_id": "job-aroll-materialized",
                    "render_mode": "aroll_broll",
                    "asset_materialization": {
                        "source_provider": "manifest",
                        "query": "brand launch",
                        "b_roll_asset_count": 2,
                    },
                },
            )

            result = find_result_for_job(
                "job-aroll-materialized",
                completed_dir=completed_dir.parent,
                tasks_dir=tasks_dir,
            )

        self.assertIsNotNone(result)
        self.assertEqual(result["asset_materialization_source_label"], "manifest")
        self.assertEqual(result["asset_materialization_query"], "brand launch")
        self.assertEqual(result["b_roll_asset_count"], 2)

    def test_old_completed_aroll_broll_job_without_asset_policy_does_not_break(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            completed_dir, tasks_dir, _ = self._write_completed_job(
                base,
                job_id="job-aroll-old",
                task_id="aroll-broll-old-001",
                job_payload={"job_id": "job-aroll-old", "render_mode": "aroll_broll"},
            )

            jobs = list_completed_render_jobs(
                completed_dir=completed_dir.parent,
                tasks_dir=tasks_dir,
            )

        self.assertEqual(jobs[0]["render_mode"], "aroll_broll")
        self.assertNotIn("asset_policy_short_label", jobs[0])

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

    def test_list_intent_results_lists_job_intent_source_only(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            pending_dir = Path(tmp_dir) / "pending"
            self._write_intent_queue_item(pending_dir, task_id="intent-results-001")
            self._write_intent_queue_item(
                pending_dir,
                task_id="other-source-001",
                source="nightly_runner",
            )

            results = list_intent_results(pending_dir, tasks_dir=Path(tmp_dir) / "tasks")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["task_id"], "intent-results-001")
        self.assertEqual(results[0]["source"], JOB_INTENT_QUEUE_SOURCE)
        self.assertEqual(results[0]["visual_autofill_source"], "local_picker_v1")

    def test_list_intent_results_detects_done_output_exists(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            pending_dir = root / "pending"
            tasks_dir = root / "tasks"
            task_dir = tasks_dir / "intent-results-done"
            task_dir.mkdir(parents=True)
            final_path = task_dir / "final-1.mp4"
            final_path.write_bytes(b"mp4")
            self._write_intent_queue_item(
                pending_dir,
                task_id="intent-results-done",
                status="DONE",
            )

            results = list_intent_results(
                pending_dir,
                tasks_dir=tasks_dir,
                status="DONE",
            )
            resolved = resolve_task_output_path(
                "intent-results-done",
                tasks_dir=tasks_dir,
            )

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["output_exists"])
        self.assertEqual(results[0]["output_path"], final_path.resolve().as_posix())
        self.assertEqual(resolved, final_path.resolve().as_posix())

    def test_list_intent_results_output_exists_false_without_final(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            pending_dir = Path(tmp_dir) / "pending"
            self._write_intent_queue_item(
                pending_dir,
                task_id="intent-results-missing",
                status="DONE",
            )

            results = list_intent_results(pending_dir, tasks_dir=Path(tmp_dir) / "tasks")

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["output_exists"])
        self.assertEqual(results[0]["output_path"], "")

    def test_list_intent_results_failed_includes_error(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            pending_dir = Path(tmp_dir) / "pending"
            self._write_intent_queue_item(
                pending_dir,
                task_id="intent-results-failed",
                status="FAILED",
                error="native submit failed",
            )

            results = list_intent_queue_items(
                pending_dir,
                tasks_dir=Path(tmp_dir) / "tasks",
                status="FAILED",
            )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "FAILED")
        self.assertEqual(results[0]["error"], "native submit failed")

    def test_list_intent_results_filters_status_and_respects_limit(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            pending_dir = Path(tmp_dir) / "pending"
            self._write_intent_queue_item(
                pending_dir,
                task_id="intent-results-queued-001",
                filename="20260712-120001-intent-results-queued-001.json",
            )
            self._write_intent_queue_item(
                pending_dir,
                task_id="intent-results-done-001",
                status="DONE",
                filename="20260712-120002-intent-results-done-001.json",
            )
            self._write_intent_queue_item(
                pending_dir,
                task_id="intent-results-done-002",
                status="DONE",
                filename="20260712-120003-intent-results-done-002.json",
            )

            results = list_intent_results(
                pending_dir,
                tasks_dir=Path(tmp_dir) / "tasks",
                status="DONE",
                limit=1,
            )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "DONE")
        self.assertEqual(results[0]["task_id"], "intent-results-done-002")

    def test_summarize_intent_job_item_keeps_core_fields(self):
        payload = {
            "task_id": "intent-summary-001",
            "status": "QUEUED",
            "source": JOB_INTENT_QUEUE_SOURCE,
            "mode": "audio_to_video",
            "normalized_intent": {"audio_path": "storage/local_audios/a.mp3"},
            "resolved_visual_path": "storage/local_videos/v.mp4",
            "visual_autofill_source": "local_picker_v1",
        }

        summary = summarize_intent_job_item({"path": "pending/item.json", "raw": payload})

        self.assertEqual(summary["task_id"], "intent-summary-001")
        self.assertEqual(summary["audio_path"], "storage/local_audios/a.mp3")
        self.assertEqual(summary["queue_item_path"], "pending/item.json")
        self.assertFalse(summary["output_exists"])

    def test_intent_results_helpers_do_not_reference_execution_surfaces(self):
        source = "\n".join(
            inspect.getsource(func)
            for func in (
                list_intent_results,
                list_intent_queue_items,
                summarize_intent_job_item,
                resolve_task_output_path,
            )
        )

        for forbidden in (
            "run_controlled_runner",
            "nightly_runner",
            "/api/v1/videos",
            "submit_mpt",
            "openai",
            "pexels",
            "pixabay",
            "coverr",
            "asset_hub",
        ):
            self.assertNotIn(forbidden, source.lower())


if __name__ == "__main__":
    unittest.main()
