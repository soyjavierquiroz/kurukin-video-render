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


if __name__ == "__main__":
    unittest.main()
