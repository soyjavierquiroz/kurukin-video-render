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

    def _job(self, *, title="A title", profile="MI_OTRA_YO", video_terms=None):
        path = self.root / "jobs" / "test-niche" / "cid_001"
        path.mkdir(parents=True)
        audio, script = path / "source.mp3", path / "script.txt"
        audio.write_bytes(b"audio")
        script.write_text("script", encoding="utf-8")
        data = {"content_id": "cid_001", "niche_id": "test-niche", "title": title,
                "asset_profile": profile, "audio_sha256": hashlib.sha256(b"audio").hexdigest(),
                "script_sha256": hashlib.sha256(b"script").hexdigest()}
        if video_terms is not None:
            data["video_terms"] = video_terms
        (path / "content.json").write_text(json.dumps(data), encoding="utf-8")
        return path, data

    def _fake_process(self, job, **_kwargs):
        plan = human_review.plan_path(job.batch_id, job.stem, self.root)
        policy = adapter.resolve_asset_profile("test-niche", "MI_OTRA_YO", self.registry)
        human_review.write_json_atomic(plan, {
            "batch_id": job.batch_id,
            "task_id": job.task_id,
            "stem": job.stem,
            "job_name": job.stem,
            "audio_path": job.mp3.as_posix(),
            "script_path": job.txt.as_posix(),
            "material_source_policy": policy.to_dict(),
            "asset_hub_source_policy": adapter.build_asset_hub_source_policy(policy),
            "review_status": human_review.STATUS_PENDING,
        })
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

    def test_operator_video_terms_are_provenanced_and_same_terms_reuse_plan(self):
        job, _ = self._job(video_terms="café, barista")
        (_, plan), _ = self._create(job)
        self.assertEqual(human_review.read_json(plan)["content_job"]["video_terms"], "café, barista")
        with patch.object(produce_batch, "HOST_ROOT", self.root), patch.object(produce_batch, "process_job") as process:
            self.assertEqual(adapter.create_content_job_review(job, registry_path=self.registry)[0], "already_exists")
        process.assert_not_called()

    def test_changed_operator_video_terms_marks_pending_plan_stale_and_rebuilds(self):
        job, data = self._job(video_terms="café")
        (_, plan), _ = self._create(job)
        data["video_terms"] = "barista"
        (job / "content.json").write_text(json.dumps(data), encoding="utf-8")
        (result, rebuilt), process = self._create(job)
        self.assertEqual((result, rebuilt), ("created", plan))
        self.assertEqual(human_review.read_json(plan)["content_job"]["video_terms"], "barista")
        self.assertEqual(process.call_args.kwargs["video_terms"], "barista")

    def test_pending_plan_missing_provenance_is_safely_backfilled_only(self):
        job, data = self._job()
        (_, plan), _ = self._create(job)
        original = human_review.read_json(plan)
        del original["content_job"]
        human_review.write_json_atomic(plan, original)

        with patch.object(produce_batch, "HOST_ROOT", self.root), patch.object(produce_batch, "process_job") as process:
            result, repeated = adapter.create_content_job_review(job, registry_path=self.registry)

        updated = human_review.read_json(plan)
        self.assertEqual((result, repeated), ("provenance_backfilled", plan))
        self.assertEqual(updated["content_job"]["content_id"], data["content_id"])
        without_provenance = dict(updated)
        del without_provenance["content_job"]
        self.assertEqual(without_provenance, original)
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

    def test_non_pending_plan_missing_provenance_is_not_mutated(self):
        job, _ = self._job()
        (_, plan), _ = self._create(job)
        original = human_review.read_json(plan)
        del original["content_job"]
        original["review_status"] = human_review.STATUS_APPROVED
        human_review.write_json_atomic(plan, original)

        with patch.object(produce_batch, "HOST_ROOT", self.root), self.assertRaisesRegex(
            adapter.ContentJobReviewError, "not pending"
        ):
            adapter.create_content_job_review(job, registry_path=self.registry)

        self.assertEqual(human_review.read_json(plan), original)

    def test_policy_mismatch_prevents_backfill(self):
        job, _ = self._job()
        (_, plan), _ = self._create(job)
        original = human_review.read_json(plan)
        del original["content_job"]
        original["asset_hub_source_policy"] = {"sources": []}
        human_review.write_json_atomic(plan, original)

        with patch.object(produce_batch, "HOST_ROOT", self.root), self.assertRaisesRegex(
            adapter.ContentJobReviewError, "source policy differs"
        ):
            adapter.create_content_job_review(job, registry_path=self.registry)

        self.assertEqual(human_review.read_json(plan), original)

    def test_source_identity_mismatch_prevents_backfill(self):
        job, _ = self._job()
        (_, plan), _ = self._create(job)
        original = human_review.read_json(plan)
        del original["content_job"]
        original["audio_path"] = (self.root / "other.mp3").as_posix()
        human_review.write_json_atomic(plan, original)

        with patch.object(produce_batch, "HOST_ROOT", self.root), self.assertRaisesRegex(
            adapter.ContentJobReviewError, "source identity differs"
        ):
            adapter.create_content_job_review(job, registry_path=self.registry)

        self.assertEqual(human_review.read_json(plan), original)

    def test_title_cannot_escape_review_directory(self):
        job, _ = self._job(title="../../outside")
        (result, plan), _ = self._create(job)
        self.assertEqual(result, "created")
        self.assertTrue(plan.is_relative_to(self.root / "storage" / "review_queue"))
        self.assertEqual(plan.parent.name, produce_batch.sanitize_id("../../outside"))


if __name__ == "__main__":
    unittest.main()
