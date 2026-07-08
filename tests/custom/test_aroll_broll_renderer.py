import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.custom.aroll_broll_renderer import (
    ArollBrollAsset,
    ArollBrollRenderPlan,
    ArollBrollRendererError,
    build_alternating_fullscreen_ffmpeg_command,
    build_alternating_fullscreen_timeline,
    build_aroll_broll_output_path,
    build_ffprobe_duration_command,
    detect_asset_kind,
    extract_broll_assets_from_manifest,
    parse_ffprobe_duration,
    run_aroll_broll_render,
    validate_aroll_path,
)


class TestArollBrollRenderer(unittest.TestCase):
    def _make_project_files(self, root: Path):
        aroll = root / "storage" / "local_videos" / "presenter.mp4"
        broll = root / "storage" / "local_assets" / "cutaway.mp4"
        still = root / "storage" / "local_images" / "still.png"
        for path in (aroll, broll, still):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"dummy")
        return aroll, broll, still

    def _make_plan(self, root: Path):
        aroll, broll, still = self._make_project_files(root)
        assets = [
            ArollBrollAsset(path=broll, kind="video", label="clip"),
            ArollBrollAsset(path=still, kind="image", label="still"),
        ]
        timeline = build_alternating_fullscreen_timeline(18, assets, 4, "high")
        return ArollBrollRenderPlan(
            a_roll_path=aroll,
            b_roll_assets=assets,
            output_path=build_aroll_broll_output_path("task_001", root),
            timeline=timeline,
            aroll_duration_seconds=18,
        )

    def _segment_duration_sum(self, timeline):
        return sum(float(item["end"]) - float(item["start"]) for item in timeline)

    def test_build_ffprobe_duration_command_returns_list_with_ffprobe(self):
        command = build_ffprobe_duration_command("/tmp/video.mp4")

        self.assertIsInstance(command, list)
        self.assertEqual(command[0], "ffprobe")
        self.assertIn("/tmp/video.mp4", command)

    def test_parse_ffprobe_duration_accepts_valid_stdout(self):
        self.assertEqual(parse_ffprobe_duration("12.345\n"), 12.345)

    def test_parse_ffprobe_duration_rejects_invalid_stdout(self):
        with self.assertRaises(ArollBrollRendererError):
            parse_ffprobe_duration("not-a-number")

    def test_path_safety_accepts_aroll_under_local_videos(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            aroll, _, _ = self._make_project_files(root)

            self.assertEqual(
                validate_aroll_path("storage/local_videos/presenter.mp4", root),
                aroll.resolve(),
            )

    def test_path_safety_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_project_files(root)

            with self.assertRaises(ArollBrollRendererError):
                validate_aroll_path(
                    "storage/local_videos/../local_videos/presenter.mp4",
                    root,
                )

    def test_path_safety_rejects_file_outside_allowed_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root / "outside.mp4"
            outside.write_bytes(b"dummy")

            with self.assertRaises(ArollBrollRendererError):
                validate_aroll_path(outside, root)

    def test_detect_asset_kind_by_extension(self):
        self.assertEqual(detect_asset_kind("clip.webm"), "video")
        self.assertEqual(detect_asset_kind("still.webp"), "image")

    def test_extract_broll_assets_from_manifest_reads_flexible_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "job-assets"
            bundle = base / "jab_test"
            video = bundle / "scene-01" / "clip.mp4"
            image = bundle / "scene-02" / "still.png"
            for path in (video, image):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"dummy")
            manifest = bundle / "manifests" / "renderer-manifest.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text(
                json.dumps(
                    {
                        "scenes": [
                            {
                                "assets": [
                                    {"type": "video", "local_path": str(video)},
                                    {"media_type": "image", "path": "scene-02/still.png"},
                                    {"kind": "video", "source_path": "missing.mp4"},
                                ]
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            result = extract_broll_assets_from_manifest(
                manifest,
                allowed_root=base,
            )

        self.assertEqual([asset.kind for asset in result["assets"]], ["video", "image"])
        self.assertTrue(result["warnings"])

    def test_manifest_outside_data_job_assets_is_rejected_without_test_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "renderer-manifest.json"
            manifest.write_text("{}", encoding="utf-8")

            with self.assertRaises(ArollBrollRendererError):
                extract_broll_assets_from_manifest(manifest)

    def test_timeline_with_broll_never_exceeds_aroll_duration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, broll, _ = self._make_project_files(root)
            timeline = build_alternating_fullscreen_timeline(
                11,
                [ArollBrollAsset(path=broll, kind="video")],
                4,
                "high",
            )

        self.assertLessEqual(max(item["end"] for item in timeline), 11)

    def test_timeline_clamps_final_broll_to_aroll_duration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, broll, _ = self._make_project_files(root)
            timeline = build_alternating_fullscreen_timeline(
                6,
                [ArollBrollAsset(path=broll, kind="video")],
                4,
                "medium",
            )

        self.assertEqual(timeline[-1]["visual"], "b_roll")
        self.assertEqual(timeline[-1]["start"], 5.0)
        self.assertEqual(timeline[-1]["end"], 6.0)

    def test_timeline_does_not_produce_zero_or_negative_segments(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, broll, _ = self._make_project_files(root)
            timeline = build_alternating_fullscreen_timeline(
                6,
                [ArollBrollAsset(path=broll, kind="video")],
                4,
                "medium",
            )

        self.assertTrue(timeline)
        self.assertTrue(
            all(float(item["end"]) > float(item["start"]) for item in timeline)
        )

    def test_timeline_for_six_second_aroll_medium_frequency_ends_within_aroll(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, broll, _ = self._make_project_files(root)
            timeline = build_alternating_fullscreen_timeline(
                6,
                [ArollBrollAsset(path=broll, kind="video")],
                4,
                "medium",
            )

        self.assertLessEqual(max(item["end"] for item in timeline), 6)

    def test_timeline_segment_duration_sum_does_not_exceed_aroll_duration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, broll, _ = self._make_project_files(root)
            timeline = build_alternating_fullscreen_timeline(
                6,
                [ArollBrollAsset(path=broll, kind="video")],
                4,
                "medium",
            )

        self.assertLessEqual(self._segment_duration_sum(timeline), 6)

    def test_command_builder_returns_list_not_string(self):
        with tempfile.TemporaryDirectory() as tmp:
            command = build_alternating_fullscreen_ffmpeg_command(
                self._make_plan(Path(tmp))
            )

        self.assertIsInstance(command, list)

    def test_command_includes_ffmpeg(self):
        with tempfile.TemporaryDirectory() as tmp:
            command = build_alternating_fullscreen_ffmpeg_command(
                self._make_plan(Path(tmp))
            )

        self.assertEqual(command[0], "ffmpeg")

    def test_command_includes_filter_complex(self):
        with tempfile.TemporaryDirectory() as tmp:
            command = build_alternating_fullscreen_ffmpeg_command(
                self._make_plan(Path(tmp))
            )

        self.assertIn("-filter_complex", command)

    def test_command_maps_visual_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            command = build_alternating_fullscreen_ffmpeg_command(
                self._make_plan(Path(tmp))
            )

        self.assertIn("-map", command)
        self.assertIn("[vout]", command)

    def test_command_maps_optional_aroll_audio(self):
        with tempfile.TemporaryDirectory() as tmp:
            command = build_alternating_fullscreen_ffmpeg_command(
                self._make_plan(Path(tmp))
            )

        self.assertIn("0:a?", command)

    def test_command_includes_aroll_duration_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = self._make_plan(Path(tmp))
            command = build_alternating_fullscreen_ffmpeg_command(plan)

        self.assertIn("-t", command)
        self.assertEqual(command[command.index("-t") + 1], "18")

    def test_command_duration_limit_appears_before_output_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = self._make_plan(Path(tmp))
            command = build_alternating_fullscreen_ffmpeg_command(plan)

        self.assertLess(command.index("-t"), command.index(plan.output_path.as_posix()))

    def test_command_uses_aroll_duration_when_timeline_exceeds_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            aroll, broll, _ = self._make_project_files(root)
            plan = ArollBrollRenderPlan(
                a_roll_path=aroll,
                b_roll_assets=[ArollBrollAsset(path=broll, kind="video")],
                output_path=build_aroll_broll_output_path("task_001", root),
                timeline=[
                    {"start": 0, "end": 5, "visual": "a_roll"},
                    {
                        "start": 5,
                        "end": 9,
                        "visual": "b_roll",
                        "broll_index": 0,
                    },
                    {"start": 9, "end": 12, "visual": "a_roll"},
                ],
                aroll_duration_seconds=6,
            )

            command = build_alternating_fullscreen_ffmpeg_command(plan)
            filtergraph = command[command.index("-filter_complex") + 1]

        self.assertEqual(command[command.index("-t") + 1], "6")
        self.assertIn("trim=start=0:end=5", filtergraph)
        self.assertIn("trim=start=0:duration=1", filtergraph)
        self.assertNotIn("trim=start=9:end=12", filtergraph)

    def test_command_does_not_map_broll_audio(self):
        with tempfile.TemporaryDirectory() as tmp:
            command = build_alternating_fullscreen_ffmpeg_command(
                self._make_plan(Path(tmp))
            )

        self.assertNotIn("1:a", command)
        self.assertNotIn("2:a", command)

    def test_command_includes_final_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            command = build_alternating_fullscreen_ffmpeg_command(
                self._make_plan(Path(tmp))
            )

        self.assertTrue(command[-1].endswith("/storage/tasks/task_001/final-1.mp4"))

    def test_run_aroll_broll_render_dry_run_does_not_execute_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = self._make_plan(Path(tmp))

            def runner(*args, **kwargs):
                raise AssertionError("runner should not execute in dry_run")

            result = run_aroll_broll_render(plan, runner=runner, dry_run=True)

        self.assertTrue(result["ok"])
        self.assertTrue(result["dry_run"])
        self.assertIsNone(result["returncode"])

    def test_run_aroll_broll_render_dry_run_does_not_create_output_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = self._make_plan(Path(tmp))
            output_parent = plan.output_path.parent

            result = run_aroll_broll_render(plan, dry_run=True)

            self.assertTrue(result["ok"])
            self.assertFalse(output_parent.exists())

    def test_run_aroll_broll_render_with_fake_runner_uses_command_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            plan = self._make_plan(Path(tmp))

            def runner(command, cwd, timeout):
                calls.append((command, cwd, timeout))
                return {"returncode": 0, "stdout": "ok", "stderr": ""}

            result = run_aroll_broll_render(plan, runner=runner)

        self.assertTrue(result["ok"])
        self.assertIsInstance(calls[0][0], list)
        self.assertEqual(result["stdout"], "ok")

    def test_run_aroll_broll_render_execute_creates_output_parent_before_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = self._make_plan(Path(tmp))
            output_parent = plan.output_path.parent

            def runner(command, cwd, timeout):
                self.assertTrue(output_parent.exists())
                return {"returncode": 0, "stdout": "ok", "stderr": ""}

            result = run_aroll_broll_render(plan, runner=runner)

            self.assertTrue(result["ok"])
            self.assertTrue(output_parent.exists())

    def test_run_aroll_broll_render_execute_success_returns_ok_true(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = self._make_plan(Path(tmp))

            def runner(command, cwd, timeout):
                return {"returncode": 0, "stdout": "done", "stderr": ""}

            result = run_aroll_broll_render(plan, runner=runner)

        self.assertTrue(result["ok"])
        self.assertFalse(result["dry_run"])
        self.assertEqual(result["returncode"], 0)

    def test_run_aroll_broll_render_execute_failure_returns_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = self._make_plan(Path(tmp))

            def runner(command, cwd, timeout):
                return {"returncode": 1, "stdout": "partial", "stderr": "boom"}

            result = run_aroll_broll_render(plan, runner=runner)

        self.assertFalse(result["ok"])
        self.assertFalse(result["dry_run"])
        self.assertEqual(result["returncode"], 1)
        self.assertEqual(result["stdout"], "partial")
        self.assertEqual(result["stderr"], "boom")

    def test_output_path_stays_under_task_final_mp4(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = build_aroll_broll_output_path("task-abc_123", Path(tmp))

        self.assertTrue(
            output.as_posix().endswith("/storage/tasks/task-abc_123/final-1.mp4")
        )


if __name__ == "__main__":
    unittest.main()
