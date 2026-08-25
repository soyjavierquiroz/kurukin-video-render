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

    def _approved_schedule_fixture(self, content_id="cid_001"):
        job, metadata = self._identity_job(content_id)
        plan = self._identity_plan(metadata, review_status=human_review.STATUS_APPROVED)
        return job, metadata, plan

    def _schedule_context(self):
        return patch.object(automation_api.content_ingest, "DEFAULT_JOB_ROOT", self.jobs), patch.object(
            automation_api.create_content_job_review.produce_batch, "HOST_ROOT", self.root,
        )

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
        plan = self._plan()
        payload = {
            "niche_id": "test-niche", "content_id": "cid_001", "title": "A title",
            "audio_file_id": "audio-id", "script_file_id": "script-id", "asset_profile": "GENERALES",
        }
        metadata = {"niche_id": "test-niche", "content_id": "cid_001"}
        with patch.object(automation_api.content_ingest, "validate_request"), patch.object(
            automation_api.content_ingest, "ingest_content", return_value=metadata
        ) as ingest, patch.object(
            automation_api.create_content_job_review, "create_content_job_review", return_value=("already_exists", plan)
        ) as review, patch.object(human_review, "approve_plan") as approve, patch.object(
            automation_api.create_content_job_review.produce_batch, "process_job"
        ) as production:
            response = self.client.post("/v1/content/review", json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["review_status"], "pending_review")
        ingest.assert_called_once()
        review.assert_called_once()
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
        ) as process:
            response = self.client.post("/v1/content/review", json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "ok": True, "content_id": "cid_001", "niche_id": "test-niche",
            "review_status": "pending_review", "review_relative_url": "/?content_id=cid_001",
        })
        ingest.assert_not_called()
        review.assert_not_called()
        process.assert_not_called()
        approve.assert_not_called()

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

    def test_identity_conflict_is_409(self):
        payload = {
            "niche_id": "test-niche", "content_id": "cid_001", "title": "A title",
            "audio_file_id": "audio-id", "script_file_id": "script-id", "asset_profile": "GENERALES",
        }
        conflict = automation_api.content_ingest.ContentIngestError(
            "content_id already exists with different source Drive IDs; refusing to overwrite content identity"
        )
        with patch.object(automation_api.content_ingest, "validate_request"), patch.object(
            automation_api.content_ingest, "ingest_content", side_effect=conflict
        ):
            response = self.client.post("/v1/content/review", json=payload)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], "content identity conflict")

    def test_underlying_failure_is_a_safe_500(self):
        payload = {
            "niche_id": "test-niche", "content_id": "cid_001", "title": "A title",
            "audio_file_id": "audio-id", "script_file_id": "script-id", "asset_profile": "GENERALES",
        }
        with patch.object(automation_api.content_ingest, "validate_request"), patch.object(
            automation_api.content_ingest, "ingest_content", side_effect=RuntimeError("secret=never-returned")
        ):
            response = self.client.post("/v1/content/review", json=payload)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["detail"], "unable to prepare human review")
        self.assertNotIn("secret", response.text)

    def test_review_adapter_failure_is_a_safe_500(self):
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
        ):
            response = self.client.post("/v1/content/review", json=payload)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["detail"], "review creation failed")

    def test_night_approved_content_enqueues_once_and_never_starts_immediate_production(self):
        _, _, plan = self._approved_schedule_fixture()
        jobs_root, host_root = self._schedule_context()
        with jobs_root, host_root, patch.object(automation_api, "_launch_immediate_production") as launch, patch.object(
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
        launch.assert_not_called()
        approve.assert_not_called()

    def test_now_approved_content_launches_canonical_producer_once_without_nightly_enqueue(self):
        job, _, _ = self._approved_schedule_fixture()
        jobs_root, host_root = self._schedule_context()
        with jobs_root, host_root, patch.object(automation_api, "_launch_immediate_production") as launch:
            first = self.client.post("/v1/content/cid_001/schedule", json={"run_mode": "NOW"})
            second = self.client.post("/v1/content/cid_001/schedule", json={"run_mode": "NOW"})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["production_state"], "producing")
        launch.assert_called_once_with(job / "production-schedule.json")
        self.assertFalse((self.root / "storage/nightly_jobs/pending").exists())

    def test_now_worker_uses_canonical_approved_plan_producer(self):
        job, _, plan = self._approved_schedule_fixture()
        record = job / "production-schedule.json"
        record.write_text(json.dumps({"content_id": "cid_001", "production_plan_path": plan.as_posix()}), encoding="utf-8")
        with patch.object(
            automation_api.create_content_job_review.produce_batch,
            "process_approved_review_plan", return_value="completed",
        ) as produce:
            self.assertEqual(automation_api._run_immediate_production(record), 0)
        produce.assert_called_once_with(plan)
        self.assertEqual(json.loads(record.read_text())["production_state"], "completed")

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
