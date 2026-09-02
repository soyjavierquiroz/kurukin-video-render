"""Offline durability tests for asynchronous Human Review preparation."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from app.custom.kurukin_asset_hub import KurukinAssetHubUnavailableError
from scripts import content_ingest, host_execution_runner, review_preparation


class AsyncReviewPreparationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.jobs = self.root / "storage" / "content_jobs"
        self.payload = {"niche_id": "niche", "content_id": "cid", "title": "Title", "audio_file_id": "audio", "script_file_id": "script", "asset_profile": "PROFILE"}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _path(self) -> Path:
        return review_preparation.state_path("niche", "cid", job_root=self.jobs)

    def _canonical_plan(self, content_id: str = "cid") -> dict[str, str]:
        return {
            "review_status": "pending_review",
            "batch_id": content_id,
            "task_id": f"batch-content-niche-{content_id}-Title",
            "audio_path": f"/MoneyPrinterTurbo/storage/content_jobs/niche/{content_id}/source.mp3",
            "script_path": f"/MoneyPrinterTurbo/storage/content_jobs/niche/{content_id}/script.txt",
        }

    def _exclusive_empty_plan(self, *, unavailable: bool) -> dict:
        plan = self._canonical_plan()
        plan.update({
            "material_source_policy": {
                "providers": {"enabled": ["asset_hub"]},
                "asset_hub": {"include": {"titles": ["mi-otra-yo"]}},
            },
            "asset_hub_source_policy": {
                "sources": [{"scope": "title", "title": "mi-otra-yo"}],
            },
            "source_policy": "title-exclusive",
            "provider_diagnostics": [{
                "provider": "asset_hub",
                "status": "unavailable" if unavailable else "empty",
                "candidate_count": 0,
                "error_class": "KurukinAssetHubUnavailableError" if unavailable else "",
                "error": "transport detail must remain plan-only",
            }],
            "segments": [{
                "selected_asset": None,
                "alternatives": [],
                "backup_assets": [],
                "warnings": [{"code": "missing_primary"}],
            }],
        })
        return plan

    def test_enqueue_is_durable_and_idempotent_without_execution(self) -> None:
        with patch.object(review_preparation.content_ingest, "ingest_content") as ingest, patch.object(review_preparation.create_content_job_review, "create_content_job_review") as create:
            first = review_preparation.enqueue(self.payload, job_root=self.jobs)
            second = review_preparation.enqueue(self.payload, job_root=self.jobs)
        self.assertEqual(first["state"], "pending")
        self.assertEqual(second["state"], "pending")
        self.assertEqual(len(list(self.jobs.glob("*/*/review-preparation.json"))), 1)
        ingest.assert_not_called(); create.assert_not_called()

    def test_changed_video_terms_requeues_preparation_but_same_terms_are_idempotent(self) -> None:
        original = {**self.payload, "video_terms": "café"}
        first = review_preparation.enqueue(original, job_root=self.jobs)
        same = review_preparation.enqueue(original, job_root=self.jobs)
        changed = review_preparation.enqueue({**original, "video_terms": "barista"}, job_root=self.jobs)
        self.assertEqual((first["state"], same["state"]), ("pending", "pending"))
        self.assertEqual((changed["state"], changed["video_terms"], changed["attempt"]), ("pending", "barista", 0))

    def test_runner_success_marks_completed(self) -> None:
        review_preparation.enqueue(self.payload, job_root=self.jobs)
        plan = self.root / "plan.json"; plan.write_text(json.dumps(self._canonical_plan()), encoding="utf-8")
        with patch.object(review_preparation.content_ingest, "ingest_content", return_value=self.payload), patch.object(review_preparation.create_content_job_review, "create_content_job_review", return_value=("created", plan)):
            result = review_preparation.run_record(self._path(), boot_id="boot", pid=1)
        self.assertEqual(result["action"], "completed")
        self.assertEqual(json.loads(self._path().read_text())["state"], "completed")

    def test_runner_canonical_plan_without_content_job_marks_completed(self) -> None:
        review_preparation.enqueue(self.payload, job_root=self.jobs)
        plan = self.root / "plan.json"
        plan.write_text(json.dumps(self._canonical_plan()), encoding="utf-8")
        with patch.object(
            review_preparation.content_ingest, "ingest_content", return_value=self.payload,
        ), patch.object(
            review_preparation.create_content_job_review, "create_content_job_review", return_value=("created", plan),
        ):
            result = review_preparation.run_record(self._path(), boot_id="boot", pid=1)
        self.assertEqual(result["action"], "completed")
        self.assertEqual(json.loads(self._path().read_text())["state"], "completed")

    def test_title_exclusive_unavailable_with_no_material_fails_closed(self) -> None:
        """A plan file alone must not make an empty exclusive review reachable."""
        review_preparation.enqueue(self.payload, job_root=self.jobs)
        plan = self.root / "plan.json"
        plan.write_text(json.dumps(self._exclusive_empty_plan(unavailable=True)), encoding="utf-8")

        with patch.object(review_preparation.content_ingest, "ingest_content", return_value=self.payload), patch.object(
            review_preparation.create_content_job_review, "create_content_job_review", return_value=("created", plan),
        ):
            result = review_preparation.run_record(self._path(), boot_id="boot", pid=1)

        record = json.loads(self._path().read_text())
        self.assertEqual(result["action"], "error")
        self.assertEqual(record["state"], "error")
        self.assertEqual(review_preparation.public_state(record), "ERROR")
        self.assertEqual(
            record["last_error_message"],
            "Exclusive asset source is unavailable; no review material could be prepared.",
        )
        self.assertNotIn("transport detail", record["last_error_message"])
        persisted = json.loads(plan.read_text(encoding="utf-8"))
        self.assertEqual(persisted["provider_diagnostics"][0]["error_class"], "KurukinAssetHubUnavailableError")
        self.assertEqual(persisted["material_source_policy"]["providers"]["enabled"], ["asset_hub"])
        self.assertEqual(persisted["asset_hub_source_policy"]["sources"], [{"scope": "title", "title": "mi-otra-yo"}])

    def test_title_exclusive_empty_provider_returns_safe_no_material_error(self) -> None:
        review_preparation.enqueue(self.payload, job_root=self.jobs)
        plan = self.root / "plan.json"
        plan.write_text(json.dumps(self._exclusive_empty_plan(unavailable=False)), encoding="utf-8")
        with patch.object(review_preparation.content_ingest, "ingest_content", return_value=self.payload), patch.object(
            review_preparation.create_content_job_review, "create_content_job_review", return_value=("created", plan),
        ):
            review_preparation.run_record(self._path(), boot_id="boot", pid=1)
        record = json.loads(self._path().read_text())
        self.assertEqual(record["state"], "error")
        self.assertEqual(
            record["last_error_message"],
            "Exclusive asset source returned no usable material; no review material could be prepared.",
        )

    def test_open_generales_plan_with_stock_material_still_completes(self) -> None:
        review_preparation.enqueue(self.payload, job_root=self.jobs)
        plan = self.root / "plan.json"
        payload = self._canonical_plan()
        payload.update({
            "material_source_policy": {"providers": {"enabled": ["asset_hub", "pexels", "pixabay"]}},
            "asset_hub_source_policy": {"sources": [{"scope": "generic"}]},
            "provider_diagnostics": [
                {"provider": "asset_hub", "status": "unavailable", "candidate_count": 0},
                {"provider": "pexels", "status": "success", "candidate_count": 1},
                {"provider": "pixabay", "status": "success", "candidate_count": 1},
            ],
            "segments": [{"selected_asset": {
                "asset_uid": "pexels-1",
                "preview": {"status": "available", "type": "url", "value": "https://example.test/preview.jpg"},
            }, "alternatives": []}],
        })
        plan.write_text(json.dumps(payload), encoding="utf-8")
        with patch.object(review_preparation.content_ingest, "ingest_content", return_value=self.payload), patch.object(
            review_preparation.create_content_job_review, "create_content_job_review", return_value=("created", plan),
        ):
            result = review_preparation.run_record(self._path(), boot_id="boot", pid=1)
        self.assertEqual(result["action"], "completed")
        self.assertEqual(json.loads(self._path().read_text())["state"], "completed")

    def test_uninspectable_primary_fails_closed_and_projects_safe_error(self) -> None:
        review_preparation.enqueue(self.payload, job_root=self.jobs)
        plan = self.root / "plan.json"
        payload = self._canonical_plan()
        payload["segments"] = [{"selected_asset": {
            "asset_uid": "diagnostic-only",
            "preview": {"status": "unavailable", "type": "none", "value": ""},
        }, "alternatives": []}]
        plan.write_text(json.dumps(payload), encoding="utf-8")
        with patch.object(review_preparation.content_ingest, "ingest_content", return_value=self.payload), patch.object(
            review_preparation.create_content_job_review, "create_content_job_review", return_value=("created", plan),
        ):
            result = review_preparation.run_record(self._path(), boot_id="boot", pid=1)
        record = json.loads(self._path().read_text())
        self.assertEqual(result["action"], "error")
        self.assertEqual(record["state"], "error")
        self.assertEqual(
            record["last_error_message"],
            "No inspectable visual candidate is available for one or more review segments.",
        )
        self.assertEqual(
            review_preparation.sheet_error_message(record),
            "Human Review preparation failed: No inspectable visual candidate is available for one or more review segments.",
        )

    def test_transient_failure_retries_then_eventually_completes(self) -> None:
        review_preparation.enqueue(self.payload, job_root=self.jobs)
        transient = KurukinAssetHubUnavailableError("offline")
        with patch.object(review_preparation.content_ingest, "ingest_content", side_effect=transient):
            first = review_preparation.run_record(self._path(), boot_id="boot", pid=1)
        record = json.loads(self._path().read_text())
        self.assertEqual(first["action"], "retry_wait"); self.assertEqual(record["attempt"], 1)
        record["next_retry_at"] = "2000-01-01T00:00:00+00:00"; self._path().write_text(json.dumps(record), encoding="utf-8")
        plan = self.root / "plan.json"; plan.write_text(json.dumps(self._canonical_plan()), encoding="utf-8")
        with patch.object(review_preparation.content_ingest, "ingest_content", return_value=self.payload), patch.object(review_preparation.create_content_job_review, "create_content_job_review", return_value=("created", plan)):
            second = review_preparation.run_record(self._path(), boot_id="boot", pid=1)
        self.assertEqual(second["action"], "completed")

    def test_terminal_failure_does_not_retry(self) -> None:
        review_preparation.enqueue(self.payload, job_root=self.jobs)
        conflict = content_ingest.ContentIngestError(
            "content_id already exists with different source Drive IDs; refusing to overwrite content identity"
        )
        with patch.object(review_preparation.content_ingest, "ingest_content", side_effect=conflict) as ingest, patch.object(
            review_preparation.create_content_job_review, "create_content_job_review"
        ) as create:
            result = review_preparation.run_record(self._path(), boot_id="boot", pid=1)
        record = json.loads(self._path().read_text())
        self.assertEqual(result["action"], "error"); self.assertEqual(record["state"], "error")
        self.assertEqual(record["last_error_class"], "ContentIngestError")
        self.assertEqual(record["last_error_message"], str(conflict))
        self.assertFalse(review_preparation.due(record))
        ingest.assert_called_once(); create.assert_not_called()

    def test_adapter_terminal_failure_is_recorded_without_a_plan(self) -> None:
        review_preparation.enqueue(self.payload, job_root=self.jobs)
        failure = review_preparation.create_content_job_review.ContentJobReviewError("adapter misconfigured")
        with patch.object(review_preparation.content_ingest, "ingest_content", return_value=self.payload), patch.object(
            review_preparation.create_content_job_review, "create_content_job_review", side_effect=failure
        ) as create:
            result = review_preparation.run_record(self._path(), boot_id="boot", pid=1)
        record = json.loads(self._path().read_text())
        self.assertEqual(result["action"], "error")
        self.assertEqual(record["state"], "error")
        self.assertEqual(record["last_error_class"], "ContentJobReviewError")
        self.assertFalse(review_preparation.due(record))
        self.assertFalse(any(self.jobs.glob("**/plan.json")))
        create.assert_called_once()

    def test_unexpected_ingest_failure_is_sanitized_and_terminal(self) -> None:
        review_preparation.enqueue(self.payload, job_root=self.jobs)
        with self.assertLogs("scripts.review_preparation", level="ERROR") as logs, patch.object(
            review_preparation.content_ingest, "ingest_content",
            side_effect=RuntimeError("failure token=SUPER_SECRET"),
        ):
            result = review_preparation.run_record(self._path(), boot_id="boot", pid=1)
        record = json.loads(self._path().read_text())
        self.assertEqual(result["action"], "error")
        self.assertEqual(record["state"], "error")
        self.assertEqual(record["last_error_message"], "<redacted>")
        self.assertEqual(review_preparation.public_state(record), "ERROR")
        self.assertNotIn("SUPER_SECRET", json.dumps(record))
        self.assertNotIn("SUPER_SECRET", "\n".join(logs.output))

    def test_runner_recovers_stale_running_record(self) -> None:
        record = review_preparation.enqueue(self.payload, job_root=self.jobs)
        record.update({"state": "running", "boot_id": "old", "pid": 999999})
        self._path().write_text(json.dumps(record), encoding="utf-8")
        with patch.object(host_execution_runner, "current_boot_id", return_value="new"), patch.object(host_execution_runner, "pid_is_alive", return_value=False), patch.object(review_preparation, "run_record", return_value={"content_id": "cid", "niche_id": "niche", "action": "completed", "attempt": "1", "elapsed_ms": "1", "error_class": ""}) as run:
            decisions = host_execution_runner.reconcile_review_preparations(project_root=self.root)
        run.assert_called_once()
        self.assertTrue(any(item["action"] == "stale" for item in decisions))

    def test_timeout_releases_single_slot_for_a_later_job(self) -> None:
        first_payload = {**self.payload, "content_id": "a"}
        second_payload = {**self.payload, "content_id": "b"}
        first_path = review_preparation.state_path("niche", "a", job_root=self.jobs)
        review_preparation.enqueue(first_payload, job_root=self.jobs)
        review_preparation.enqueue(second_payload, job_root=self.jobs)
        plan = self.root / "plan.json"; plan.write_text(json.dumps(self._canonical_plan("b")), encoding="utf-8")
        with patch.object(
            review_preparation.content_ingest,
            "ingest_content",
            side_effect=[KurukinAssetHubUnavailableError("offline"), second_payload],
        ), patch.object(
            review_preparation.create_content_job_review,
            "create_content_job_review",
            return_value=("created", plan),
        ):
            first = host_execution_runner.reconcile_review_preparations(project_root=self.root)
            second = host_execution_runner.reconcile_review_preparations(project_root=self.root)
        self.assertTrue(any(item.get("content_id") == "a" and item["action"] == "retry_wait" for item in first))
        self.assertTrue(any(item.get("content_id") == "b" and item["action"] == "completed" for item in second))
        self.assertEqual(json.loads(first_path.read_text())["state"], "retry_wait")
