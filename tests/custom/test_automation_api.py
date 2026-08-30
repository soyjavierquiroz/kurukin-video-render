"""Focused offline tests for the internal Human Review automation API."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.custom import human_review
from scripts import automation_api


class AutomationApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.jobs = self.root / "content_jobs"
        self.client = TestClient(automation_api.app)

    def tearDown(self):
        self.tmp.cleanup()

    def _job(self, content_id="cid_001"):
        job = self.jobs / "test-niche" / content_id
        job.mkdir(parents=True)
        (job / "content.json").write_text(json.dumps({
            "content_id": content_id, "niche_id": "test-niche", "title": "A title",
        }), encoding="utf-8")
        return job

    def _plan(self, content_id="cid_001"):
        path = human_review.plan_path(
            "content-test-niche-" + content_id, "A-title", self.root,
        )
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({
            "review_status": "pending_review",
            "content_job": {"content_id": content_id},
        }), encoding="utf-8")
        return path

    def _identity_job(self, content_id="cid_001"):
        job = self.jobs / "test-niche" / content_id
        job.mkdir(parents=True)
        metadata = {
            "content_id": content_id,
            "niche_id": "test-niche",
            "title": "Stored title",
            "audio_file_id": "audio-id",
            "script_file_id": "script-id",
            "asset_profile": "MI_OTRA_YO",
            "audio_sha256": "a" * 64,
            "script_sha256": "b" * 64,
            "resolved_asset_policy": {"providers": ["asset_hub"]},
        }
        (job / "content.json").write_text(json.dumps(metadata), encoding="utf-8")
        return job, metadata

    def _identity_plan(self, metadata, *, legacy_batch_id=False, review_status="pending_review"):
        path = human_review.plan_path(
            "content-test-niche-" + metadata["content_id"], "Stored-title", self.root,
        )
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({
            "review_status": review_status,
            # Legacy adapters used the content ID here, while the directory
            # and task identity above remained deterministic.
            "batch_id": metadata["content_id"] if legacy_batch_id else "content-test-niche-" + metadata["content_id"],
            "content_job": {
                key: metadata[key] for key in (
                    "content_id", "niche_id", "asset_profile", "audio_sha256", "script_sha256",
                    "resolved_asset_policy",
                )
            },
        }), encoding="utf-8")
        return path

    def _current_plan(self, metadata, *, review_status="pending_review", **overrides):
        """Create the provenance shape emitted by the current automation path."""
        content_id = metadata["content_id"]
        niche_id = metadata["niche_id"]
        path = human_review.plan_path(
            f"content-{niche_id}-{content_id}", "Stored-title", self.root,
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        plan = {
            "review_status": review_status,
            "batch_id": content_id,
            "task_id": f"batch-content-{niche_id}-{content_id}-editorial-title",
            "audio_path": f"/MoneyPrinterTurbo/storage/content_jobs/{niche_id}/{content_id}/source.mp3",
            "script_path": f"/MoneyPrinterTurbo/storage/content_jobs/{niche_id}/{content_id}/script.txt",
        }
        plan.update(overrides)
        path.write_text(json.dumps(plan), encoding="utf-8")
        return path

    def _approved_schedule_fixture(self, content_id="cid_001"):
        job, metadata = self._identity_job(content_id)
        plan = self._identity_plan(metadata, review_status=human_review.STATUS_APPROVED)
        return job, metadata, plan

    def _schedule_context(self):
        return patch.object(automation_api.content_ingest, "DEFAULT_JOB_ROOT", self.jobs), patch.object(
            automation_api.create_content_job_review.produce_batch, "HOST_ROOT", self.root,
        )

    def _nightly_queue(self, *, pending=0, processing=0, completed=0, failed=0):
        queue = self.root / "storage" / "nightly_jobs"
        for directory, count in {
            "pending": pending,
            "processing": processing,
            "completed": completed,
            "failed": failed,
        }.items():
            root = queue / directory
            root.mkdir(parents=True, exist_ok=True)
            for number in range(count):
                if directory == "pending":
                    (root / f"job-{number}.json").write_text("{}", encoding="utf-8")
                else:
                    run = root / f"run-{number}"
                    run.mkdir()
                    (run / "job.json").write_text("{}", encoding="utf-8")
        return queue

    def _review_queue_job(self, plan, content_id):
        return {
            "render_mode": human_review.RENDER_MODE,
            "job_id": f"review-{content_id}",
            "batch_id": f"content-test-niche-{content_id}",
            "production_plan_path": plan.as_posix(),
        }

    @staticmethod
    def _identity_payload(**overrides):
        payload = {
            "niche_id": "test-niche", "content_id": "cid_001", "title": "Different editorial title",
            "hook_title": "Different optional hook", "audio_file_id": "audio-id",
            "script_file_id": "script-id", "asset_profile": "MI_OTRA_YO",
        }
        payload.update(overrides)
        return payload

    def test_health(self):
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})

    def test_niches_returns_only_safe_enabled_registry_entries_without_hardcoding(self):
        entries = [
            ("generic-niche", {"sheet_id": "sheet-1", "sheet_tab": "Ideas"}),
        ]
        with patch.object(automation_api, "enabled_niches", return_value=entries):
            response = self.client.get("/v1/niches")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True, "niches": [{
            "niche_id": "generic-niche", "enabled": True, "sheet_id": "sheet-1", "sheet_tab": "Ideas",
        }]})
        self.assertNotIn("rclone_remote", response.text)
        self.assertNotIn("final_drive_folder_id", response.text)
        self.assertNotIn("/storage/", response.text)

    def test_niches_excludes_disabled_entries_and_registry_failure_is_safe(self):
        with patch.object(automation_api, "enabled_niches", return_value=[]):
            response = self.client.get("/v1/niches")
        self.assertEqual(response.json(), {"ok": True, "niches": []})
        with patch.object(automation_api, "enabled_niches", side_effect=automation_api.NicheRegistryError("secret=x")):
            response = self.client.get("/v1/niches")
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("secret", response.text)

    def _reconcile(self, content_id="cid_001", **overrides):
        payload = {"niche_id": "test-niche", "status": "READY", "run_mode": "NIGHT"}
        payload.update(overrides)
        return self.client.post(f"/v1/content/{content_id}/reconcile", json=payload)

    def test_reconcile_draft_is_noop_and_unknown_or_disabled_niche_is_rejected(self):
        with patch.object(automation_api, "_validate_enabled_niche") as validate:
            response = self._reconcile(status="DRAFT")
        self.assertEqual(response.json()["status"], "DRAFT")
        validate.assert_called_once_with("test-niche")
        with patch.object(automation_api, "load_niche", side_effect=automation_api.NicheRegistryError("unknown")):
            unknown = self._reconcile(status="DRAFT", niche_id="unknown")
        self.assertEqual(unknown.status_code, 404)
        with patch.object(automation_api, "load_niche", return_value={"enabled": False}):
            disabled = self._reconcile(status="DRAFT")
        self.assertEqual(disabled.status_code, 403)

    def test_reconcile_ready_accepts_review_preparation_before_a_plan_exists(self):
        jobs_root, host_root = self._schedule_context()
        with patch.object(automation_api, "_validate_enabled_niche"), jobs_root, host_root, patch.object(
            automation_api.content_ingest, "validate_request"
        ), patch.object(automation_api.content_ingest, "ingest_content") as ingest, patch.object(
            automation_api.create_content_job_review, "create_content_job_review"
        ) as review:
            response = self._reconcile(**self._identity_payload())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "PREPARING_REVIEW")
        self.assertIsNone(response.json()["review_url"])
        path = automation_api.review_preparation.state_path("test-niche", "cid_001", job_root=self.jobs)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["state"], "pending")
        ingest.assert_not_called()
        review.assert_not_called()

    def test_reconcile_completed_preparation_with_valid_plan_is_human_review_ready(self):
        _, metadata = self._identity_job()
        self._current_plan(metadata)
        record = automation_api.review_preparation.enqueue(self._identity_payload(), job_root=self.jobs)
        record["state"] = "completed"
        path = automation_api.review_preparation.state_path("test-niche", "cid_001", job_root=self.jobs)
        path.write_text(json.dumps(record), encoding="utf-8")
        jobs_root, host_root = self._schedule_context()
        with patch.object(automation_api, "_validate_enabled_niche"), jobs_root, host_root:
            response = self._reconcile(**self._identity_payload())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "HUMAN_REVIEW_READY")
        self.assertEqual(response.json()["review_url"], "/?content_id=cid_001")

    def test_reconcile_unapproved_and_identity_conflict_are_safe(self):
        _, metadata = self._identity_job()
        self._identity_plan(metadata)
        jobs_root, host_root = self._schedule_context()
        with patch.object(automation_api, "_validate_enabled_niche"), jobs_root, host_root:
            response = self._reconcile()
        self.assertEqual(response.json()["status"], "HUMAN_REVIEW_READY")
        jobs_root, host_root = self._schedule_context()
        with patch.object(automation_api, "_validate_enabled_niche"), jobs_root, host_root:
            conflict = self._reconcile(niche_id="other-niche")
        self.assertEqual(conflict.status_code, 409)
        with patch.object(automation_api, "_validate_enabled_niche"), jobs_root, host_root:
            draft_conflict = self._reconcile(status="DRAFT", niche_id="other-niche")
        self.assertEqual(draft_conflict.status_code, 409)

    def test_reconcile_queued_night_preserves_night_and_promotes_now(self):
        job, _, plan = self._approved_schedule_fixture()
        jobs_root, host_root = self._schedule_context()
        with patch.object(automation_api, "_validate_enabled_niche"), jobs_root, host_root, patch.object(
            automation_api, "_nightly_queue_dir", return_value=self.root / "storage/nightly_jobs",
        ):
            night = self._reconcile(status="QUEUED_NIGHT", run_mode="NIGHT")
            night_again = self._reconcile(status="QUEUED_NIGHT", run_mode="NIGHT")
            now = self._reconcile(status="QUEUED_NIGHT", run_mode="NOW")
        self.assertEqual(night.json()["status"], "QUEUED_NIGHT")
        self.assertEqual(night_again.json()["status"], "QUEUED_NIGHT")
        self.assertEqual(now.json()["status"], "PRODUCING")
        self.assertEqual(len(list((self.root / "storage/nightly_jobs/pending").glob("*.json"))), 0)
        schedule = json.loads((job / "production-schedule.json").read_text(encoding="utf-8"))
        self.assertEqual(schedule["production_state"], "launching")

    def test_reconcile_producing_completed_and_error_are_idempotent(self):
        job, _, _ = self._approved_schedule_fixture()
        jobs_root, host_root = self._schedule_context()
        (job / "production-schedule.json").write_text(json.dumps({"production_state": "producing"}), encoding="utf-8")
        with patch.object(automation_api, "_validate_enabled_niche"), jobs_root, host_root:
            producing = self._reconcile(status="PRODUCING", run_mode="NOW")
        self.assertEqual(producing.json()["status"], "PRODUCING")

        (job / "production-schedule.json").write_text(json.dumps({"production_state": "completed"}), encoding="utf-8")
        (job / "delivery.json").write_text(json.dumps({
            "content_id": "cid_001", "niche_id": "test-niche", "final_drive_file_id": "file-1",
            "final_drive_url": "https://drive.google.com/file/d/file-1/view", "checksum": "a" * 64,
        }), encoding="utf-8")
        jobs_root, host_root = self._schedule_context()
        with patch.object(automation_api, "_validate_enabled_niche"), jobs_root, host_root:
            completed = self._reconcile(status="COMPLETED", run_mode="NOW")
        self.assertEqual(completed.json()["status"], "COMPLETED")
        self.assertEqual(completed.json()["final_drive_file_id"], "file-1")
        self.assertEqual(completed.json()["final_drive_url"], "https://drive.google.com/file/d/file-1/view")
        self.assertEqual(completed.json()["checksum"], "a" * 64)

        (job / "production-schedule.json").write_text(json.dumps({"production_state": "error"}), encoding="utf-8")
        jobs_root, host_root = self._schedule_context()
        with patch.object(automation_api, "_validate_enabled_niche"), jobs_root, host_root:
            errored = self._reconcile(status="ERROR", run_mode="NOW")
        self.assertEqual(errored.json()["status"], "ERROR")

    def test_reconcile_review_projection_statuses_follow_actual_review_facts(self):
        _, metadata = self._identity_job()
        self._identity_plan(metadata)
        for status in ("PREPARING_REVIEW", "HUMAN_REVIEW_READY", "PRODUCTION_READY"):
            with self.subTest(status=status):
                jobs_root, host_root = self._schedule_context()
                with patch.object(automation_api, "_validate_enabled_niche"), jobs_root, host_root:
                    response = self._reconcile(status=status, run_mode="NIGHT")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["status"], "HUMAN_REVIEW_READY")

    def test_reconcile_unknown_projection_status_is_rejected(self):
        with patch.object(automation_api, "_validate_enabled_niche"):
            response = self._reconcile(status="GARBAGE")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"detail": "invalid status"})

    def test_nightly_status_reports_one_pending_job(self):
        queue = self._nightly_queue(pending=1)
        with patch.object(automation_api, "_nightly_queue_dir", return_value=queue):
            response = self.client.get("/v1/nightly/status")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "ok": True, "running": False, "lock_present": False,
            "pending_count": 1, "processing_count": 0,
            "completed_count": 0, "failed_count": 0, "current_job": None,
        })

    def test_nightly_status_reports_queue_counts_processing_job_and_lock(self):
        queue = self._nightly_queue(pending=1, processing=1, completed=1, failed=1)
        (queue / "nightly_runner.lock").write_text("pid=123\n", encoding="utf-8")
        with patch.object(automation_api, "_nightly_queue_dir", return_value=queue):
            response = self.client.get("/v1/nightly/status")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "ok": True, "running": True, "lock_present": True,
            "pending_count": 1, "processing_count": 1,
            "completed_count": 1, "failed_count": 1, "current_job": "run-0",
        })

    def test_nightly_run_with_no_pending_does_not_create_a_process(self):
        queue = self._nightly_queue()
        with patch.object(automation_api, "_nightly_queue_dir", return_value=queue), patch("subprocess.Popen") as popen:
            response = self.client.post("/v1/nightly/run")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True, "nightly_state": "no_pending"})
        popen.assert_not_called()

    def test_nightly_run_outside_window_does_not_create_a_process(self):
        queue = self._nightly_queue(pending=1)
        with patch.object(automation_api, "_nightly_queue_dir", return_value=queue), patch.object(
            automation_api, "_nightly_window_is_open", return_value=False
        ), patch("subprocess.Popen") as popen:
            response = self.client.post("/v1/nightly/run")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True, "nightly_state": "outside_window"})
        popen.assert_not_called()

    def test_nightly_run_with_active_runner_does_not_create_a_process(self):
        queue = self._nightly_queue(pending=1)
        (queue / "nightly_runner.lock").write_text("pid=123\n", encoding="utf-8")
        with patch.object(automation_api, "_nightly_queue_dir", return_value=queue), patch("subprocess.Popen") as popen:
            response = self.client.post("/v1/nightly/run")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True, "nightly_state": "already_running"})
        popen.assert_not_called()

    def test_nightly_run_accepts_pending_job_without_creating_process_or_logs(self):
        queue = self._nightly_queue(pending=1)
        with patch.object(automation_api, "_nightly_queue_dir", return_value=queue), patch.object(
            automation_api, "_nightly_window_is_open", return_value=True
        ), patch("subprocess.Popen") as popen:
            response = self.client.post("/v1/nightly/run")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True, "nightly_state": "started"})
        popen.assert_not_called()
        self.assertEqual(len(list((queue / "pending").glob("*.json"))), 1)
        self.assertFalse((queue / "logs").exists())
        self.assertFalse(hasattr(automation_api, "_launch_nightly_runner"))

    def test_review_url_encodes_content_id(self):
        self.assertEqual(automation_api.review_relative_url("a b/?"), "/?content_id=a%20b%2F%3F")

    def test_status_lookup(self):
        self._job()
        self._plan()
        with patch.object(automation_api.content_ingest, "DEFAULT_JOB_ROOT", self.jobs), patch.object(
            automation_api.create_content_job_review.produce_batch, "HOST_ROOT", self.root
        ):
            response = self.client.get("/v1/content/cid_001")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["review_status"], "pending_review")
        self.assertTrue(response.json()["review_exists"])

    def test_unknown_content_is_404(self):
        with patch.object(automation_api.content_ingest, "DEFAULT_JOB_ROOT", self.jobs):
            response = self.client.get("/v1/content/missing")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "content job not found")

    def test_request_validation_is_400(self):
        response = self.client.post("/v1/content/review", json={})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"error": "invalid request"})

    def test_existing_identity_is_reused_without_approval_or_production(self):
        _, metadata = self._identity_job()
        payload = self._identity_payload()
        with patch.object(automation_api.content_ingest, "validate_request"), patch.object(
            automation_api.content_ingest, "ingest_content", return_value=metadata
        ) as ingest, patch.object(
            automation_api.create_content_job_review, "create_content_job_review"
        ) as review, patch.object(human_review, "approve_plan") as approve, patch.object(
            automation_api.create_content_job_review.produce_batch, "process_job"
        ) as production, patch.object(
            automation_api.review_preparation, "enqueue", return_value={"state": "pending"}
        ) as enqueue:
            response = self.client.post("/v1/content/review", json=payload)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], "PREPARING_REVIEW")
        self.assertEqual(response.json()["review_status"], "pending_review")
        self.assertIsNone(response.json()["review_relative_url"])
        enqueue.assert_called_once()
        ingest.assert_not_called()
        review.assert_not_called()
        approve.assert_not_called()
        production.assert_not_called()

    def test_existing_matching_review_is_returned_without_regeneration(self):
        _, metadata = self._identity_job()
        self._identity_plan(metadata, legacy_batch_id=True)
        payload = self._identity_payload()
        with patch.object(automation_api.content_ingest, "DEFAULT_JOB_ROOT", self.jobs), patch.object(
            automation_api.create_content_job_review.produce_batch, "HOST_ROOT", self.root
        ), patch.object(automation_api.content_ingest, "validate_request"), patch.object(
            automation_api.content_ingest, "ingest_content"
        ) as ingest, patch.object(
            automation_api.create_content_job_review, "create_content_job_review"
        ) as review, patch.object(human_review, "approve_plan") as approve, patch.object(
            automation_api.create_content_job_review.produce_batch, "process_job"
        ) as process, patch.object(automation_api.review_preparation, "enqueue") as enqueue:
            response = self.client.post("/v1/content/review", json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "ok": True, "content_id": "cid_001", "niche_id": "test-niche",
            "status": "HUMAN_REVIEW_READY", "review_status": "pending_review",
            "review_relative_url": "/?content_id=cid_001",
        })
        enqueue.assert_not_called()
        ingest.assert_not_called()
        review.assert_not_called()
        process.assert_not_called()
        approve.assert_not_called()

    def test_existing_legacy_review_reuses_explicit_effective_defaults(self):
        job, metadata = self._identity_job()
        metadata["mpt_defaults"] = {"video_aspect": "9:16", "video_clip_duration": 5}
        job.joinpath("content.json").write_text(json.dumps(metadata), encoding="utf-8")
        self._identity_plan(metadata, legacy_batch_id=True)
        with patch.object(automation_api.content_ingest, "DEFAULT_JOB_ROOT", self.jobs), patch.object(
            automation_api.create_content_job_review.produce_batch, "HOST_ROOT", self.root
        ), patch.object(automation_api.content_ingest, "validate_request"), patch.object(
            automation_api.content_ingest, "ingest_content"
        ) as ingest, patch.object(
            automation_api.review_preparation, "enqueue"
        ) as enqueue:
            response = self.client.post("/v1/content/review", json=self._identity_payload())
        self.assertEqual(response.status_code, 200)
        ingest.assert_not_called()
        enqueue.assert_not_called()

    def test_existing_legacy_review_requires_preparation_for_changed_visual_semantics(self):
        job, metadata = self._identity_job()
        metadata["mpt_defaults"] = {"video_aspect": "16:9"}
        job.joinpath("content.json").write_text(json.dumps(metadata), encoding="utf-8")
        self._identity_plan(metadata, legacy_batch_id=True)
        with patch.object(automation_api.content_ingest, "DEFAULT_JOB_ROOT", self.jobs), patch.object(
            automation_api.create_content_job_review.produce_batch, "HOST_ROOT", self.root
        ), patch.object(automation_api.content_ingest, "validate_request"), patch.object(
            automation_api.content_ingest, "ingest_content"
        ) as ingest, patch.object(
            automation_api.create_content_job_review, "create_content_job_review"
        ) as review, patch.object(
            automation_api.review_preparation, "enqueue", return_value={"state": "pending"}
        ) as enqueue:
            response = self.client.post("/v1/content/review", json=self._identity_payload())
        self.assertEqual(response.status_code, 202)
        ingest.assert_not_called()
        review.assert_not_called()
        enqueue.assert_called_once()

    def test_existing_legacy_review_requires_preparation_for_changed_clip_duration(self):
        job, metadata = self._identity_job()
        metadata["mpt_defaults"] = {"video_clip_duration": 7}
        job.joinpath("content.json").write_text(json.dumps(metadata), encoding="utf-8")
        self._identity_plan(metadata, legacy_batch_id=True)
        with patch.object(automation_api.content_ingest, "DEFAULT_JOB_ROOT", self.jobs), patch.object(
            automation_api.create_content_job_review.produce_batch, "HOST_ROOT", self.root
        ), patch.object(automation_api.content_ingest, "validate_request"), patch.object(
            automation_api.review_preparation, "enqueue", return_value={"state": "pending"}
        ) as enqueue:
            response = self.client.post("/v1/content/review", json=self._identity_payload())
        self.assertEqual(response.status_code, 202)
        enqueue.assert_called_once()

    def test_existing_legacy_review_ignores_bgm_only_defaults_change(self):
        job, metadata = self._identity_job()
        metadata["mpt_defaults"] = {"bgm": {"mode": "RANDOM", "volume": .2}}
        job.joinpath("content.json").write_text(json.dumps(metadata), encoding="utf-8")
        self._identity_plan(metadata, legacy_batch_id=True)
        with patch.object(automation_api.content_ingest, "DEFAULT_JOB_ROOT", self.jobs), patch.object(
            automation_api.create_content_job_review.produce_batch, "HOST_ROOT", self.root
        ), patch.object(automation_api.content_ingest, "validate_request"), patch.object(
            automation_api.review_preparation, "enqueue"
        ) as enqueue:
            response = self.client.post("/v1/content/review", json=self._identity_payload())
        self.assertEqual(response.status_code, 200)
        enqueue.assert_not_called()

    def test_existing_legacy_review_ignores_render_only_defaults_change(self):
        job, metadata = self._identity_job()
        metadata["mpt_defaults"] = {
            "video_resolution": "720p", "video_transition_mode": "FadeIn",
        }
        job.joinpath("content.json").write_text(json.dumps(metadata), encoding="utf-8")
        self._identity_plan(metadata, legacy_batch_id=True)
        with patch.object(automation_api.content_ingest, "DEFAULT_JOB_ROOT", self.jobs), patch.object(
            automation_api.create_content_job_review.produce_batch, "HOST_ROOT", self.root
        ), patch.object(automation_api.content_ingest, "validate_request"), patch.object(
            automation_api.review_preparation, "enqueue"
        ) as enqueue:
            response = self.client.post("/v1/content/review", json=self._identity_payload())
        self.assertEqual(response.status_code, 200)
        enqueue.assert_not_called()

    def test_provenance_accepts_legacy_and_current_formats_but_rejects_partial_identity(self):
        _, metadata = self._identity_job()
        legacy_path = self._identity_plan(metadata)
        legacy = json.loads(legacy_path.read_text(encoding="utf-8"))
        self.assertTrue(automation_api._plan_provenance_matches(legacy, metadata, "cid_001"))
        legacy["content_job"]["audio_sha256"] = "wrong"
        self.assertFalse(automation_api._plan_provenance_matches(legacy, metadata, "cid_001"))

        current_path = self._current_plan(metadata)
        current = json.loads(current_path.read_text(encoding="utf-8"))
        self.assertTrue(automation_api._plan_provenance_matches(current, metadata, "cid_001"))

        invalid_cases = {
            "batch_id": "another-content",
            "task_id": "batch-content-other-niche-cid_002-editorial-title",
            "audio_path": "/MoneyPrinterTurbo/storage/content_jobs/test-niche/cid_002/source.mp3",
            "script_path": "/MoneyPrinterTurbo/storage/content_jobs/test-niche/cid_002/script.txt",
        }
        for field, value in invalid_cases.items():
            with self.subTest(field=field):
                altered = dict(current)
                altered[field] = value
                self.assertFalse(automation_api._plan_provenance_matches(altered, metadata, "cid_001"))

        self.assertFalse(automation_api._plan_provenance_matches(
            {"batch_id": "cid_001"}, metadata, "cid_001"
        ))

    def test_validate_pending_plan_accepts_valid_current_plan(self):
        _, metadata = self._identity_job()
        plan = self._current_plan(metadata)
        with patch.object(automation_api.content_ingest, "DEFAULT_JOB_ROOT", self.jobs):
            automation_api._validate_pending_plan(plan, "cid_001")

    def test_existing_valid_current_review_is_reused_without_regeneration(self):
        _, metadata = self._identity_job()
        self._current_plan(metadata)
        with patch.object(automation_api.content_ingest, "DEFAULT_JOB_ROOT", self.jobs), patch.object(
            automation_api.create_content_job_review.produce_batch, "HOST_ROOT", self.root
        ), patch.object(automation_api.content_ingest, "validate_request"), patch.object(
            automation_api.content_ingest, "ingest_content"
        ) as ingest, patch.object(
            automation_api.create_content_job_review, "create_content_job_review"
        ) as review:
            response = self.client.post("/v1/content/review", json=self._identity_payload())
        self.assertEqual(response.status_code, 200)
        ingest.assert_not_called()
        review.assert_not_called()

    def test_existing_review_ignores_hook_title_and_legacy_batch_representation(self):
        _, metadata = self._identity_job()
        self._identity_plan(metadata, legacy_batch_id=True)
        with patch.object(automation_api.content_ingest, "DEFAULT_JOB_ROOT", self.jobs), patch.object(
            automation_api.create_content_job_review.produce_batch, "HOST_ROOT", self.root
        ), patch.object(automation_api.content_ingest, "validate_request"), patch.object(
            automation_api.content_ingest, "ingest_content"
        ) as ingest:
            response = self.client.post("/v1/content/review", json=self._identity_payload(hook_title="Changed hook"))
        self.assertEqual(response.status_code, 200)
        ingest.assert_not_called()

    def test_existing_content_source_or_profile_conflicts_are_409(self):
        _, metadata = self._identity_job()
        self._identity_plan(metadata)
        cases = {
            "niche_id": "other-niche",
            "audio_file_id": "other-audio",
            "script_file_id": "other-script",
            "asset_profile": "GENERALES",
        }
        for field, value in cases.items():
            with self.subTest(field=field), patch.object(automation_api.content_ingest, "DEFAULT_JOB_ROOT", self.jobs), patch.object(
                automation_api.create_content_job_review.produce_batch, "HOST_ROOT", self.root
            ), patch.object(automation_api.content_ingest, "validate_request"), patch.object(
                automation_api.content_ingest, "ingest_content"
            ) as ingest:
                response = self.client.post("/v1/content/review", json=self._identity_payload(**{field: value}))
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["detail"], "content identity conflict")
            ingest.assert_not_called()

    def test_review_request_is_accepted_before_runner_resolves_ingest_conflicts(self):
        payload = {
            "niche_id": "test-niche", "content_id": "cid_001", "title": "A title",
            "audio_file_id": "audio-id", "script_file_id": "script-id", "asset_profile": "GENERALES",
        }
        with patch.object(automation_api.content_ingest, "validate_request"), patch.object(
            automation_api.content_ingest, "ingest_content"
        ) as ingest, patch.object(
            automation_api.review_preparation, "enqueue", return_value={"state": "pending"}
        ):
            response = self.client.post("/v1/content/review", json=payload)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], "PREPARING_REVIEW")
        ingest.assert_not_called()

    def test_underlying_failure_is_acknowledged_for_durable_runner(self):
        payload = {
            "niche_id": "test-niche", "content_id": "cid_001", "title": "A title",
            "audio_file_id": "audio-id", "script_file_id": "script-id", "asset_profile": "GENERALES",
        }
        with patch.object(automation_api.content_ingest, "validate_request"), patch.object(
            automation_api.content_ingest, "ingest_content"
        ) as ingest, patch.object(
            automation_api.review_preparation, "enqueue", return_value={"state": "pending"}
        ):
            response = self.client.post("/v1/content/review", json=payload)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], "PREPARING_REVIEW")
        ingest.assert_not_called()

    def test_review_adapter_failure_is_acknowledged_for_durable_runner(self):
        payload = {
            "niche_id": "test-niche", "content_id": "cid_001", "title": "A title",
            "audio_file_id": "audio-id", "script_file_id": "script-id", "asset_profile": "GENERALES",
        }
        metadata = {"niche_id": "test-niche", "content_id": "cid_001"}
        with patch.object(automation_api.content_ingest, "validate_request"), patch.object(
            automation_api.content_ingest, "ingest_content", return_value=metadata
        ), patch.object(
            automation_api.create_content_job_review, "create_content_job_review",
            side_effect=automation_api.create_content_job_review.ContentJobReviewError("Asset Hub unavailable"),
        ) as review, patch.object(
            automation_api.review_preparation, "enqueue", return_value={"state": "pending"}
        ):
            response = self.client.post("/v1/content/review", json=payload)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], "PREPARING_REVIEW")
        review.assert_not_called()

    def test_night_approved_content_enqueues_once(self):
        _, _, plan = self._approved_schedule_fixture()
        jobs_root, host_root = self._schedule_context()
        with jobs_root, host_root, patch.object(
            human_review, "approve_plan"
        ) as approve:
            first = self.client.post("/v1/content/cid_001/schedule", json={"run_mode": "NIGHT"})
            second = self.client.post("/v1/content/cid_001/schedule", json={"run_mode": "NIGHT"})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["production_state"], "queued_night")
        queue = self.root / "storage/nightly_jobs/pending"
        self.assertEqual(list(queue.glob("*.json")).__len__(), 1)
        self.assertEqual(json.loads(next(queue.glob("*.json")).read_text())["production_plan_path"], plan.as_posix())
        approve.assert_not_called()

    def test_night_schedule_accepts_approved_current_plan_and_queues_it(self):
        _, metadata = self._identity_job()
        plan = self._current_plan(metadata, review_status=human_review.STATUS_APPROVED)
        jobs_root, host_root = self._schedule_context()
        with jobs_root, host_root:
            response = self.client.post("/v1/content/cid_001/schedule", json={"run_mode": "NIGHT"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["production_state"], "queued_night")
        queued = list((self.root / "storage/nightly_jobs/pending").glob("*.json"))
        self.assertEqual(len(queued), 1)
        self.assertEqual(json.loads(queued[0].read_text())["production_plan_path"], plan.as_posix())

    def test_now_approved_content_persists_launching_and_never_launches_production(self):
        job, _, _ = self._approved_schedule_fixture()
        jobs_root, host_root = self._schedule_context()
        with jobs_root, host_root, patch("subprocess.Popen") as popen, patch.object(
            automation_api.create_content_job_review.produce_batch, "process_approved_review_plan"
        ) as produce:
            first = self.client.post("/v1/content/cid_001/schedule", json={"run_mode": "NOW"})
            second = self.client.post("/v1/content/cid_001/schedule", json={"run_mode": "NOW"})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["production_state"], "producing")
        self.assertEqual(second.json()["production_state"], "producing")
        schedule = json.loads((job / "production-schedule.json").read_text(encoding="utf-8"))
        self.assertEqual(schedule["production_state"], "launching")
        self.assertEqual(len(list(job.glob("production-schedule.json"))), 1)
        popen.assert_not_called()
        produce.assert_not_called()
        self.assertFalse((self.root / "storage/nightly_jobs/pending").exists())

    def test_explicit_now_retry_rearms_failed_schedule_and_cleans_obsolete_runtime_fields(self):
        job, _, plan = self._approved_schedule_fixture()
        original = {
            "content_id": "cid_001",
            "niche_id": "test-niche",
            "production_plan_path": plan.as_posix(),
            "production_state": "error",
            "pid": 123,
            "boot_id": "old-boot",
            "error": "render failed",
            "finished_at": "2026-08-28T00:00:00Z",
            "retry_metadata": {"keep": True},
        }
        (job / "production-schedule.json").write_text(json.dumps(original), encoding="utf-8")
        jobs_root, host_root = self._schedule_context()
        with jobs_root, host_root:
            response = self.client.post(
                "/v1/content/cid_001/schedule", json={"run_mode": "NOW", "retry": True},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["production_state"], "launching")
        schedule = json.loads((job / "production-schedule.json").read_text(encoding="utf-8"))
        self.assertEqual(schedule["production_state"], "launching")
        self.assertEqual(schedule["content_id"], "cid_001")
        self.assertEqual(schedule["niche_id"], "test-niche")
        self.assertEqual(schedule["production_plan_path"], plan.as_posix())
        self.assertEqual(schedule["retry_metadata"], {"keep": True})
        for field in ("pid", "boot_id", "error", "finished_at"):
            self.assertNotIn(field, schedule)

    def test_now_retry_is_idempotent_for_launching_producing_and_completed(self):
        job, _, plan = self._approved_schedule_fixture()
        for state in ("launching", "producing", "completed"):
            with self.subTest(state=state):
                original = {
                    "content_id": "cid_001",
                    "production_plan_path": plan.as_posix(),
                    "production_state": state,
                    "marker": state,
                }
                (job / "production-schedule.json").write_text(json.dumps(original), encoding="utf-8")
                jobs_root, host_root = self._schedule_context()
                with jobs_root, host_root:
                    response = self.client.post(
                        "/v1/content/cid_001/schedule", json={"run_mode": "NOW", "retry": True},
                    )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["production_state"], state)
                self.assertEqual(
                    json.loads((job / "production-schedule.json").read_text(encoding="utf-8")), original,
                )

    def test_now_retry_without_approved_plan_is_rejected_without_changing_schedule(self):
        job, metadata = self._identity_job()
        self._identity_plan(metadata, review_status=human_review.STATUS_PENDING)
        original = {"production_state": "error", "pid": 123}
        record = job / "production-schedule.json"
        record.write_text(json.dumps(original), encoding="utf-8")
        jobs_root, host_root = self._schedule_context()
        with jobs_root, host_root:
            response = self.client.post(
                "/v1/content/cid_001/schedule", json={"run_mode": "NOW", "retry": True},
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(json.loads(record.read_text(encoding="utf-8")), original)

    def test_night_retry_is_rejected_without_changing_failed_schedule(self):
        job, _, plan = self._approved_schedule_fixture()
        original = {"content_id": "cid_001", "production_plan_path": plan.as_posix(), "production_state": "error"}
        record = job / "production-schedule.json"
        record.write_text(json.dumps(original), encoding="utf-8")
        jobs_root, host_root = self._schedule_context()
        with jobs_root, host_root:
            response = self.client.post(
                "/v1/content/cid_001/schedule", json={"run_mode": "NIGHT", "retry": True},
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(record.read_text(encoding="utf-8")), original)

    def test_reconcile_error_does_not_auto_retry_failed_now_schedule(self):
        job, _, plan = self._approved_schedule_fixture()
        original = {
            "content_id": "cid_001",
            "production_plan_path": plan.as_posix(),
            "production_state": "error",
            "pid": 123,
            "error": "render failed",
        }
        record = job / "production-schedule.json"
        record.write_text(json.dumps(original), encoding="utf-8")
        jobs_root, host_root = self._schedule_context()
        with patch.object(automation_api, "_validate_enabled_niche"), jobs_root, host_root:
            response = self._reconcile(status="ERROR", run_mode="NOW")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ERROR")
        self.assertEqual(json.loads(record.read_text(encoding="utf-8")), original)

    def test_now_promotes_only_matching_pending_nightly_job_to_held_once(self):
        job, _, plan = self._approved_schedule_fixture()
        _, _, other_plan = self._approved_schedule_fixture("cid_002")
        pending = self.root / "storage/nightly_jobs/pending"
        pending.mkdir(parents=True)
        exact = pending / "exact.json"
        exact_payload = self._review_queue_job(plan, "cid_001")
        exact.write_text(json.dumps(exact_payload), encoding="utf-8")
        unrelated = pending / "unrelated.json"
        unrelated_payload = self._review_queue_job(other_plan, "cid_002")
        unrelated.write_text(json.dumps(unrelated_payload), encoding="utf-8")
        jobs_root, host_root = self._schedule_context()
        with jobs_root, host_root, patch.object(
            automation_api, "_nightly_queue_dir", return_value=pending.parent,
        ), patch("subprocess.Popen") as popen:
            first = self.client.post("/v1/content/cid_001/schedule", json={"run_mode": "NOW"})
            second = self.client.post("/v1/content/cid_001/schedule", json={"run_mode": "NOW"})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["schedule_state"], "started_now")
        self.assertEqual(first.json()["promoted_from"], "queued_night")
        self.assertFalse(exact.exists())
        self.assertEqual(json.loads(unrelated.read_text(encoding="utf-8")), unrelated_payload)
        held = list((self.root / "storage/nightly_jobs/held").glob("*.json"))
        self.assertEqual(len(held), 1)
        self.assertEqual(json.loads(held[0].read_text(encoding="utf-8")), exact_payload)
        self.assertEqual(json.loads((job / "production-schedule.json").read_text())["production_state"], "launching")
        popen.assert_not_called()
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["production_state"], "producing")

    def test_now_does_not_promote_a_nightly_job_already_processing(self):
        _, _, plan = self._approved_schedule_fixture()
        processing = self.root / "storage/nightly_jobs/processing/run-1"
        processing.mkdir(parents=True)
        (processing / "job.json").write_text(
            json.dumps(self._review_queue_job(plan, "cid_001")), encoding="utf-8",
        )
        jobs_root, host_root = self._schedule_context()
        with jobs_root, host_root, patch.object(
            automation_api, "_nightly_queue_dir", return_value=processing.parent.parent,
        ):
            response = self.client.post("/v1/content/cid_001/schedule", json={"run_mode": "NOW"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["schedule_state"], "already_processing")
        self.assertEqual(response.json()["production_state"], "producing")

    def test_reconcile_completed_without_delivery_is_error(self):
        job, _, _ = self._approved_schedule_fixture()
        (job / "production-schedule.json").write_text(json.dumps({"production_state": "completed"}), encoding="utf-8")
        jobs_root, host_root = self._schedule_context()
        with patch.object(automation_api, "_validate_enabled_niche"), jobs_root, host_root:
            response = self._reconcile(status="COMPLETED", run_mode="NOW")
        self.assertEqual(response.json()["status"], "ERROR")
        self.assertEqual(response.json()["error"], "delivery incomplete")

    def test_schedule_rejects_unapproved_invalid_unknown_and_provenance_conflict(self):
        _, metadata = self._identity_job()
        self._identity_plan(metadata)
        jobs_root, host_root = self._schedule_context()
        with jobs_root, host_root:
            unapproved = self.client.post("/v1/content/cid_001/schedule", json={"run_mode": "NIGHT"})
            invalid = self.client.post("/v1/content/cid_001/schedule", json={"run_mode": "MORNING"})
            unknown = self.client.post("/v1/content/missing/schedule", json={"run_mode": "NIGHT"})
        self.assertEqual(unapproved.status_code, 409)
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(unknown.status_code, 404)

        _, metadata, plan = self._approved_schedule_fixture("cid_002")
        payload = json.loads(plan.read_text())
        payload["content_job"]["audio_sha256"] = "wrong"
        plan.write_text(json.dumps(payload), encoding="utf-8")
        with self._schedule_context()[0], self._schedule_context()[1]:
            conflict = self.client.post("/v1/content/cid_002/schedule", json={"run_mode": "NIGHT"})
        self.assertEqual(conflict.status_code, 409)

    def test_status_reports_nightly_running_and_completed_states(self):
        job, _, plan = self._approved_schedule_fixture()
        queue_job = {
            "render_mode": human_review.RENDER_MODE,
            "production_plan_path": plan.as_posix(),
        }
        processing = self.root / "storage/nightly_jobs/processing/run-1"
        processing.mkdir(parents=True)
        (processing / "job.json").write_text(json.dumps(queue_job), encoding="utf-8")
        jobs_root, host_root = self._schedule_context()
        with jobs_root, host_root:
            running = self.client.get("/v1/content/cid_001")
        self.assertEqual(running.json()["production_state"], "producing")
        (job / "production-schedule.json").write_text(json.dumps({"production_state": "completed"}), encoding="utf-8")
        with self._schedule_context()[0], self._schedule_context()[1]:
            completed = self.client.get("/v1/content/cid_001")
        self.assertEqual(completed.json()["production_state"], "completed")


if __name__ == "__main__":
    unittest.main()
