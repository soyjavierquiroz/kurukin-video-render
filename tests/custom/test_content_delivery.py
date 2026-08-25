"""Offline tests for the completed Human Review batch delivery sidecar."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import call, patch

from app.custom import human_review
from scripts import content_delivery, nightly_runner


class ContentDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.content_id = "cid_001"
        self.niche_id = "test-niche"
        self.job = self.root / "storage/content_jobs" / self.niche_id / self.content_id
        self.job.mkdir(parents=True)
        (self.job / "content.json").write_text(json.dumps({
            "content_id": self.content_id, "niche_id": self.niche_id,
        }), encoding="utf-8")
        self.plan = self.root / "plan.json"
        self.plan.write_text(json.dumps({
            "review_status": human_review.STATUS_APPROVED,
            "batch_id": "batch-1", "stem": "video-1", "script_path": (self.root / "script.txt").as_posix(),
            "content_job": {"content_id": self.content_id, "niche_id": self.niche_id},
        }), encoding="utf-8")
        self.video = self.root / "storage/batch_outputs/batch-1/video-1.mp4"
        self.video.parent.mkdir(parents=True)
        self.video.write_bytes(b"not-a-real-video-but-validation-is-mocked")
        report = self.root / "storage/batch_outputs/batch-1/batch-report.json"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps({"jobs": {"video-1": {
            "status": "completed", "batch_final": self.video.as_posix(),
        }}}), encoding="utf-8")
        self.root_patch = patch.object(content_delivery, "PROJECT_ROOT", self.root)
        self.root_patch.start()

    def tearDown(self):
        self.root_patch.stop()
        self.tmp.cleanup()

    def _finalize(self, *, lsjson='{"ID": "drive-file-1"}'):
        completed = [
            CompletedProcess([], 0, "", ""),
            CompletedProcess([], 0, lsjson, ""),
        ]
        with patch.object(content_delivery, "valid_mp4", return_value=True), patch.object(
            content_delivery, "load_niche", return_value={
                "rclone_remote": "deliveries:", "final_drive_folder_id": "folder-1",
            },
        ), patch.object(content_delivery.subprocess, "run", side_effect=completed) as run:
            payload = content_delivery.finalize_production_plan(self.plan)
        return payload, run

    def test_finalization_uploads_completed_batch_final_and_writes_sidecar(self):
        payload, run = self._finalize()
        self.assertEqual(payload["checksum"], content_delivery.sha256_file(self.video))
        self.assertEqual(json.loads((self.job / "delivery.json").read_text()), payload)
        self.assertEqual(run.call_args_list, [
            call(["rclone", "copyto", self.video.as_posix(), "deliveries:cid_001.mp4", "--drive-root-folder-id", "folder-1"], check=True, capture_output=True, text=True),
            call(["rclone", "lsjson", "deliveries:cid_001.mp4", "--stat", "--drive-root-folder-id", "folder-1"], check=True, capture_output=True, text=True),
        ])
        self.assertEqual(payload["final_drive_url"], "https://drive.google.com/file/d/drive-file-1/view")

    def test_valid_sidecar_is_idempotent_and_stale_checksum_refinalizes(self):
        payload, _ = self._finalize()
        with patch.object(content_delivery, "valid_mp4", return_value=True), patch.object(content_delivery.subprocess, "run") as run:
            self.assertEqual(content_delivery.finalize_production_plan(self.plan), payload)
        run.assert_not_called()
        payload["checksum"] = "0" * 64
        (self.job / "delivery.json").write_text(json.dumps(payload), encoding="utf-8")
        _, run = self._finalize()
        self.assertEqual(run.call_count, 2)

    def test_incomplete_report_or_metadata_without_id_fails(self):
        report = self.root / "storage/batch_outputs/batch-1/batch-report.json"
        report.write_text(json.dumps({"jobs": {"video-1": {"status": "pending"}}}), encoding="utf-8")
        with patch.object(content_delivery, "valid_mp4", return_value=True):
            with self.assertRaises(content_delivery.DeliveryError):
                content_delivery.finalize_production_plan(self.plan)
        report.write_text(json.dumps({"jobs": {"video-1": {
            "status": "completed", "batch_final": self.video.as_posix(),
        }}}), encoding="utf-8")
        with self.assertRaises(content_delivery.DeliveryError):
            self._finalize(lsjson="{}")

    def test_paths_cannot_escape_content_or_relevant_batch_directory(self):
        outside = self.root / "outside.mp4"
        outside.write_bytes(b"outside")
        report = self.root / "storage/batch_outputs/batch-1/batch-report.json"
        report.write_text(json.dumps({"jobs": {"video-1": {
            "status": "completed", "batch_final": outside.as_posix(),
        }}}), encoding="utf-8")
        with patch.object(content_delivery, "valid_mp4", return_value=True):
            with self.assertRaises(content_delivery.DeliveryError):
                content_delivery.finalize_production_plan(self.plan)

        plan = json.loads(self.plan.read_text(encoding="utf-8"))
        plan["content_job"]["content_id"] = "../outside"
        self.plan.write_text(json.dumps(plan), encoding="utf-8")
        with self.assertRaises(content_delivery.DeliveryError):
            content_delivery.finalize_production_plan(self.plan)

        plan["content_job"] = {"content_id": self.content_id, "niche_id": self.niche_id}
        plan["batch_id"] = "../outside"
        self.plan.write_text(json.dumps(plan), encoding="utf-8")
        with self.assertRaises(content_delivery.DeliveryError):
            content_delivery.finalize_production_plan(self.plan)

        plan["batch_id"] = "batch-1"
        plan["content_job"]["niche_id"] = "../outside"
        self.plan.write_text(json.dumps(plan), encoding="utf-8")
        with self.assertRaises(content_delivery.DeliveryError):
            content_delivery.finalize_production_plan(self.plan)

    def test_content_identity_metadata_mismatch_fails(self):
        (self.job / "content.json").write_text(json.dumps({
            "content_id": self.content_id, "niche_id": "other-niche",
        }), encoding="utf-8")
        with self.assertRaises(content_delivery.DeliveryError):
            content_delivery.finalize_production_plan(self.plan)

    def test_read_delivery_rejects_malformed_checksum_markdown_url_and_identity_mismatch(self):
        sidecar = self.job / "delivery.json"
        payload = {
            "content_id": self.content_id, "niche_id": self.niche_id,
            "final_drive_file_id": "file-1",
            "final_drive_url": "https://drive.google.com/file/d/file-1/view",
            "checksum": "a" * 64,
        }
        sidecar.write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual(content_delivery.read_delivery(sidecar, content_id=self.content_id), payload)
        payload["checksum"] = "not-a-checksum"
        sidecar.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(content_delivery.DeliveryError):
            content_delivery.read_delivery(sidecar)
        payload["checksum"] = "a" * 64
        payload["final_drive_url"] = "[https://drive.google.com/file/d/file-1/view](https://drive.google.com/file/d/file-1/view)"
        sidecar.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(content_delivery.DeliveryError):
            content_delivery.read_delivery(sidecar)
        payload["final_drive_url"] = "https://drive.google.com/file/d/file-1/view"
        payload["content_id"] = "other"
        sidecar.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(content_delivery.DeliveryError):
            content_delivery.read_delivery(sidecar, content_id=self.content_id)

    def test_nightly_delivers_only_after_completed_render_and_propagates_delivery_error(self):
        reserved = self.root / "reserved"
        reserved.mkdir()
        job = {"render_mode": "human_review_batch", "production_plan_path": self.plan.as_posix()}
        with patch("scripts.produce_batch.process_approved_review_plan", return_value="completed") as render, patch(
            "scripts.content_delivery.finalize_production_plan", return_value={"checksum": "x"},
        ) as deliver:
            result = nightly_runner.handle_human_review_batch_job(job, reserved)
        self.assertTrue(result["ok"])
        render.assert_called_once()
        deliver.assert_called_once_with(self.plan.as_posix())
        with patch("scripts.produce_batch.process_approved_review_plan", return_value="completed"), patch(
            "scripts.content_delivery.finalize_production_plan", side_effect=RuntimeError("offline"),
        ):
            with self.assertRaises(RuntimeError):
                nightly_runner.handle_human_review_batch_job(job, reserved)


if __name__ == "__main__":
    unittest.main()
