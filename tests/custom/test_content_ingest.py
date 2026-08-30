"""Focused offline contracts for the local content-ingestion step."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from scripts.content_ingest import (
    ContentIngestError,
    audio_duration_seconds,
    download_by_file_id,
    ingest_content,
    validate_request,
)


class ContentIngestTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.registry_path = self.root / "niches.json"
        self.registry_path.write_text(json.dumps({"version": 1, "niches": {
            "test-niche": {"sheet_id": "sheet", "rclone_remote": "test-remote",
             "final_drive_folder_id": "folder", "default_asset_profile": "MI_OTRA_YO",
             "allowed_asset_profiles": ["MI_OTRA_YO", "GENERALES", "ROMPIENDO_CIRCULO"]}
        }}), encoding="utf-8")

    def args(self, **overrides):
        values = dict(niche_id="test-niche", content_id="cf_000001", title="A real title",
                      audio_file_id="audio-id", script_file_id="script-id", asset_profile="MI_OTRA_YO",
                      registry_path=self.registry_path, job_root=self.root / "jobs",
                      download_file=self.download, duration_reader=lambda _: 12.5)
        values.update(overrides)
        return values

    @staticmethod
    def download(_remote, file_id, target):
        target.write_bytes(b"audio bytes" if file_id == "audio-id" else b"Valid script text")

    def test_valid_metadata_validation(self):
        niche, policy = validate_request(**{key: value for key, value in self.args().items()
                                            if key in {"niche_id", "content_id", "title", "audio_file_id", "script_file_id", "asset_profile", "registry_path"}})
        self.assertEqual(niche["rclone_remote"], "test-remote")
        self.assertTrue(policy.providers.is_enabled("asset_hub"))

    def test_invalid_niche_fails(self):
        with self.assertRaisesRegex(ContentIngestError, "unknown niche_id"):
            ingest_content(**self.args(niche_id="missing"))

    def test_profile_not_allowed_or_not_ready_fails(self):
        with self.assertRaisesRegex(ContentIngestError, "not allowed"):
            ingest_content(**self.args(asset_profile="NOT_ALLOWED"))
        with self.assertRaisesRegex(ContentIngestError, "NOT READY"):
            ingest_content(**self.args(asset_profile="ROMPIENDO_CIRCULO"))

    def test_unsafe_content_id_fails(self):
        with self.assertRaisesRegex(ContentIngestError, "filesystem-safe"):
            ingest_content(**self.args(content_id="../unsafe"))

    def test_existing_same_content_identity_is_idempotent(self):
        first = ingest_content(**self.args())
        second = ingest_content(**self.args(download_file=lambda *_: self.fail("must not download")))
        self.assertEqual(first, second)
        self.assertEqual((self.root / "jobs" / "test-niche").glob("cf_000001").__next__().name, "cf_000001")

    def test_metadata_uses_resolved_asset_policy_not_legacy_name(self):
        metadata = ingest_content(**self.args())
        self.assertIn("resolved_asset_policy", metadata)
        self.assertNotIn("asset_policy", metadata)
        self.assertEqual(metadata["resolved_asset_policy"], {
            "providers": ["asset_hub"],
            "asset_hub": {
                "sources": [{"scope": "title", "title": "mi-otra-yo"}],
                "generic_fallback": False,
            },
        })

    def test_metadata_carries_resolved_mpt_defaults(self):
        registry = json.loads(self.registry_path.read_text(encoding="utf-8"))
        registry["niches"]["test-niche"]["mpt_defaults"] = {
            "version": 1, "video_aspect": "16:9", "video_clip_duration": 7,
            "bgm": {"mode": "RANDOM", "volume": .12},
        }
        self.registry_path.write_text(json.dumps(registry), encoding="utf-8")
        metadata = ingest_content(**self.args())
        self.assertEqual(metadata["effective_mpt_settings"]["video_aspect"], "16:9")
        self.assertEqual(metadata["effective_mpt_settings"]["video_clip_duration"], 7)
        self.assertEqual(metadata["effective_mpt_settings"]["bgm"], {
            "mode": "RANDOM", "volume": .12, "file_id": "", "prompt": "",
        })

    def test_existing_content_id_with_different_drive_ids_fails(self):
        ingest_content(**self.args())
        with self.assertRaisesRegex(ContentIngestError, "different source Drive IDs"):
            ingest_content(**self.args(audio_file_id="other-audio"))

    def test_existing_content_id_with_different_asset_profile_fails(self):
        ingest_content(**self.args())
        with self.assertRaisesRegex(ContentIngestError, "different asset_profile"):
            ingest_content(**self.args(asset_profile="GENERALES"))

    def test_empty_downloaded_script_fails(self):
        def empty_script(_remote, file_id, target):
            target.write_bytes(b"audio" if file_id == "audio-id" else b"   \n")
        with self.assertRaisesRegex(ContentIngestError, "no non-whitespace"):
            ingest_content(**self.args(download_file=empty_script))

    def test_failed_fresh_ingest_is_cleaned_up_and_retryable(self):
        calls = []

        def fail_second_download(_remote, file_id, target):
            calls.append(file_id)
            if file_id == "script-id":
                raise ContentIngestError("script download failed")
            target.write_bytes(b"audio bytes")

        with self.assertRaisesRegex(ContentIngestError, "script download failed"):
            ingest_content(**self.args(download_file=fail_second_download))
        job_dir = self.root / "jobs" / "test-niche" / "cf_000001"
        self.assertFalse(job_dir.exists())
        self.assertEqual(calls, ["audio-id", "script-id"])

        metadata = ingest_content(**self.args())
        self.assertTrue((job_dir / "content.json").is_file())
        self.assertEqual(metadata["content_id"], "cf_000001")

    def test_rclone_download_uses_file_id_and_atomic_target(self):
        target = self.root / "source.mp3"

        def fake_run(command, **_kwargs):
            Path(command[-1]).write_bytes(b"downloaded")
            return SimpleNamespace(stdout="")

        with patch("scripts.content_ingest.subprocess.run", side_effect=fake_run) as run:
            download_by_file_id("test-remote", "drive-file-id", target)
        self.assertEqual(target.read_bytes(), b"downloaded")
        self.assertEqual(run.call_args.args[0][:4], [
            "rclone", "backend", "copyid", "test-remote:"
        ])
        self.assertEqual(run.call_args.args[0][4], "drive-file-id")
        self.assertFalse(list(self.root.glob(".source.mp3.*.partial")))

    def test_ffprobe_duration_is_parsed(self):
        audio = self.root / "source.mp3"
        audio.write_bytes(b"audio")
        with patch(
            "scripts.content_ingest.subprocess.run",
            return_value=SimpleNamespace(stdout="3.25\n"),
        ) as run:
            self.assertEqual(audio_duration_seconds(audio), 3.25)
        self.assertEqual(run.call_args.args[0][0], "ffprobe")


if __name__ == "__main__":
    unittest.main()
