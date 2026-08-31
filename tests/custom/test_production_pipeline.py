"""Contract tests for the resumable approved-plan production gates."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from scripts import produce_batch
from scripts import batch_mpt_worker
from app.models.schema import MaterialInfo


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

    def test_manifest_carries_effective_mpt_settings(self):
        settings = {"version": 1, "video_aspect": "16:9", "video_clip_duration": 7,
                    "video_resolution": "720p", "video_transition_mode": None,
                    "bgm": {"mode": "NONE", "volume": 0, "file_id": "", "prompt": ""}}
        manifest = produce_batch.make_manifest(self.job, self.task_dir, "Hola", effective_mpt_settings=settings)
        self.assertEqual(manifest["effective_mpt_settings"], settings)

    def test_manifest_carries_raw_mpt_defaults(self):
        defaults = {"version": 1, "bgm": {"mode": "RANDOM", "volume": .12}}
        manifest = produce_batch.make_manifest(self.job, self.task_dir, "Hola", mpt_defaults=defaults)
        self.assertEqual(manifest["mpt_defaults"], defaults)

    def test_manifest_and_review_provenance_preserve_operator_video_terms(self):
        manifest = produce_batch.make_manifest(self.job, self.task_dir, "Hola", video_terms="café, barista")
        self.assertEqual(manifest["video_terms"], "café, barista")
        manifest.update({"production_plan_path": (self.root / "plan.json").as_posix()})

        def start(_task_id, params, *, stop_at):
            self.assertEqual(stop_at, "review")
            self.assertEqual(params.video_terms, "café, barista")
            self.assertEqual(params.human_review["video_terms_source"], "operator")
            self.assertEqual(params.human_review["video_terms_raw"], "café, barista")
            (self.root / "plan.json").write_text("{}", encoding="utf-8")
            return {}

        with patch("app.services.task.start", side_effect=start):
            batch_mpt_worker.run_review(manifest)

    def test_review_and_master_share_effective_clip_duration(self):
        plan_path = self.root / "production-plan.json"
        settings = {"version": 1, "video_aspect": "16:9", "video_clip_duration": 7,
                    "video_resolution": "", "video_transition_mode": None,
                    "bgm": {"mode": "NONE", "volume": 0, "file_id": "", "prompt": ""}}
        manifest = {
            "batch_id": "batch", "task_id": "task", "task_dir": self.task_dir.as_posix(),
            "stem": "story", "script": "Hola", "audio_file": self.mp3.as_posix(),
            "text_file": self.txt.as_posix(), "production_plan_path": plan_path.as_posix(),
            "effective_mpt_settings": settings,
        }

        def start(_task_id, params, *, stop_at):
            self.assertEqual(stop_at, "review")
            self.assertEqual(params.video_aspect.value, "16:9")
            self.assertEqual(params.video_clip_duration, 7)
            plan_path.write_text("{}", encoding="utf-8")
            return {}

        with patch("app.services.task.start", side_effect=start):
            batch_mpt_worker.run_review(manifest)

    def test_worker_without_defaults_keeps_no_bgm_baseline(self):
        manifest = {
            "batch_id": "batch", "task_id": "task", "task_dir": self.task_dir.as_posix(),
            "stem": "story", "script": "Hola", "audio_file": self.mp3.as_posix(),
            "text_file": self.txt.as_posix(), "production_plan_path": (self.root / "plan.json").as_posix(),
        }

        def start(_task_id, params, *, stop_at):
            self.assertEqual(stop_at, "review")
            self.assertEqual(params.bgm_type, "")
            self.assertEqual(params.bgm_file, "")
            self.assertEqual(params.bgm_volume, 0)
            self.assertEqual(params.video_aspect.value, "9:16")
            self.assertEqual(params.video_clip_duration, 5)
            (self.root / "plan.json").write_text("{}", encoding="utf-8")
            return {}

        with patch("app.services.task.start", side_effect=start):
            batch_mpt_worker.run_review(manifest)

    def artifact(self, name: str, content: bytes = b"valid") -> Path:
        path = self.task_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def approved_subtitle(self, content: str = SRT) -> None:
        self.artifact("subtitle.srt", content.encode())
        produce_batch.write_json_atomic(self.task_dir / "subtitle-alignment.json", APPROVED_REPORT)
        produce_batch.write_stage_metadata(
            self.task_dir / produce_batch.SUBTITLE_STAGE_NAME,
            produce_batch.subtitle_stage_fingerprint(self.job),
        )

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

    def approved_plan_payload(self, *, duration: float | None = 4.89) -> dict:
        segments = []
        for index in range(10):
            segments.append({
                "segment_id": f"segment-{index + 1:03d}",
                "duration": 4.89,
                "selected_asset": {
                    "asset_uid": f"drive-{index + 1}",
                    "duration": duration,
                    "source_duration": duration,
                    "metadata": {"duration": duration},
                },
                "backup_assets": [],
            })
        return {
            "review_status": "approved",
            "duration": 48.9,
            "audio_path": self.mp3.as_posix(),
            "script_path": self.txt.as_posix(),
            "batch_id": "batch",
            "stem": "story",
            "coverage": {
                "target_duration": 49.0,
                "covered_duration": 49.0,
                "missing_duration": 0.0,
            },
            "segments": segments,
        }

    def approved_single_segment_plan(
        self,
        *,
        primary_duration: float | None,
        backup_duration: float | None = None,
    ) -> dict:
        primary = {
            "asset_uid": "primary",
            "duration": primary_duration,
            "source_duration": primary_duration,
            "metadata": {"duration": primary_duration},
        }
        backups = []
        if backup_duration is not None:
            backups.append({
                "asset_uid": "backup",
                "duration": backup_duration,
                "source_duration": backup_duration,
                "metadata": {"duration": backup_duration},
            })
        plan = {
            "review_status": "approved",
            # The final scene receives the renderer's 0.10s tail, making the
            # effective scene target exactly five seconds.
            "duration": 4.9,
            "segments": [{
                "segment_id": "segment-001",
                "duration": 4.9,
                "selected_asset": primary,
                "backup_assets": backups,
            }],
        }
        return produce_batch.human_review.refresh_plan_coverage(plan)

    def test_case_1_missing_outputs_runs_all_stages_from_mp3(self):
        self.assertEqual(self.produce(), "completed")
        self.assertEqual(self.calls, ["master", "subtitles", "hyperframes"])
        record = produce_batch.production_identity(
            self.job.stem, self.mp3, self.txt,
            production_recipe_version=produce_batch.production_recipe_for("none", "karaoke", "bottom"),
        )
        with patch.object(produce_batch, "HOST_ROOT", self.root):
            with produce_batch.production_registry()._connect() as conn:
                row = conn.execute(
                    "SELECT status FROM completed_productions WHERE production_fingerprint=?",
                    (record["production_fingerprint"],),
                ).fetchone()
        self.assertEqual(row["status"], "completed")

    def test_failed_production_is_not_registered_completed(self):
        def valid_video(path: Path) -> bool:
            return path.is_file() and path.read_bytes() == b"valid"

        def worker(_manifest: Path, stage: str, _log: Path) -> None:
            if stage == "master":
                self.artifact("final-1.mp4")
            elif stage == "subtitles":
                self.approved_subtitle()

        with patch.object(produce_batch, "HOST_ROOT", self.root), patch.object(produce_batch, "run_worker", worker), \
             patch.object(produce_batch, "run_hyperframes", side_effect=produce_batch.StageError("failed")), \
             patch.object(produce_batch, "valid_mp4", valid_video), patch.object(produce_batch, "ensure_similar_duration", lambda *_: None):
            with self.assertRaisesRegex(produce_batch.StageError, "failed"):
                produce_batch.process_job(self.job, index=1, total=1, batch_output_dir=self.output_dir,
                                          report=self.report, report_path=self.report_path, preset="karaoke", position="bottom")
            with produce_batch.production_registry()._connect() as conn:
                count = conn.execute("SELECT count(*) FROM completed_productions").fetchone()[0]
        self.assertEqual(count, 0)

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
        produce_batch.write_stage_metadata(
            self.task_dir / produce_batch.HYPERFRAMES_STAGE_NAME,
            produce_batch.final_stage_fingerprint(
                self.task_dir / "final-1.mp4", self.task_dir / "subtitle.srt",
                preset="karaoke", position="bottom", visual_style=produce_batch.VISUAL_STYLE_NONE,
            ),
        )
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

    def test_old_subtitle_recipe_rebuilds_but_reuses_master(self):
        self.artifact("final-1.mp4")
        self.artifact("subtitle.srt", SRT.encode())
        produce_batch.write_json_atomic(self.task_dir / "subtitle-alignment.json", APPROVED_REPORT)
        produce_batch.write_stage_metadata(self.task_dir / produce_batch.SUBTITLE_STAGE_NAME, "old-subtitle-recipe")
        self.assertEqual(self.produce(), "completed")
        self.assertEqual(self.calls, ["subtitles", "hyperframes"])

    def test_changed_srt_hash_requires_hyperframes_rebuild(self):
        self.artifact("final-1.mp4")
        self.approved_subtitle()
        before = produce_batch.final_stage_fingerprint(
            self.task_dir / "final-1.mp4", self.task_dir / "subtitle.srt",
            preset="karaoke", position="bottom", visual_style="none",
        )
        self.artifact("subtitle.srt", b"1\n00:00:00,000 --> 00:00:01,000\nTexto cambiado\n")
        after = produce_batch.final_stage_fingerprint(
            self.task_dir / "final-1.mp4", self.task_dir / "subtitle.srt",
            preset="karaoke", position="bottom", visual_style="none",
        )
        self.assertNotEqual(before, after)

    def test_master_fingerprint_changes_only_for_authoritative_timeline_inputs(self):
        plan = self.approved_plan_payload()
        current = produce_batch.master_stage_fingerprint(plan)
        stage = self.task_dir / produce_batch.MASTER_STAGE_NAME
        produce_batch.write_stage_metadata(stage, current)
        self.assertTrue(produce_batch.stage_metadata_is_current(stage, current))

        variants = []
        primary = json.loads(json.dumps(plan)); primary["segments"][0]["selected_asset"]["asset_uid"] = "changed-primary"; variants.append(primary)
        backup = json.loads(json.dumps(plan)); backup["segments"][0]["backup_assets"] = [json.loads(json.dumps(backup["segments"][0]["selected_asset"]))]; backup["segments"][0]["backup_assets"][0]["asset_uid"] = "changed-backup"; variants.append(backup)
        flipped = json.loads(json.dumps(plan)); flipped["segments"][0]["selected_asset"]["flip_horizontal"] = False; variants.append(flipped)
        duration = json.loads(json.dumps(plan)); duration["segments"][0]["selected_asset"]["metadata"]["duration"] = 3.0; variants.append(duration)
        self.assertTrue(all(produce_batch.master_stage_fingerprint(item) != current for item in variants))

        coverage_only = json.loads(json.dumps(plan))
        coverage_only["coverage"] = {"missing_duration": 999}
        coverage_only["segments"][0]["coverage"] = {"stale": True}
        self.assertEqual(current, produce_batch.master_stage_fingerprint(coverage_only))

    def test_master_recipe_version_changes_fingerprint(self):
        plan = self.approved_plan_payload()
        with patch.object(produce_batch, "MASTER_RECIPE_VERSION", "changed-timeline-recipe"):
            changed = produce_batch.master_stage_fingerprint(plan)
        self.assertNotEqual(produce_batch.master_stage_fingerprint(plan), changed)

    def test_styled_master_requires_current_master_style_fingerprint(self):
        master = self.artifact("final-1.mp4", b"master-one")
        styled = self.artifact("final-styled-warm-sepia.mp4", b"styled")
        stage = styled.with_name(produce_batch.STYLED_MASTER_STAGE_NAME)
        produce_batch.write_stage_metadata(stage, produce_batch.styled_master_fingerprint(master, "warm-sepia"))
        with patch.object(produce_batch, "validate_styled_master"):
            self.assertTrue(produce_batch.styled_master_is_current(master, styled, "warm-sepia"))
            master.write_bytes(b"master-two")
            self.assertFalse(produce_batch.styled_master_is_current(master, styled, "warm-sepia"))
            produce_batch.write_stage_metadata(stage, produce_batch.styled_master_fingerprint(master, "warm-sepia"))
            with patch.object(produce_batch, "VISUAL_STYLE_VERSION", {"none": 1, "warm-sepia": 3}):
                self.assertFalse(produce_batch.styled_master_is_current(master, styled, "warm-sepia"))

    def test_old_production_recipe_is_not_a_global_completion_match(self):
        old = produce_batch.production_identity(self.job.stem, self.mp3, self.txt, production_recipe_version="old")
        current = produce_batch.production_identity(
            self.job.stem, self.mp3, self.txt,
            production_recipe_version=produce_batch.production_recipe_for("none", "karaoke", "bottom"),
        )
        self.assertNotEqual(old["production_fingerprint"], current["production_fingerprint"])

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

    def test_approved_container_audio_and_script_paths_resolve_on_host(self):
        plan_path = self.root / "production-plan.json"
        plan = self.approved_plan_payload()
        plan["audio_path"] = (produce_batch.CONTAINER_ROOT / self.mp3.relative_to(self.root)).as_posix()
        plan["script_path"] = (produce_batch.CONTAINER_ROOT / self.txt.relative_to(self.root)).as_posix()
        plan_path.write_text(json.dumps(plan), encoding="utf-8")

        with patch.object(produce_batch, "HOST_ROOT", self.root), \
             patch.object(produce_batch, "process_job", return_value="completed") as process_job:
            self.assertEqual(produce_batch.process_approved_review_plan(plan_path), "completed")

        job = process_job.call_args.args[0]
        self.assertEqual(job.mp3, self.mp3)
        self.assertEqual(job.txt, self.txt)

    def test_approved_host_audio_and_script_paths_still_work(self):
        plan_path = self.root / "production-plan.json"
        plan_path.write_text(json.dumps(self.approved_plan_payload()), encoding="utf-8")

        with patch.object(produce_batch, "HOST_ROOT", self.root), \
             patch.object(produce_batch, "process_job", return_value="completed") as process_job:
            self.assertEqual(produce_batch.process_approved_review_plan(plan_path), "completed")

        job = process_job.call_args.args[0]
        self.assertEqual(job.mp3, self.mp3)
        self.assertEqual(job.txt, self.txt)

    def test_production_uses_the_exact_cli_approved_plan_not_batch_stem_lookup(self):
        """The worker manifest must retain the frozen file selected by the operator."""
        plan_path = self.root / "operator-approved-plan.json"
        plan_path.write_text(json.dumps(self.approved_plan_payload()), encoding="utf-8")

        with patch.object(produce_batch, "HOST_ROOT", self.root), \
             patch.object(produce_batch, "process_job", return_value="completed") as process_job:
            produce_batch.process_approved_review_plan(plan_path)

        self.assertEqual(process_job.call_args.kwargs["approved_plan_path"], plan_path)

    def test_master_uses_current_human_selected_asset_without_discovery(self):
        """A reviewed replacement wins over original_selected_asset end-to-end."""
        plan_path = self.root / "operator-approved-plan.json"
        plan = {
            "review_status": "approved", "duration": 4.9,
            "segments": [{
                "segment_id": "segment-001", "duration": 4.9,
                "selected_asset": {"asset_uid": "approved-A", "canonical_id": "approved-A",
                                   "dedupe_key": "approved-A", "provider": "asset_hub",
                                   "metadata": {"duration": 5}},
                "original_selected_asset": {"asset_uid": "original-X", "canonical_id": "original-X",
                                            "provider": "asset_hub", "metadata": {"duration": 5}},
                "alternatives": [{"asset_uid": "alternative-Z", "provider": "asset_hub"}],
                "backup_assets": [],
            }],
        }
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        audio_source = self.root / "audio.mp3"
        audio_source.write_bytes(b"audio")
        source = self.root / "approved-A.mp4"; source.write_bytes(b"asset")
        task_dir = self.root / "task"; task_dir.mkdir()
        acquired = SimpleNamespace(materials=(MaterialInfo(
            provider="asset_hub", url=source.as_posix(), duration=5,
            source_info={"asset_id": "approved-A"},
        ),))

        def start(task_id, _params, *, stop_at):
            self.assertEqual(stop_at, "video")
            self.assertEqual(_params.video_aspect, "16:9")
            self.assertEqual(_params.video_resolution, "720p")
            self.assertEqual(_params.video_clip_duration, 7)
            self.assertEqual(_params.video_transition_mode.value, "FadeIn")
            self.assertEqual(_params.bgm_type, "random")
            self.assertEqual(_params.bgm_volume, .12)
            self.assertFalse(_params.subtitle_enabled)
            self.assertEqual(_params.custom_audio_file, (task_dir / "custom-audio.mp3").as_posix())
            self.assertTrue((task_dir / "custom-audio.mp3").is_file())
            self.assertTrue((task_dir / "custom-audio.mp3").stat().st_size > 0)
            self.assertEqual((task_dir / "custom-audio.mp3").read_bytes(), audio_source.read_bytes())
            (task_dir / "final-1.mp4").write_bytes(b"rendered")
            return {"task_id": task_id}

        with patch("app.custom.material_acquisition.acquire_selected_materials", return_value=acquired) as acquire, \
             patch.object(batch_mpt_worker, "_stage_human_review_timeline", return_value=([], self.root / "timeline")), \
             patch("app.services.task.start", side_effect=start) as start_task, \
             patch("app.services.task.generate_terms") as terms, \
             patch("app.services.task.discover_material_candidates") as discover, \
             patch("app.services.task.select_material_candidates") as select:
            result = batch_mpt_worker.run_master({
                "production_plan_path": plan_path.as_posix(), "task_id": "task-1",
                "task_dir": task_dir.as_posix(), "stem": "story", "script": "script",
                "audio_file": audio_source.as_posix(),
                "effective_mpt_settings": {
                    "version": 1, "bgm": {"mode": "RANDOM", "volume": .12},
                    "video_aspect": "16:9", "video_resolution": "720p",
                    "video_clip_duration": 7, "video_transition_mode": "FadeIn",
                },
            })

        self.assertTrue(result["ok"])
        self.assertEqual([item.candidate.canonical_id for item in acquire.call_args.kwargs["selection_result"].decisions], ["approved-A"])
        terms.assert_not_called(); discover.assert_not_called(); select.assert_not_called()
        start_task.assert_called_once()

    def test_master_blocks_materialization_with_unapproved_asset_before_render(self):
        selection = SimpleNamespace(decisions=(
            SimpleNamespace(candidate=SimpleNamespace(canonical_id="A")),
            SimpleNamespace(candidate=SimpleNamespace(canonical_id="B")),
        ))
        acquisition = SimpleNamespace(materials=(
            MaterialInfo(provider="asset_hub", url="/tmp/A.mp4", duration=5, source_info={"asset_id": "A"}),
            MaterialInfo(provider="asset_hub", url="/tmp/X.mp4", duration=5, source_info={"asset_id": "X"}),
        ))

        with self.assertRaisesRegex(RuntimeError, "unapproved asset_uids=X"):
            batch_mpt_worker._validate_approved_materialization(selection, acquisition)

    def test_approved_missing_container_audio_still_raises_stage_error(self):
        plan_path = self.root / "production-plan.json"
        plan = self.approved_plan_payload()
        plan["audio_path"] = "/MoneyPrinterTurbo/storage/content_jobs/cf_000002/source.mp3"
        plan["script_path"] = (produce_batch.CONTAINER_ROOT / self.txt.relative_to(self.root)).as_posix()
        plan_path.write_text(json.dumps(plan), encoding="utf-8")

        with patch.object(produce_batch, "HOST_ROOT", self.root), \
             self.assertRaisesRegex(produce_batch.StageError, "approved audio missing"):
            produce_batch.process_approved_review_plan(plan_path)

    def test_approved_cached_coverage_cannot_hide_missing_asset_durations(self):
        plan_path = self.root / "production-plan.json"
        plan = self.approved_plan_payload(duration=None)
        plan_path.write_text(json.dumps(plan), encoding="utf-8")

        integrity = produce_batch.human_review.validate_approved_plan_integrity(plan)
        self.assertFalse(integrity["ok"])
        self.assertIn(
            "segment-001 primary drive-1 has no usable duration",
            integrity["errors"],
        )

        with patch.object(produce_batch, "process_job") as process_job:
            with self.assertRaisesRegex(produce_batch.StageError, "integrity failed"):
                produce_batch.process_approved_review_plan(plan_path)
        process_job.assert_not_called()

    def test_valid_frozen_durations_pass_integrity(self):
        integrity = produce_batch.human_review.validate_approved_plan_integrity(
            self.approved_plan_payload(duration=4.89)
        )
        self.assertTrue(integrity["ok"], integrity["errors"])

    def test_single_primary_without_duration_cannot_start_production(self):
        plan_path = self.root / "production-plan.json"
        plan = self.approved_plan_payload(duration=4.89)
        plan["segments"] = [plan["segments"][0]]
        plan["segments"][0]["selected_asset"].update(
            {"duration": None, "source_duration": None, "metadata": {"duration": None}}
        )
        plan_path.write_text(json.dumps(plan), encoding="utf-8")

        with patch.object(produce_batch, "process_job") as process_job:
            with self.assertRaisesRegex(produce_batch.StageError, "primary drive-1 has no usable duration"):
                produce_batch.process_approved_review_plan(plan_path)
        process_job.assert_not_called()

    def test_real_coverage_shortfall_cannot_start_production(self):
        plan_path = self.root / "production-plan.json"
        plan = self.approved_plan_payload(duration=4.89)
        plan["segments"] = [plan["segments"][0]]
        plan["duration"] = 5.0
        plan["segments"][0]["duration"] = 5.0
        plan["segments"][0]["selected_asset"]["metadata"]["duration"] = 2.0
        plan["coverage"] = {
            "target_duration": 5.1,
            "covered_duration": 2.222,
            "missing_duration": 2.878,
        }
        plan_path.write_text(json.dumps(plan), encoding="utf-8")

        with patch.object(produce_batch, "process_job") as process_job:
            with self.assertRaisesRegex(produce_batch.StageError, "insufficient approved visual coverage"):
                produce_batch.process_approved_review_plan(plan_path)
        process_job.assert_not_called()

    def test_integrity_allows_primary_slowdown_to_hard_floor(self):
        plan = self.approved_single_segment_plan(primary_duration=4.458)
        integrity = produce_batch.human_review.validate_approved_plan_integrity(plan)
        self.assertTrue(integrity["ok"], integrity["errors"])

        timeline = produce_batch.human_review.render_timeline_from_plan(plan)
        self.assertEqual(len(timeline.pieces), 1)
        self.assertEqual(timeline.pieces[0]["source_duration"], 4.458)
        self.assertEqual(timeline.pieces[0]["output_duration"], 5.0)
        self.assertAlmostEqual(timeline.pieces[0]["playback_speed"], 4.458 / 5.0)

    def test_integrity_freezes_primary_below_hard_slowdown_floor_when_small(self):
        plan = self.approved_single_segment_plan(primary_duration=4.125)
        integrity = produce_batch.human_review.validate_approved_plan_integrity(plan)
        self.assertTrue(integrity["ok"], integrity["errors"])

    def test_integrity_rejects_large_unresolved_primary_deficit(self):
        plan = self.approved_single_segment_plan(primary_duration=2.0)
        integrity = produce_batch.human_review.validate_approved_plan_integrity(plan)
        self.assertFalse(integrity["ok"])
        self.assertTrue(any("insufficient approved visual coverage" in error for error in integrity["errors"]))

    def test_integrity_rejects_short_timeline_tail_without_auto_extension(self):
        plan = self.approved_single_segment_plan(primary_duration=4.41)
        plan["segments"][0]["duration"] = 4.31
        plan["duration"] = 5.0
        plan = produce_batch.human_review.refresh_plan_coverage(plan)

        integrity = produce_batch.human_review.validate_approved_plan_integrity(plan)

        self.assertFalse(integrity["ok"])
        self.assertAlmostEqual(integrity["coverage"]["missing_duration"], 0.69)
        self.assertAlmostEqual(integrity["segment_coverage"]["timeline-tail"]["missing_duration"], 0.69)
        self.assertTrue(any("timeline tail" in error for error in integrity["errors"]))

    def test_integrity_freezes_real_segment_shortfall_under_limit(self):
        # 3.969s produces 4.41s at the normal slowdown floor, leaving a
        # genuine 0.69s scene gap in a 5.10s target.
        plan = self.approved_single_segment_plan(primary_duration=3.969)
        plan["segments"][0]["duration"] = 5.0
        plan["duration"] = 5.0
        plan = produce_batch.human_review.refresh_plan_coverage(plan)

        integrity = produce_batch.human_review.validate_approved_plan_integrity(plan)

        self.assertTrue(integrity["ok"], integrity["errors"])
        self.assertEqual(integrity["coverage"]["missing_duration"], 0.0)
        timeline = produce_batch.human_review.render_timeline_from_plan(plan)
        self.assertEqual(timeline.pieces[-1]["role"], "FREEZE")
        self.assertAlmostEqual(timeline.pieces[-1]["output_duration"], 0.69)

    def test_integrity_rejects_timeline_tail_under_five_seconds(self):
        plan = self.approved_single_segment_plan(primary_duration=4.0)
        plan["segments"][0]["duration"] = 3.9
        plan["duration"] = 5.0
        plan = produce_batch.human_review.refresh_plan_coverage(plan)

        integrity = produce_batch.human_review.validate_approved_plan_integrity(plan)

        self.assertFalse(integrity["ok"])
        timeline = produce_batch.human_review.render_timeline_from_plan(plan)
        self.assertFalse(any(piece["segment_id"] == "timeline-tail" for piece in timeline.pieces))
        self.assertTrue(any(item["segment_id"] == "timeline-tail" for item in timeline.segment_shortfalls))

    def test_integrity_allows_approved_backup_that_resolves_deficit(self):
        plan = self.approved_single_segment_plan(primary_duration=4.125, backup_duration=1.0)
        integrity = produce_batch.human_review.validate_approved_plan_integrity(plan)
        self.assertTrue(integrity["ok"], integrity["errors"])

    def test_integrity_rejects_primary_without_duration(self):
        plan = self.approved_single_segment_plan(primary_duration=None)
        integrity = produce_batch.human_review.validate_approved_plan_integrity(plan)
        self.assertFalse(integrity["ok"])
        self.assertIn("segment-001 primary primary has no usable duration", integrity["errors"])

    def test_stored_coverage_mismatch_refreshes_when_assets_are_valid(self):
        plan = self.approved_plan_payload(duration=4.89)
        plan["coverage"]["covered_duration"] = 48.0
        integrity = produce_batch.human_review.validate_approved_plan_integrity(plan)
        self.assertTrue(integrity["ok"], integrity["errors"])
        self.assertEqual(integrity["coverage"]["missing_duration"], 0.0)

    def test_segment_freeze_over_limit_blocks(self):
        plan = self.approved_single_segment_plan(primary_duration=3.3)
        integrity = produce_batch.human_review.validate_approved_plan_integrity(plan)
        self.assertFalse(integrity["ok"])

    def test_timeline_tail_over_limit_blocks(self):
        plan = self.approved_single_segment_plan(primary_duration=4.0)
        plan["segments"][0]["duration"] = 1.0
        plan["duration"] = 6.1
        integrity = produce_batch.human_review.validate_approved_plan_integrity(plan)
        self.assertFalse(integrity["ok"])

    def test_stage_error_includes_safe_child_output_tail(self):
        log = self.root / "worker.log"
        with self.assertRaisesRegex(produce_batch.StageError, "worker diagnostics"):
            produce_batch.run_logged(
                ["sh", "-c", "echo worker diagnostics >&2; exit 1"],
                log,
            )

    def test_worker_failure_summary_prefers_terminal_mpt_stage_error(self):
        log = self.root / "worker.log"
        log.write_text(
            "script output that is not a failure\n"
            "task failed, task_id: example, stage: audio, error: invalid custom audio file\n",
            encoding="utf-8",
        )
        self.assertEqual(
            produce_batch._worker_failure_summary(log),
            "invalid custom audio file",
        )


class WorkerRuntimeTests(unittest.TestCase):
    def test_inside_mpt_runtime_uses_direct_worker_command(self):
        manifest = produce_batch.CONTAINER_ROOT / "storage/tasks/example/batch-manifest.json"
        log = Path("/tmp/worker.log")
        with patch.object(produce_batch, "HOST_ROOT", produce_batch.CONTAINER_ROOT), patch.object(
            produce_batch, "compose_base_command"
        ) as compose, patch.object(produce_batch, "run_logged") as run_logged:
            produce_batch.run_worker(manifest, "review", log)

        compose.assert_not_called()
        run_logged.assert_called_once_with(
            [
                produce_batch.sys.executable,
                "scripts/batch_mpt_worker.py",
                manifest.as_posix(),
                "--stage",
                "review",
            ],
            log,
            timeout=produce_batch.PROCESS_TIMEOUT,
        )

    def test_outside_mpt_runtime_preserves_compose_command(self):
        host_root = Path("/opt/moneyprinterturbo")
        manifest = host_root / "storage/tasks/example/batch-manifest.json"
        log = Path("/tmp/worker.log")
        compose_command = ["docker", "compose", "-f", "docker-compose.yml"]
        with patch.object(produce_batch, "HOST_ROOT", host_root), patch.object(
            produce_batch, "compose_base_command", return_value=compose_command
        ) as compose, patch.object(produce_batch, "run_logged") as run_logged:
            produce_batch.run_worker(manifest, "master", log)

        compose.assert_called_once_with()
        run_logged.assert_called_once_with(
            compose_command + [
                "exec",
                "-T",
                "api",
                "python3",
                "scripts/batch_mpt_worker.py",
                "/MoneyPrinterTurbo/storage/tasks/example/batch-manifest.json",
                "--stage",
                "master",
            ],
            log,
            timeout=produce_batch.PROCESS_TIMEOUT,
        )

    def test_direct_worker_propagates_stage_error_from_run_logged(self):
        manifest = produce_batch.CONTAINER_ROOT / "storage/tasks/example/batch-manifest.json"
        error = produce_batch.StageError("review failed exit=1")
        with patch.object(produce_batch, "HOST_ROOT", produce_batch.CONTAINER_ROOT), patch.object(
            produce_batch, "run_logged", side_effect=error
        ):
            with self.assertRaisesRegex(produce_batch.StageError, "review failed exit=1"):
                produce_batch.run_worker(manifest, "review", Path("/tmp/worker.log"))


class SubtitleRefreshBatchTests(unittest.TestCase):
    def test_refresh_continues_after_one_job_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jobs = [
                produce_batch.Job("first", root / "first.mp3", root / "first.txt", None, "repair"),
                produce_batch.Job("second", root / "second.mp3", root / "second.txt", None, "repair"),
            ]
            for job in jobs:
                plan = produce_batch.human_review.plan_path("repair", job.stem, root)
                produce_batch.write_json_atomic(plan, {"review_status": "approved"})
            with patch.object(produce_batch, "HOST_ROOT", root), \
                 patch.object(produce_batch, "scan_input", return_value=jobs), \
                 patch.object(produce_batch, "process_approved_review_plan", side_effect=[produce_batch.StageError("broken"), "completed"]) as retry:
                status = produce_batch.refresh_stale_subtitles_batch("repair", preset="karaoke", position="bottom")
        self.assertEqual(status, 3)
        self.assertEqual(retry.call_count, 2)


class GlobalProductionRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old_input = self.root / "old-input"
        self.new_input = self.root / "new-input"
        self.old_input.mkdir()
        self.new_input.mkdir()
        for directory in (self.old_input, self.new_input):
            (directory / "Café, la hija's historia.mp3").write_bytes(b"same canonical audio")
            (directory / "Café, la hija's historia.txt").write_text("mismo guion", encoding="utf-8")
        self.title = "Café, la hija's historia"

    def tearDown(self):
        self.temp.cleanup()

    def test_cross_batch_valid_output_skips_renderer_and_creates_destination(self):
        old_final = self.root / "storage/batch_outputs/old-batch" / f"{self.title}.mp4"
        old_final.parent.mkdir(parents=True)
        old_final.write_bytes(b"valid")
        old_job = produce_batch.Job(self.title, self.old_input / f"{self.title}.mp3", self.old_input / f"{self.title}.txt", None, "old-batch")
        record = produce_batch.production_identity(
            old_job.stem, old_job.mp3, old_job.txt, "series", produce_batch.production_recipe_for("none", "karaoke", "bottom"),
        )
        new_job = produce_batch.Job(self.title, self.new_input / f"{self.title}.mp3", self.new_input / f"{self.title}.txt", None, "new-batch")
        out = self.root / "storage/batch_outputs/new-batch"
        out.mkdir(parents=True)
        report_path = out / produce_batch.REPORT_NAME
        report = produce_batch.init_report("new-batch", [new_job], report_path)

        def valid_video(path): return path.is_file() and path.read_bytes() == b"valid"
        with patch.object(produce_batch, "HOST_ROOT", self.root), patch.object(produce_batch, "valid_mp4", valid_video), \
             patch.object(produce_batch, "run_worker") as worker, patch.object(produce_batch, "run_hyperframes") as hyperframes:
            produce_batch.production_registry().upsert(record, old_final, "old-batch", 10.0)
            status = produce_batch.process_job(new_job, index=1, total=1, batch_output_dir=out, report=report,
                                               report_path=report_path, preset="karaoke", position="bottom", material_title="series")
            repeated = produce_batch.process_job(new_job, index=1, total=1, batch_output_dir=out, report=report,
                                                  report_path=report_path, preset="karaoke", position="bottom", material_title="series")
        self.assertEqual(status, "completed")
        self.assertEqual(repeated, "completed")
        self.assertEqual((out / f"{self.title}.mp4").read_bytes(), b"valid")
        worker.assert_not_called()
        hyperframes.assert_not_called()

    def test_changed_audio_or_script_has_a_different_identity(self):
        audio = self.old_input / f"{self.title}.mp3"
        script = self.old_input / f"{self.title}.txt"
        original = produce_batch.production_identity(self.title, audio, script, "series")
        audio.write_bytes(b"changed audio")
        self.assertNotEqual(original["production_fingerprint"], produce_batch.production_identity(self.title, audio, script, "series")["production_fingerprint"])
        audio.write_bytes(b"same canonical audio")
        script.write_text("guion cambiado", encoding="utf-8")
        self.assertNotEqual(original["production_fingerprint"], produce_batch.production_identity(self.title, audio, script, "series")["production_fingerprint"])

    def test_corrupt_registry_target_is_rejected_and_removed(self):
        audio = self.old_input / f"{self.title}.mp3"
        script = self.old_input / f"{self.title}.txt"
        corrupt = self.root / "corrupt.mp4"
        corrupt.write_bytes(b"corrupt")
        record = produce_batch.production_identity(self.title, audio, script)
        with patch.object(produce_batch, "HOST_ROOT", self.root), patch.object(produce_batch, "valid_mp4", lambda _: False):
            registry = produce_batch.production_registry()
            registry.upsert(record, corrupt, "old", 1.0)
            self.assertIsNone(registry.find_valid(record["production_fingerprint"], produce_batch.valid_mp4))
            self.assertIsNone(registry.find_valid(record["production_fingerprint"], produce_batch.valid_mp4))

    def test_backfill_skips_completed_output_without_production_plan_path(self):
        output_dir = self.root / "storage" / "batch_outputs" / "completed-batch"
        output_dir.mkdir(parents=True)
        final_mp4 = output_dir / f"{self.title}.mp4"
        final_mp4.write_bytes(b"valid")
        produce_batch.write_json_atomic(output_dir / produce_batch.REPORT_NAME, {
            "batch_id": "completed-batch",
            "jobs": {
                self.title: {
                    "status": "completed",
                    "batch_final": final_mp4.as_posix(),
                    "production_plan_path": None,
                },
            },
        })

        with patch.object(produce_batch, "HOST_ROOT", self.root), \
             patch.object(produce_batch, "valid_mp4", return_value=True), \
             patch.object(produce_batch, "production_registry") as registry:
            produce_batch.backfill_completed()

        registry.return_value.upsert.assert_not_called()


if __name__ == "__main__":
    unittest.main()
