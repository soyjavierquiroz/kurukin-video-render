"""Offline contract tests for the content-job Human Review adapter."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.custom import human_review
from scripts import create_content_job_review as adapter
from scripts import produce_batch


class ContentJobHumanReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.registry = self.root / "niches.json"
        self._write_registry(["MI_OTRA_YO", "GENERALES", "ROMPIENDO_CIRCULO"])

    def tearDown(self):
        self.tmp.cleanup()

    def _write_registry(self, profiles):
        self.registry.write_text(json.dumps({"version": 1, "niches": {"test-niche": {
            "sheet_id": "sheet", "rclone_remote": "remote", "final_drive_folder_id": "folder",
            "default_asset_profile": profiles[0], "allowed_asset_profiles": profiles,
        }}}), encoding="utf-8")

    def _job(self, *, title="A title", profile="MI_OTRA_YO"):
        path = self.root / "jobs" / "test-niche" / "cid_001"
        path.mkdir(parents=True)
        audio, script = path / "source.mp3", path / "script.txt"
        audio.write_bytes(b"audio")
        script.write_text("script", encoding="utf-8")
        data = {"content_id": "cid_001", "niche_id": "test-niche", "title": title,
                "asset_profile": profile, "audio_sha256": hashlib.sha256(b"audio").hexdigest(),
                "script_sha256": hashlib.sha256(b"script").hexdigest()}
        (path / "content.json").write_text(json.dumps(data), encoding="utf-8")
        return path, data

    def _fake_process(self, job, **_kwargs):
        plan = human_review.plan_path(job.batch_id, job.stem, self.root)
        human_review.write_json_atomic(plan, {"batch_id": job.batch_id, "stem": job.stem,
                                              "review_status": human_review.STATUS_PENDING})
        return human_review.STATUS_PENDING

    def _create(self, job):
        with patch.object(produce_batch, "HOST_ROOT", self.root), patch.object(
            produce_batch, "process_job", side_effect=self._fake_process
        ) as process:
            result = adapter.create_content_job_review(job, registry_path=self.registry)
        return result, process

    def test_direct_cli_help_succeeds(self):
        project_root = Path(__file__).resolve().parents[2]
        result = subprocess.run(
            [sys.executable, "scripts/create_content_job_review.py", "--help"],
            cwd=project_root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage:", result.stdout)

    def test_deterministic_batch_id(self):
        self.assertEqual(adapter.deterministic_batch_id("test-niche", "cid_001"), "content-test-niche-cid_001")

    def test_hash_mismatch_blocks_before_review(self):
        job, data = self._job()
        data["audio_sha256"] = "0" * 64
        (job / "content.json").write_text(json.dumps(data), encoding="utf-8")
        with patch.object(produce_batch, "process_job") as process:
            with self.assertRaisesRegex(adapter.ContentJobReviewError, "SHA256"):
                adapter.create_content_job_review(job, registry_path=self.registry)
        process.assert_not_called()

    def test_mi_otra_yo_maps_to_strict_title_exclusive(self):
        policy = adapter.resolve_asset_profile("test-niche", "MI_OTRA_YO", self.registry)
        self.assertEqual(adapter.legacy_review_arguments(policy), ("mi-otra-yo", "title-exclusive"))

    def test_generales_maps_to_existing_open_behavior(self):
        policy = adapter.resolve_asset_profile("test-niche", "GENERALES", self.registry)
        self.assertEqual(adapter.legacy_review_arguments(policy), ("", "open"))

    def test_not_ready_blocks_before_review(self):
        job, _ = self._job(profile="ROMPIENDO_CIRCULO")
        with patch.object(produce_batch, "process_job") as process:
            with self.assertRaisesRegex(adapter.ContentJobReviewError, "NOT READY"):
                adapter.create_content_job_review(job, registry_path=self.registry)
        process.assert_not_called()

    def test_provenance_is_added_to_generated_plan(self):
        job, data = self._job()
        (result, plan), process = self._create(job)
        self.assertEqual(result, "created")
        self.assertEqual(human_review.read_json(plan)["content_job"]["content_id"], data["content_id"])
        self.assertEqual(human_review.read_json(plan)["content_job"]["resolved_asset_policy"], {
            "providers": ["asset_hub"], "asset_hub": {"sources": [{"scope": "title", "title": "mi-otra-yo"}], "generic_fallback": False}})
        process.assert_called_once()

    def test_exact_existing_plan_is_idempotent(self):
        job, _ = self._job()
        (_, plan), _ = self._create(job)
        with patch.object(produce_batch, "HOST_ROOT", self.root), patch.object(produce_batch, "process_job") as process:
            result, repeated = adapter.create_content_job_review(job, registry_path=self.registry)
        self.assertEqual((result, repeated), ("already_exists", plan))
        process.assert_not_called()

    def test_conflicting_existing_provenance_fails(self):
        job, _ = self._job()
        (_, plan), _ = self._create(job)
        payload = human_review.read_json(plan)
        payload["content_job"]["content_id"] = "other"
        human_review.write_json_atomic(plan, payload)
        with patch.object(produce_batch, "HOST_ROOT", self.root):
            with self.assertRaisesRegex(adapter.ContentJobReviewError, "provenance differs"):
                adapter.create_content_job_review(job, registry_path=self.registry)

    def test_title_cannot_escape_review_directory(self):
        job, _ = self._job(title="../../outside")
        (result, plan), _ = self._create(job)
        self.assertEqual(result, "created")
        self.assertTrue(plan.is_relative_to(self.root / "storage" / "review_queue"))
        self.assertEqual(plan.parent.name, produce_batch.sanitize_id("../../outside"))


if __name__ == "__main__":
    unittest.main()
