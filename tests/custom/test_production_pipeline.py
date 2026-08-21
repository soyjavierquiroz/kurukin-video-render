"""Contract tests for the resumable approved-plan production gates."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import produce_batch


SRT = "1\n00:00:00,000 --> 00:00:01,000\nHola mundo\n"
APPROVED_REPORT = {"status": "ok", "confidence": 0.99, "review_required": False}


class ProductionPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.input_dir = self.root / "input"
        self.input_dir.mkdir()
        self.mp3 = self.input_dir / "story.mp3"
        self.txt = self.input_dir / "story.txt"
        self.mp3.write_bytes(b"canonical mp3")
        self.txt.write_text("Hola mundo", encoding="utf-8")
        self.job = produce_batch.Job("story", self.mp3, self.txt, None, "batch")
        self.task_dir = self.root / "storage" / "tasks" / self.job.task_id
        self.output_dir = self.root / "storage" / "batch_outputs" / "batch"
        self.output_dir.mkdir(parents=True)
        self.report_path = self.output_dir / produce_batch.REPORT_NAME
        self.report = produce_batch.init_report("batch", [self.job], self.report_path)
        produce_batch.write_json_atomic(self.report_path, self.report)
        self.calls: list[str] = []

    def tearDown(self):
        self.temp.cleanup()

    def artifact(self, name: str, content: bytes = b"valid") -> Path:
        path = self.task_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def approved_subtitle(self, content: str = SRT) -> None:
        self.artifact("subtitle.srt", content.encode())
        produce_batch.write_json_atomic(self.task_dir / "subtitle-alignment.json", APPROVED_REPORT)

    def produce(self) -> str:
        def valid_video(path: Path) -> bool:
            return path.is_file() and path.read_bytes() == b"valid"

        def worker(manifest: Path, stage: str, _log: Path) -> None:
            self.calls.append(stage)
            payload = produce_batch.read_json(manifest)
            self.assertNotIn("subtitle_audio_file", payload)
            if stage == "master":
                self.artifact("final-1.mp4")
            elif stage == "subtitles":
                self.assertEqual(payload["audio_file"], produce_batch.host_to_container(self.mp3))
                self.approved_subtitle()
            else:
                self.fail(f"unexpected worker stage {stage}")

        def hyperframes(_job, _master, _srt, final, _log, _preset, _position):
            self.calls.append("hyperframes")
            final.parent.mkdir(parents=True, exist_ok=True)
            final.write_bytes(b"valid")
            return final

        with patch.object(produce_batch, "HOST_ROOT", self.root), patch.object(
            produce_batch, "run_worker", worker
        ), patch.object(produce_batch, "run_hyperframes", hyperframes), patch.object(
            produce_batch, "valid_mp4", valid_video
        ), patch.object(produce_batch, "ensure_similar_duration", lambda *_: None):
            return produce_batch.process_job(
                self.job,
                index=1,
                total=1,
                batch_output_dir=self.output_dir,
                report=self.report,
                report_path=self.report_path,
                preset="karaoke",
                position="bottom",
            )

    def test_case_1_missing_outputs_runs_all_stages_from_mp3(self):
        self.assertEqual(self.produce(), "completed")
        self.assertEqual(self.calls, ["master", "subtitles", "hyperframes"])

    def test_case_2_valid_master_runs_subtitles_and_hyperframes(self):
        self.artifact("final-1.mp4")
        self.assertEqual(self.produce(), "completed")
        self.assertEqual(self.calls, ["subtitles", "hyperframes"])

    def test_case_3_valid_master_and_srt_runs_only_hyperframes(self):
        self.artifact("final-1.mp4")
        self.approved_subtitle()
        self.assertEqual(self.produce(), "completed")
        self.assertEqual(self.calls, ["hyperframes"])

    def test_case_4_all_valid_skips_every_stage(self):
        self.artifact("final-1.mp4")
        self.approved_subtitle()
        self.artifact("final-subtitled.mp4")
        self.assertEqual(self.produce(), "completed")
        self.assertEqual(self.calls, [])

    def test_case_5_corrupt_master_reruns_master(self):
        self.artifact("final-1.mp4", b"corrupt")
        self.assertEqual(self.produce(), "completed")
        self.assertEqual(self.calls, ["master", "subtitles", "hyperframes"])

    def test_case_6_corrupt_delivery_reruns_only_hyperframes(self):
        self.artifact("final-1.mp4")
        self.approved_subtitle()
        self.artifact("final-subtitled.mp4", b"corrupt")
        self.assertEqual(self.produce(), "completed")
        self.assertEqual(self.calls, ["hyperframes"])

    def test_case_7_corrupt_srt_reruns_subtitles(self):
        self.artifact("final-1.mp4")
        self.approved_subtitle("not an srt")
        self.assertEqual(self.produce(), "completed")
        self.assertEqual(self.calls, ["subtitles", "hyperframes"])

    def test_case_8_missing_subtitle_wav_does_not_affect_production(self):
        self.assertFalse((self.task_dir / "subtitle-audio.wav").exists())
        self.assertEqual(self.produce(), "completed")
        self.assertNotIn("subtitle-audio.wav", " ".join(self.calls))

    def test_case_9_missing_mp3_is_a_clear_deterministic_failure(self):
        self.mp3.unlink()
        plan = self.root / "production-plan.json"
        plan.write_text(
            json.dumps({
                "review_status": "approved", "audio_path": self.mp3.as_posix(),
                "script_path": self.txt.as_posix(), "batch_id": "batch", "stem": "story",
            }),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(produce_batch.StageError, "approved audio missing"):
            produce_batch.process_approved_review_plan(plan)


if __name__ == "__main__":
    unittest.main()
