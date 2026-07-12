import json
import importlib.util
import inspect
import os
import sys
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.custom.kurukin_job_adapter import KurukinJobAdapterError
from app.custom.asset_source_policy import (
    ASSET_SOURCE_MODE_EXCLUSIVE_BRAND_ASSETS,
    ASSET_SOURCE_MODE_LOCAL_ONLY,
    ASSET_SOURCE_MODE_OPEN_SOURCES,
)
from app.custom.kurukin_job_queue import (
    CONTAINER_API_BASE_URL,
    CONTAINER_NIGHTLY_QUEUE_DIR,
    MANUAL_RUNNER_EXECUTION_MODE,
    MANUAL_RUNNER_MAX_JOBS,
    RUNNER_CONFIRM_TEXT,
    RUNNER_QUEUE_CONFIRM_TEXT,
    build_safe_runner_command,
    build_preflight_checks,
    detect_task_outputs,
    detect_runner_candidates,
    enqueue_moneyprinter_payload,
    get_job_lifecycle_summary,
    get_runner_preflight_summary,
    get_storage_usage_summary,
    infer_task_status,
    is_aroll_broll_queue_enabled,
    is_aroll_broll_renderer_enabled,
    is_ui_runner_enabled,
    list_rendered_videos,
    list_pending_jobs,
    list_task_summaries,
    run_controlled_runner,
    validate_runner_execution_request,
)
from app.custom.kurukin_render_console import (
    ASSET_SOURCE_LOCAL,
    ASSET_SOURCE_ASSET_HUB,
    ASSET_SOURCE_STOCK,
    MPT_ENGINE_SUBMIT_FLAG,
    PEXELS_SOURCE_FLAG,
    SOURCE_MODE_ASSET_HUB,
    SOURCE_MODE_LOCAL,
    SOURCE_MODE_STOCK,
    build_aroll_broll_payload_from_console,
    build_operator_summary,
    build_render_console_spec,
    build_workflow_payload,
    default_asset_hub_manifest_path,
    enqueue_aroll_broll_from_console,
    enqueue_job_intent_from_console,
    get_manifest_summary_for_ui,
    is_mpt_engine_submit_enabled,
    is_pexels_source_enabled,
    list_local_storage_files,
    normalize_aroll_broll_local_asset_paths,
    prepare_broll_assets_from_console,
    process_queued_intent_batch_with_mpt_native,
    process_queued_intent_with_mpt_native,
    safe_relative_path,
    submit_mpt_native_local_job_from_console,
    validate_and_build_payload_from_console_spec,
)


BUNDLE_UID = "jab_b28367fb22d44a40bae507c175f464c4"
EXPECTED_MANUAL_RUNNER_COMMAND = [
    "python3",
    "scripts/nightly_runner.py",
    "--max-jobs",
    "1",
    "--ignore-window",
    "--queue-dir",
    CONTAINER_NIGHTLY_QUEUE_DIR,
    "--api-base-url",
    CONTAINER_API_BASE_URL,
]


def load_nightly_runner_module():
    spec = importlib.util.spec_from_file_location(
        "nightly_runner_for_tests",
        Path("scripts/nightly_runner.py"),
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def make_spec(**overrides):
    values = {
        "job_id": "render-console-test-001",
        "video_subject": "Render Console Test",
        "video_script": "Example script.",
        "render_quality": "draft_720p",
        "video_aspect": "9:16",
        "asset_hub_bundle_uid": BUNDLE_UID,
        "subtitles_mode": "none",
    }
    values.update(overrides)
    return build_render_console_spec(**values)


class TestKurukinRenderConsole(unittest.TestCase):
    class _FakeMaterial:
        def __init__(self, url):
            self.url = url

    class _FakeVideoParams:
        validated_payloads = []

        @classmethod
        def model_validate(cls, payload):
            cls.validated_payloads.append(payload)
            params = type("FakeParams", (), {})()
            params.video_source = payload.get("video_source")
            params.custom_audio_file = payload.get("custom_audio_file")
            params.video_materials = [
                TestKurukinRenderConsole._FakeMaterial(item.get("url"))
                for item in payload.get("video_materials", [])
            ]
            return params

    def _fake_mpt_bridge(self, seen_jobs):
        def bridge(job):
            seen_jobs.append(job)
            return {
                "ok": True,
                "spec": {
                    "video_subject": job["video_subject"],
                    "video_script": job["video_script"],
                    "video_source": job["video_source"],
                    "video_materials": job["video_materials"],
                    "custom_audio_file": job["custom_audio_file"],
                    "subtitle_enabled": job["subtitle_enabled"],
                },
                "validated_model": "VideoParams",
                "errors": [],
            }

        return bridge

    def _intent_queue_payload(self, **overrides):
        payload = {
            "task_id": "manual-intent-001",
            "job_id": "manual-intent-001",
            "mode": "audio_to_video",
            "status": "QUEUED",
            "source": "job_intent_v1",
            "original_intent": {
                "mode": "audio_to_video",
                "task_id": "manual-intent-001",
                "audio_path": "storage/local_audios/audio.mp3",
                "topic": "Manual intent",
            },
            "normalized_intent": {
                "mode": "audio_to_video",
                "task_id": "manual-intent-001",
                "audio_path": "storage/local_audios/audio.mp3",
                "video_path": "storage/local_videos/visual.mp4",
                "visual_path": "storage/local_videos/visual.mp4",
                "resolved_visual_path": "storage/local_videos/visual.mp4",
                "topic": "Manual intent",
            },
            "resolved_visual_path": "storage/local_videos/visual.mp4",
            "visual_autofill_source": "local_picker_v1",
            "compiled_mpt_spec": {
                "kind": "mpt_video_task_spec",
                "execution": "spec_only",
                "safe_to_build_without_side_effects": True,
                "mpt_params": {
                    "video_subject": "Manual intent",
                    "video_script": "Manual intent script.",
                    "video_source": "local",
                    "video_materials": [
                        {
                            "provider": "local",
                            "url": "storage/local_videos/visual.mp4",
                            "duration": 0,
                        }
                    ],
                    "custom_audio_file": "storage/local_audios/audio.mp3",
                    "subtitle_enabled": False,
                },
            },
            "guardrails": {
                "external_providers_allowed": False,
                "ai_generation_allowed": False,
                "asset_hub_api_allowed": False,
                "real_render_started": False,
            },
        }
        payload.update(overrides)
        return payload

    def _write_intent_queue_item(
        self,
        root: Path,
        payload: dict | None = None,
        *,
        filename: str = "20260712-120000-manual-intent.json",
    ) -> Path:
        pending = root / "pending" / filename
        pending.parent.mkdir(parents=True, exist_ok=True)
        pending.write_text(
            json.dumps(payload or self._intent_queue_payload()),
            encoding="utf-8",
        )
        return pending

    def _aroll_broll_local_config(self, root: Path) -> dict:
        aroll = root / "storage" / "local_videos" / "presenter.mp4"
        broll = root / "storage" / "local_videos" / "cutaway.mp4"
        aroll.parent.mkdir(parents=True, exist_ok=True)
        broll.parent.mkdir(parents=True, exist_ok=True)
        aroll.write_bytes(b"aroll")
        broll.write_bytes(b"broll")
        return {
            "render_mode": "aroll_broll",
            "a_roll": {
                "source": "local_video",
                "path": "storage/local_videos/presenter.mp4",
                "audio_policy": "original",
                "crop": "center",
            },
            "b_roll": {
                "source": "local_assets",
                "assets": ["storage/local_videos/cutaway.mp4"],
                "clip_seconds": 2,
                "frequency": "medium",
                "audio_policy": "muted",
                "image_motion": "slow_zoom_in",
            },
            "layout": {
                "preset": "alternating_fullscreen",
                "aspect_ratio": "9:16",
                "speaker_position": "center",
                "subtitle_safe_area": True,
            },
            "subtitles": {
                "source": "none",
                "provider": "none",
                "custom_srt_path": "",
            },
        }

    def test_normalize_aroll_broll_local_asset_paths_accepts_storage_paths(self):
        paths = normalize_aroll_broll_local_asset_paths(
            "storage/local_videos/cutaway.mp4\nstorage/local_images/still.png"
        )

        self.assertEqual(
            paths,
            [
                "storage/local_videos/cutaway.mp4",
                "storage/local_images/still.png",
            ],
        )

    def test_normalize_aroll_broll_local_asset_paths_rejects_traversal(self):
        with self.assertRaises(ValueError):
            normalize_aroll_broll_local_asset_paths(
                "storage/local_videos/../secret.mp4"
            )

    def test_prepare_broll_assets_local_only_with_three_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = prepare_broll_assets_from_console(
                project_root=Path(tmp),
                asset_policy={"mode": ASSET_SOURCE_MODE_LOCAL_ONLY},
                query="city walk",
                desired_count=3,
                local_candidates=[
                    "storage/local_videos/one.mp4",
                    "storage/local_assets/two.mp4",
                    "storage/local_images/three.png",
                ],
            )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["b_roll_asset_count"], 3)
        self.assertEqual(result["source_provider"], "local_library")
        self.assertEqual(result["query"], "city walk")
        self.assertEqual(result["asset_policy"]["mode"], ASSET_SOURCE_MODE_LOCAL_ONLY)
        self.assertEqual(result["asset_policy_label"], "Asset policy: Local only")

    def test_prepare_broll_assets_multiline_candidates_parse_and_dedupe(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = prepare_broll_assets_from_console(
                project_root=Path(tmp),
                asset_policy={"mode": ASSET_SOURCE_MODE_LOCAL_ONLY},
                query=None,
                desired_count=2,
                local_candidates=(
                    "storage/local_videos/one.mp4\n"
                    "storage/local_videos/one.mp4\n"
                    "storage/local_assets/two.mp4"
                ),
            )

        self.assertTrue(result["ok"], result)
        self.assertEqual(
            result["b_roll_assets"],
            ["storage/local_videos/one.mp4", "storage/local_assets/two.mp4"],
        )

    def test_prepare_broll_assets_desired_count_clamps_to_eight(self):
        candidates = [
            f"storage/local_videos/candidate-{index}.mp4"
            for index in range(1, 10)
        ]

        with tempfile.TemporaryDirectory() as tmp:
            result = prepare_broll_assets_from_console(
                project_root=Path(tmp),
                asset_policy={"mode": ASSET_SOURCE_MODE_LOCAL_ONLY},
                query="",
                desired_count=99,
                local_candidates=candidates,
            )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["b_roll_asset_count"], 8)

    def test_prepare_broll_assets_local_only_fails_with_too_few_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = prepare_broll_assets_from_console(
                project_root=Path(tmp),
                asset_policy={"mode": ASSET_SOURCE_MODE_LOCAL_ONLY},
                query="city",
                desired_count=2,
                local_candidates="storage/local_videos/one.mp4",
            )

        self.assertFalse(result["ok"])
        self.assertEqual(
            result["error"],
            "Local-only policy requires enough local candidates",
        )
        self.assertEqual(result["b_roll_asset_count"], 1)

    def test_prepare_broll_assets_open_sources_uses_enough_candidates(self):
        calls = []

        def fake_downloader(_request):
            calls.append("called")
            return ["storage/local_videos/downloaded.mp4"]

        with tempfile.TemporaryDirectory() as tmp:
            result = prepare_broll_assets_from_console(
                project_root=Path(tmp),
                asset_policy={"mode": ASSET_SOURCE_MODE_OPEN_SOURCES},
                query="city",
                desired_count=2,
                local_candidates=(
                    "storage/local_videos/one.mp4\n"
                    "storage/local_assets/two.mp4"
                ),
                downloader=fake_downloader,
            )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["source_provider"], "local_library")
        self.assertEqual(calls, [])

    def test_prepare_broll_assets_open_sources_without_downloader_fails_clearly(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = prepare_broll_assets_from_console(
                project_root=Path(tmp),
                asset_policy={"mode": ASSET_SOURCE_MODE_OPEN_SOURCES},
                query="city",
                desired_count=2,
                local_candidates="storage/local_videos/one.mp4",
            )

        self.assertFalse(result["ok"])
        self.assertEqual(
            result["error"],
            "No hay suficientes assets locales. Pexels no está activo en esta consola.",
        )

    def test_prepare_broll_assets_flag_off_does_not_attempt_pexels(self):
        calls = []

        def fake_pexels_downloader(_request):
            calls.append("called")
            return {"assets": ["storage/local_videos/pexels.mp4"]}

        with tempfile.TemporaryDirectory() as tmp:
            result = prepare_broll_assets_from_console(
                project_root=Path(tmp),
                asset_policy={"mode": ASSET_SOURCE_MODE_OPEN_SOURCES},
                query="city",
                desired_count=1,
                local_candidates="",
                pexels_downloader=fake_pexels_downloader,
                environ={PEXELS_SOURCE_FLAG: "0"},
            )

        self.assertFalse(result["ok"])
        self.assertEqual(calls, [])
        self.assertEqual(
            result["error"],
            "No hay suficientes assets locales. Pexels no está activo en esta consola.",
        )

    def test_prepare_broll_assets_open_sources_fake_downloader_completes_assets(self):
        seen_requests = []

        def fake_downloader(request):
            seen_requests.append(request)
            return {
                "source_provider": "pexels",
                "assets": ["storage/local_videos/downloaded.mp4"],
                "metadata": {"fake": True},
            }

        with tempfile.TemporaryDirectory() as tmp:
            result = prepare_broll_assets_from_console(
                project_root=Path(tmp),
                asset_policy={"mode": ASSET_SOURCE_MODE_OPEN_SOURCES},
                query="city",
                desired_count=2,
                local_candidates="storage/local_assets/local.mp4",
                downloader=fake_downloader,
            )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["source_provider"], "mixed")
        self.assertEqual(result["b_roll_asset_count"], 2)
        self.assertEqual(seen_requests[0]["needed_count"], 1)
        self.assertTrue(result["metadata"]["fake"])

    def test_prepare_broll_assets_flag_on_uses_injected_pexels_downloader(self):
        seen_requests = []

        def fake_pexels_downloader(request):
            seen_requests.append(request)
            return {
                "source_provider": "pexels",
                "assets": ["storage/local_videos/pexels-city.mp4"],
                "metadata": {"source_provider": "pexels"},
            }

        with tempfile.TemporaryDirectory() as tmp:
            result = prepare_broll_assets_from_console(
                project_root=Path(tmp),
                asset_policy={"mode": ASSET_SOURCE_MODE_OPEN_SOURCES},
                query="city",
                desired_count=1,
                local_candidates="",
                pexels_downloader=fake_pexels_downloader,
                environ={PEXELS_SOURCE_FLAG: "1"},
            )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["source_provider"], "pexels")
        self.assertEqual(result["b_roll_assets"], ["storage/local_videos/pexels-city.mp4"])
        self.assertEqual(seen_requests[0]["needed_count"], 1)

    def test_prepare_broll_assets_combines_generic_source_adapters(self):
        calls = []

        def fake_asset_hub(_request):
            calls.append("asset_hub")
            return {"assets": ["storage/local_assets/hub.mp4"]}

        with tempfile.TemporaryDirectory() as tmp:
            result = prepare_broll_assets_from_console(
                project_root=Path(tmp),
                asset_policy={
                    "mode": ASSET_SOURCE_MODE_OPEN_SOURCES,
                    "allowed_sources": ["local_library", "asset_hub"],
                },
                query="city",
                desired_count=2,
                local_candidates="storage/local_videos/local.mp4",
                source_adapters={"asset_hub": fake_asset_hub},
            )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["source_provider"], "mixed")
        self.assertEqual(calls, ["asset_hub"])

    def test_pexels_source_flag_helper_defaults_off(self):
        self.assertFalse(is_pexels_source_enabled({}))
        self.assertFalse(is_pexels_source_enabled({PEXELS_SOURCE_FLAG: "0"}))
        self.assertTrue(is_pexels_source_enabled({PEXELS_SOURCE_FLAG: "1"}))

    def test_mpt_engine_submit_flag_helper_defaults_off(self):
        self.assertFalse(is_mpt_engine_submit_enabled({}))
        self.assertFalse(is_mpt_engine_submit_enabled({MPT_ENGINE_SUBMIT_FLAG: "0"}))
        self.assertTrue(is_mpt_engine_submit_enabled({MPT_ENGINE_SUBMIT_FLAG: "1"}))

    def test_mpt_native_local_submit_flag_off_blocks_task_start(self):
        calls = []

        result = submit_mpt_native_local_job_from_console(
            task_id="mpt-console-test-001",
            video_local_path="storage/local_videos/clip.mp4",
            audio_local_path="storage/local_audios/audio.mp3",
            video_subject="Console test",
            video_script="Console test script.",
            environ={},
            task_start=lambda **kwargs: calls.append(kwargs),
        )

        self.assertFalse(result["ok"])
        self.assertEqual(calls, [])
        self.assertIn(MPT_ENGINE_SUBMIT_FLAG, result["errors"][0])
        self.assertEqual(
            result["expected_output"],
            "storage/tasks/mpt-console-test-001/final-1.mp4",
        )

    def test_mpt_native_local_submit_flag_on_validates_and_calls_task_start_once(self):
        calls = []
        seen_jobs = []
        self._FakeVideoParams.validated_payloads = []

        def fake_task_start(*, task_id, params):
            calls.append({"task_id": task_id, "params": params})

        with tempfile.TemporaryDirectory() as tmp:
            pending_dir = Path(tmp) / "storage" / "nightly_jobs" / "pending"
            result = submit_mpt_native_local_job_from_console(
                task_id="mpt-console-test-001",
                video_local_path="storage/local_videos/clip.mp4",
                audio_local_path="storage/local_audios/audio.mp3",
                video_subject="Console test",
                video_script="Console test script.",
                environ={MPT_ENGINE_SUBMIT_FLAG: "1"},
                task_start=fake_task_start,
                bridge_builder=self._fake_mpt_bridge(seen_jobs),
                video_params_model=self._FakeVideoParams,
            )

        self.assertTrue(result["ok"], result)
        self.assertEqual(seen_jobs[0]["video_materials"][0]["url"], "clip.mp4")
        self.assertEqual(seen_jobs[0]["video_materials"][0]["provider"], "local")
        self.assertEqual(seen_jobs[0]["video_source"], "local")
        self.assertEqual(seen_jobs[0]["asset_policy"]["mode"], "local_only")
        self.assertFalse(
            seen_jobs[0]["kurukin_metadata"]["external_providers_allowed"]
        )
        self.assertNotIn("/api/v1/videos", json.dumps(seen_jobs[0]))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["task_id"], "mpt-console-test-001")
        self.assertEqual(calls[0]["params"].video_source, "local")
        self.assertEqual(
            calls[0]["params"].custom_audio_file,
            "storage/local_audios/audio.mp3",
        )
        self.assertEqual(calls[0]["params"].video_materials[0].url, "clip.mp4")
        self.assertEqual(len(self._FakeVideoParams.validated_payloads), 1)
        self.assertEqual(result["validated_model"], "VideoParams")
        self.assertFalse(pending_dir.exists())

    def test_mpt_native_local_submit_does_not_call_queue_api_or_runner_helpers(self):
        calls = []
        seen_jobs = []
        source = inspect.getsource(submit_mpt_native_local_job_from_console)
        self.assertNotIn("run_controlled_runner", source)
        self.assertNotIn("nightly_runner", source)
        self.assertNotIn("/api/v1/videos", source)
        self.assertNotIn("pexels", source.lower())
        self.assertNotIn("pixabay", source.lower())
        self.assertNotIn("coverr", source.lower())

        with mock.patch(
            "app.custom.kurukin_render_console.enqueue_moneyprinter_payload",
            side_effect=AssertionError("pending queue must not be used"),
        ):
            result = submit_mpt_native_local_job_from_console(
                task_id="mpt-console-test-002",
                video_local_path="local_videos/clip.mp4",
                audio_local_path="local_audios/audio.mp3",
                video_subject="Console test",
                video_script="Console test script.",
                environ={MPT_ENGINE_SUBMIT_FLAG: "1"},
                task_start=lambda **kwargs: calls.append(kwargs),
                bridge_builder=self._fake_mpt_bridge(seen_jobs),
                video_params_model=self._FakeVideoParams,
            )

        self.assertTrue(result["ok"], result)
        self.assertEqual(len(calls), 1)

    def test_mpt_native_local_submit_errors_are_redacted(self):
        result = submit_mpt_native_local_job_from_console(
            task_id="mpt-console-test-003",
            video_local_path="clip.mp4",
            audio_local_path="audio.mp3",
            video_subject="Console test",
            video_script="Console test script.",
            environ={MPT_ENGINE_SUBMIT_FLAG: "1"},
            bridge_builder=lambda _job: (_ for _ in ()).throw(
                RuntimeError("api_key=super-secret")
            ),
            task_start=lambda **_kwargs: None,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["errors"], ["<redacted>"])

    def test_prepare_broll_assets_exclusive_brand_without_manifest_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = prepare_broll_assets_from_console(
                project_root=Path(tmp),
                asset_policy={
                    "mode": ASSET_SOURCE_MODE_EXCLUSIVE_BRAND_ASSETS,
                    "brand_asset_bundle_uid": "jab_test",
                },
                query="brand",
                desired_count=1,
                local_candidates="",
            )

        self.assertFalse(result["ok"])
        self.assertEqual(
            result["error"],
            "Exclusive brand assets require a local manifest",
        )

    def test_prepare_broll_assets_helper_does_not_create_pending_or_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = prepare_broll_assets_from_console(
                project_root=root,
                asset_policy={"mode": ASSET_SOURCE_MODE_LOCAL_ONLY},
                query="city",
                desired_count=1,
                local_candidates="storage/local_videos/one.mp4",
            )

            self.assertTrue(result["ok"], result)
            self.assertFalse((root / "storage" / "nightly_jobs" / "pending").exists())
            self.assertFalse((root / "storage" / "tasks").exists())

    def test_build_aroll_broll_payload_from_console_sets_task_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self._aroll_broll_local_config(root)
            config["asset_policy"] = {"mode": ASSET_SOURCE_MODE_LOCAL_ONLY}
            payload = build_aroll_broll_payload_from_console(
                config,
                job_id="aroll-broll-ui-smoke-004",
                project_root=root,
                render_quality="draft_720p",
                task_id="aroll-broll-ui-smoke-004",
                created_by="render_console_ui_smoke_004",
            )

        self.assertEqual(payload["render_mode"], "aroll_broll")
        self.assertEqual(payload["job_id"], "aroll-broll-ui-smoke-004")
        self.assertEqual(payload["task_id"], "aroll-broll-ui-smoke-004")
        self.assertEqual(payload["created_by"], "render_console_ui_smoke_004")
        self.assertEqual(payload["asset_policy"]["mode"], ASSET_SOURCE_MODE_LOCAL_ONLY)
        self.assertEqual(
            payload["aroll_broll"]["asset_policy"]["mode"],
            ASSET_SOURCE_MODE_LOCAL_ONLY,
        )
        self.assertEqual(
            payload["aroll_broll"]["b_roll"]["assets"],
            [(root / "storage" / "local_videos" / "cutaway.mp4").as_posix()],
        )
        self.assertEqual(payload["b_roll_asset_count"], 1)

    def test_enqueue_aroll_broll_from_console_accepts_asset_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self._aroll_broll_local_config(root)
            second = root / "storage" / "local_images" / "still.png"
            second.parent.mkdir(parents=True)
            second.write_bytes(b"broll")
            config["b_roll"]["assets"] = [
                "storage/local_videos/cutaway.mp4",
                "storage/local_images/still.png",
            ]
            result = enqueue_aroll_broll_from_console(
                config,
                job_id="aroll-broll-list",
                project_root=root,
                queue_dir=root / "pending",
                environ={"KURUKIN_ENABLE_AROLL_BROLL_QUEUE": "1"},
            )
            payload = json.loads(
                Path(result["pending_path"]).read_text(encoding="utf-8")
            )

        self.assertEqual(payload["b_roll_asset_count"], 2)
        self.assertEqual(len(payload["aroll_broll"]["b_roll"]["assets"]), 2)

    def test_enqueue_job_intent_from_console_writes_pending_without_render(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = enqueue_job_intent_from_console(
                {
                    "mode": "audio_to_video",
                    "task_id": "console-intent-queue",
                    "audio_path": "storage/local_audios/audio.mp3",
                    "video_path": "storage/local_videos/visual.mp4",
                    "topic": "Console intent queue",
                },
                queue_dir=root / "pending",
                project_root=root,
            )
            payload = json.loads(
                Path(result["pending_path"]).read_text(encoding="utf-8")
            )

        self.assertTrue(result["ok"], result)
        self.assertEqual(payload["source"], "job_intent_v1")
        self.assertEqual(payload["status"], "QUEUED")
        self.assertEqual(payload["original_intent"]["task_id"], "console-intent-queue")
        self.assertFalse((root / "storage" / "tasks").exists())

    def test_process_queued_intent_requires_compiled_mpt_spec(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_intent_queue_item(
                Path(tmp),
                self._intent_queue_payload(compiled_mpt_spec={}),
            )
            submitter = mock.Mock(return_value={"ok": True})

            result = process_queued_intent_with_mpt_native(
                path,
                environ={MPT_ENGINE_SUBMIT_FLAG: "1"},
                submitter=submitter,
            )

        self.assertFalse(result["ok"])
        self.assertIn("compiled_mpt_spec is required", result["errors"])
        submitter.assert_not_called()

    def test_process_queued_intent_rejects_wrong_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_intent_queue_item(
                Path(tmp),
                self._intent_queue_payload(source="legacy_queue"),
            )
            submitter = mock.Mock(return_value={"ok": True})

            result = process_queued_intent_with_mpt_native(
                path,
                environ={MPT_ENGINE_SUBMIT_FLAG: "1"},
                submitter=submitter,
            )

        self.assertFalse(result["ok"])
        self.assertIn("queue item source must be job_intent_v1", result["errors"])
        submitter.assert_not_called()

    def test_process_queued_intent_rejects_external_guardrails(self):
        guardrail_cases = (
            "external_providers_allowed",
            "ai_generation_allowed",
            "asset_hub_api_allowed",
        )
        for key in guardrail_cases:
            with self.subTest(key=key), tempfile.TemporaryDirectory() as tmp:
                payload = self._intent_queue_payload()
                payload["guardrails"][key] = True
                path = self._write_intent_queue_item(Path(tmp), payload)
                submitter = mock.Mock(return_value={"ok": True})

                result = process_queued_intent_with_mpt_native(
                    path,
                    environ={MPT_ENGINE_SUBMIT_FLAG: "1"},
                    submitter=submitter,
                )

                self.assertFalse(result["ok"])
                self.assertIn(f"guardrails.{key} must be false", result["errors"])
                submitter.assert_not_called()

    def test_process_queued_intent_rejects_urls_in_audio_visual_or_spec(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._intent_queue_payload()
            payload["normalized_intent"]["audio_path"] = "https://example.test/audio.mp3"
            path = self._write_intent_queue_item(Path(tmp), payload)
            submitter = mock.Mock(return_value={"ok": True})

            result = process_queued_intent_with_mpt_native(
                path,
                environ={MPT_ENGINE_SUBMIT_FLAG: "1"},
                submitter=submitter,
            )

        self.assertFalse(result["ok"])
        self.assertTrue(
            any("URLs are not allowed" in error for error in result["errors"])
        )
        submitter.assert_not_called()

    def test_process_queued_intent_flag_off_does_not_render(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_intent_queue_item(Path(tmp))
            submitter = mock.Mock(return_value={"ok": True})

            result = process_queued_intent_with_mpt_native(
                path,
                environ={},
                submitter=submitter,
            )
            payload_after = json.loads(path.read_text(encoding="utf-8"))

        self.assertFalse(result["ok"])
        self.assertIn(MPT_ENGINE_SUBMIT_FLAG, result["errors"][0])
        self.assertEqual(payload_after["status"], "QUEUED")
        submitter.assert_not_called()

    def test_process_queued_intent_submitter_ok_marks_done_and_output_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_intent_queue_item(Path(tmp))
            submitter = mock.Mock(
                return_value={
                    "ok": True,
                    "expected_output": "storage/tasks/manual-intent-001/final-1.mp4",
                    "errors": [],
                }
            )

            result = process_queued_intent_with_mpt_native(
                path,
                environ={MPT_ENGINE_SUBMIT_FLAG: "1"},
                submitter=submitter,
            )
            payload_after = json.loads(path.read_text(encoding="utf-8"))

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "DONE")
        self.assertEqual(
            result["output_path"],
            "storage/tasks/manual-intent-001/final-1.mp4",
        )
        self.assertEqual(payload_after["status"], "DONE")
        self.assertEqual(
            payload_after["output_path"],
            "storage/tasks/manual-intent-001/final-1.mp4",
        )
        self.assertTrue(payload_after["guardrails"]["real_render_started"])
        submitter.assert_called_once()
        self.assertEqual(
            submitter.call_args.kwargs["video_local_path"],
            "storage/local_videos/visual.mp4",
        )
        self.assertEqual(
            submitter.call_args.kwargs["audio_local_path"],
            "storage/local_audios/audio.mp3",
        )

    def test_process_queued_intent_submitter_error_marks_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_intent_queue_item(Path(tmp))
            submitter = mock.Mock(return_value={"ok": False, "errors": ["boom"]})

            result = process_queued_intent_with_mpt_native(
                path,
                environ={MPT_ENGINE_SUBMIT_FLAG: "1"},
                submitter=submitter,
            )
            payload_after = json.loads(path.read_text(encoding="utf-8"))

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["errors"], ["boom"])
        self.assertEqual(payload_after["status"], "FAILED")
        self.assertEqual(payload_after["error"], "boom")

    def test_process_queued_intent_helper_does_not_call_runner_or_api(self):
        source = inspect.getsource(process_queued_intent_with_mpt_native)
        self.assertNotIn("run_controlled_runner", source)
        self.assertNotIn("nightly_runner", source)
        self.assertNotIn("/api/v1/videos", source)
        self.assertNotIn("openai", source.lower())
        self.assertNotIn("text_to_speech", source.lower())
        self.assertNotIn("pexels", source.lower())
        self.assertNotIn("pixabay", source.lower())
        self.assertNotIn("coverr", source.lower())

    def test_process_queued_intent_batch_limits_max_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = [
                self._write_intent_queue_item(
                    root,
                    self._intent_queue_payload(task_id=f"manual-intent-00{index}"),
                    filename=f"20260712-12000{index}-manual-intent.json",
                )
                for index in range(1, 4)
            ]
            submitter = mock.Mock(
                return_value={
                    "ok": True,
                    "expected_output": "storage/tasks/manual-intent/final-1.mp4",
                }
            )

            result = process_queued_intent_batch_with_mpt_native(
                paths,
                max_items=2,
                environ={MPT_ENGINE_SUBMIT_FLAG: "1"},
                submitter=submitter,
            )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["processed"], 2)
        self.assertEqual(result["done"], 2)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(submitter.call_count, 2)

    def test_process_queued_intent_batch_rejects_wrong_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_intent_queue_item(
                Path(tmp),
                self._intent_queue_payload(source="legacy_queue"),
            )
            submitter = mock.Mock(return_value={"ok": True})

            result = process_queued_intent_batch_with_mpt_native(
                [path],
                environ={MPT_ENGINE_SUBMIT_FLAG: "1"},
                submitter=submitter,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["processed"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertIn("queue item source must be job_intent_v1", result["errors"])
        submitter.assert_not_called()

    def test_process_queued_intent_batch_rejects_missing_compiled_spec(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_intent_queue_item(
                Path(tmp),
                self._intent_queue_payload(compiled_mpt_spec={}),
            )
            submitter = mock.Mock(return_value={"ok": True})

            result = process_queued_intent_batch_with_mpt_native(
                [path],
                environ={MPT_ENGINE_SUBMIT_FLAG: "1"},
                submitter=submitter,
            )
            payload_after = json.loads(path.read_text(encoding="utf-8"))

        self.assertFalse(result["ok"])
        self.assertEqual(result["failed"], 1)
        self.assertIn("compiled_mpt_spec is required", result["errors"])
        self.assertEqual(payload_after["status"], "FAILED")
        submitter.assert_not_called()

    def test_process_queued_intent_batch_rejects_dangerous_guardrails(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._intent_queue_payload()
            payload["guardrails"]["asset_hub_api_allowed"] = True
            path = self._write_intent_queue_item(Path(tmp), payload)
            submitter = mock.Mock(return_value={"ok": True})

            result = process_queued_intent_batch_with_mpt_native(
                [path],
                environ={MPT_ENGINE_SUBMIT_FLAG: "1"},
                submitter=submitter,
            )

        self.assertFalse(result["ok"])
        self.assertIn("guardrails.asset_hub_api_allowed must be false", result["errors"])
        submitter.assert_not_called()

    def test_process_queued_intent_batch_flag_off_does_not_render(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_intent_queue_item(Path(tmp))
            submitter = mock.Mock(return_value={"ok": True})

            result = process_queued_intent_batch_with_mpt_native(
                [path],
                environ={},
                submitter=submitter,
            )
            payload_after = json.loads(path.read_text(encoding="utf-8"))

        self.assertFalse(result["ok"])
        self.assertEqual(result["processed"], 0)
        self.assertIn(MPT_ENGINE_SUBMIT_FLAG, result["errors"][0])
        self.assertEqual(payload_after["status"], "QUEUED")
        submitter.assert_not_called()

    def test_process_queued_intent_batch_submitter_ok_marks_done_per_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = [
                self._write_intent_queue_item(
                    root,
                    self._intent_queue_payload(task_id=f"manual-intent-00{index}"),
                    filename=f"20260712-12000{index}-manual-intent.json",
                )
                for index in range(1, 3)
            ]

            def fake_submitter(*, task_id, **_kwargs):
                return {
                    "ok": True,
                    "expected_output": f"storage/tasks/{task_id}/final-1.mp4",
                    "errors": [],
                }

            result = process_queued_intent_batch_with_mpt_native(
                paths,
                max_items=2,
                environ={MPT_ENGINE_SUBMIT_FLAG: "1"},
                submitter=fake_submitter,
            )
            payloads = [json.loads(path.read_text(encoding="utf-8")) for path in paths]

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["processed"], 2)
        self.assertEqual(result["done"], 2)
        self.assertEqual(result["failed"], 0)
        self.assertEqual([payload["status"] for payload in payloads], ["DONE", "DONE"])
        self.assertTrue(all(item["output_path"] for item in result["items"]))

    def test_process_queued_intent_batch_submitter_error_marks_failed_and_continues(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = [
                self._write_intent_queue_item(
                    root,
                    self._intent_queue_payload(task_id="manual-intent-ok"),
                    filename="20260712-120001-manual-intent.json",
                ),
                self._write_intent_queue_item(
                    root,
                    self._intent_queue_payload(task_id="manual-intent-fail"),
                    filename="20260712-120002-manual-intent.json",
                ),
            ]

            def fake_submitter(*, task_id, **_kwargs):
                if task_id.endswith("fail"):
                    return {"ok": False, "errors": ["boom"]}
                return {
                    "ok": True,
                    "expected_output": f"storage/tasks/{task_id}/final-1.mp4",
                    "errors": [],
                }

            result = process_queued_intent_batch_with_mpt_native(
                paths,
                max_items=2,
                environ={MPT_ENGINE_SUBMIT_FLAG: "1"},
                submitter=fake_submitter,
            )
            payloads = [json.loads(path.read_text(encoding="utf-8")) for path in paths]

        self.assertFalse(result["ok"])
        self.assertEqual(result["processed"], 2)
        self.assertEqual(result["done"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertEqual([payload["status"] for payload in payloads], ["DONE", "FAILED"])
        self.assertEqual(result["items"][1]["error"], "boom")

    def test_process_queued_intent_batch_helper_does_not_call_runner_or_api(self):
        source = inspect.getsource(process_queued_intent_batch_with_mpt_native)
        self.assertNotIn("run_controlled_runner", source)
        self.assertNotIn("nightly_runner", source)
        self.assertNotIn("/api/v1/videos", source)
        self.assertNotIn("openai", source.lower())
        self.assertNotIn("text_to_speech", source.lower())
        self.assertNotIn("pexels", source.lower())
        self.assertNotIn("pixabay", source.lower())
        self.assertNotIn("coverr", source.lower())

    def test_enqueue_aroll_broll_from_console_accepts_multiline_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self._aroll_broll_local_config(root)
            second = root / "storage" / "local_assets" / "second.mp4"
            second.parent.mkdir(parents=True, exist_ok=True)
            second.write_bytes(b"broll")
            config["b_roll"]["assets"] = (
                "\nstorage/local_videos/cutaway.mp4\n\n"
                "storage/local_assets/second.mp4\n"
            )
            result = enqueue_aroll_broll_from_console(
                config,
                job_id="aroll-broll-multiline",
                project_root=root,
                queue_dir=root / "pending",
                environ={"KURUKIN_ENABLE_AROLL_BROLL_QUEUE": "1"},
            )
            payload = json.loads(
                Path(result["pending_path"]).read_text(encoding="utf-8")
            )

        self.assertEqual(payload["b_roll_asset_count"], 2)
        self.assertEqual(len(payload["aroll_broll"]["b_roll"]["assets"]), 2)

    def test_enqueue_aroll_broll_from_console_requires_queue_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue_dir = root / "storage" / "nightly_jobs" / "pending"
            with self.assertRaises(ValueError):
                enqueue_aroll_broll_from_console(
                    self._aroll_broll_local_config(root),
                    job_id="aroll-broll-ui-smoke-004",
                    project_root=root,
                    queue_dir=queue_dir,
                    environ={},
                )

        self.assertFalse(queue_dir.exists())

    def test_enqueue_aroll_broll_from_console_writes_pending_with_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue_dir = root / "storage" / "nightly_jobs" / "pending"
            result = enqueue_aroll_broll_from_console(
                self._aroll_broll_local_config(root),
                job_id="aroll-broll-ui-smoke-004",
                project_root=root,
                queue_dir=queue_dir,
                render_quality="draft_720p",
                task_id="aroll-broll-ui-smoke-004",
                created_by="render_console_ui_smoke_004",
                environ={"KURUKIN_ENABLE_AROLL_BROLL_QUEUE": "1"},
            )
            pending_path = Path(result["pending_path"])
            payload = json.loads(pending_path.read_text(encoding="utf-8"))

        self.assertTrue(pending_path.name.endswith("aroll-broll-ui-smoke-004.json"))
        self.assertEqual(payload["render_mode"], "aroll_broll")
        self.assertEqual(payload["job_id"], "aroll-broll-ui-smoke-004")
        self.assertEqual(payload["task_id"], "aroll-broll-ui-smoke-004")
        self.assertEqual(payload["video_resolution"], "draft_720p")
        self.assertEqual(payload["created_by"], "render_console_ui_smoke_004")

    def test_webui_page_does_not_use_raw_structural_html(self):
        page = Path("webui/pages/Kurukin_Render_Console.py").read_text(
            encoding="utf-8"
        )

        for forbidden in ("<div", "</div>", "class=", "<span", "<strong>"):
            self.assertNotIn(forbidden, page)

    def test_webui_page_includes_first_video_guidance(self):
        page = Path("webui/pages/Kurukin_Render_Console.py").read_text(
            encoding="utf-8"
        )

        required_copy = (
            "Para crear tu primer video",
            "Flujo completo",
            "END_TO_END_FLOW_STEPS",
            "Crear video",
            "Validar",
            "Ejecutar runner controlado",
            "API Docker",
            "Render",
            "MP4",
            "Validar video no crea archivos ni renderiza",
            "Enviar a cola solo crea un trabajo pendiente",
            "Modo recomendado para prueba",
            "Código del paquete de assets (obligatorio)",
            "Título del video (opcional pero recomendado)",
            "Guion o descripción (opcional para este flujo si el bundle ya tiene escenas)",
            "Opciones avanzadas del trabajo",
            "Primero valida. Después envía a cola.",
            "No crea pending job",
            "No renderiza inmediatamente",
            "form_enqueue_disabled",
            "Tipo de video",
            "Video normal con assets",
            "Motor MPT nativo",
            "mpt-native-ui-job",
            "Kurukin MPT native render",
            "storage/tasks/<task_id>/final-1.mp4",
            "Output pendiente/no encontrado todavía",
            "Output existe:",
            "_mpt_native_expected_output_video",
            "Enviar local-only a MPT",
            "mpt_native_local_submit",
            "MPT_ENGINE_SUBMIT_FLAG",
            "Submit real MPT desactivado. Activa",
            "Crear por intención",
            "Validar intención",
            "Preparar spec MPT",
            "Agregar a cola",
            "job_intent_enqueue",
            "Resultado cola por intención",
            "video_path (opcional para audio_to_video)",
            "Si no pasas video_path, Kurukin intentará usar un visual local automático.",
            "Visual local resuelto:",
            "Source visual:",
            "Enviar a MPT nativo",
            "job_intent_submit_mpt_native",
            "Procesar con MPT nativo",
            "process_queued_intent_with_mpt_native",
            "Procesar lote con MPT nativo",
            "process_queued_intent_batch_with_mpt_native",
            "max_items proceso lote",
            "STATUS_READY_TO_SUBMIT",
            "compile_job_intent_to_mpt_spec",
            "validate_job_intent",
            "Crear lote desde audios",
            "Agregar lote a cola",
            "batch_audio_enqueue",
            "enqueue_audio_batch_intents",
            "Resultados de intenciones",
            "status intenciones",
            "limit intenciones",
            "list_intent_results",
            "output_exists",
            "Reintentar con MPT nativo",
        )

        for expected in required_copy:
            self.assertIn(expected, page)

    def test_webui_page_includes_aroll_broll_foundation_copy(self):
        page = Path("webui/pages/Kurukin_Render_Console.py").read_text(
            encoding="utf-8"
        )

        required_copy = (
            "Presentador + B-roll",
            "Modo Presentador + B-roll",
            "A-roll / B-roll",
            "El audio del presentador manda",
            "B-roll muted",
            "Ruta del video A-roll local",
            "Fuente B-roll",
            "Bundle UID",
            "Layout preset",
            "Crop del presentador",
            "Frecuencia B-roll",
            "Duración promedio por clip",
            "aroll_audio (futuro)",
            "Calidad",
            "alternating_fullscreen",
            "Renderer MVP planeado: alternating_fullscreen",
            "Audio final: A-roll original",
            "B-roll audio: muted",
            "La cola puede preparar un pending job protegido",
            "Cola A-roll/B-roll: protegida",
            "Activa KURUKIN_ENABLE_AROLL_BROLL_QUEUE=1 solo para pruebas controladas.",
            "Renderer preparado: alternating_fullscreen",
            'summary["asset_policy"]',
            "asset_policy_short_label",
            "asset_materialization_source_label",
            "Fuente:",
            "Query:",
            "Uno o varios paths B-roll locales",
            "B-roll assets:",
            "El runner no lo renderiza todavía",
            "aroll_broll_enqueue",
            "aroll_broll_enqueue_disabled",
            "Pexels source: disponible solo con integración controlada/flag; no se usa por defecto.",
            "KURUKIN_ENABLE_PEXELS_SOURCE",
            "create_pexels_downloader",
        )

        for expected in required_copy:
            self.assertIn(expected, page)

    def test_webui_page_includes_job_lifecycle_dashboard_copy(self):
        page = Path("webui/pages/Kurukin_Render_Console.py").read_text(
            encoding="utf-8"
        )

        required_copy = (
            "Cola y resultados",
            "Revisa trabajos pendientes, resultados generados y errores sin entrar por terminal.",
            "Esta pantalla solo consulta estado. No ejecuta renders ni modifica archivos.",
            "Actualizar estado",
            "Pendientes",
            "En proceso",
            "Completados",
            "Fallidos",
            "Videos detectados",
            "No hay trabajos pendientes. Cuando envíes un video a cola, aparecerá aquí.",
            "Todavía no hay renders completados.",
            "Cuando envíes un video a cola y el runner lo procese, aparecerá aquí.",
            "Después de Enviar a cola, ejecuta el runner controlado",
            "haga el render y deje el MP4 en Resultados.",
        )

        for expected in required_copy:
            self.assertIn(expected, page)

    def test_webui_page_includes_safe_enqueue_feedback_copy(self):
        page = Path("webui/pages/Kurukin_Render_Console.py").read_text(
            encoding="utf-8"
        )

        required_copy = (
            "Video enviado a cola.",
            "El render no empezó todavía. Puedes revisar el estado en la ",
            "pestaña Cola.",
            "Abre la pestaña Cola y presiona Actualizar estado.",
            "Enviar a cola no renderiza inmediatamente.",
            "last_enqueued_job_id",
            "last_enqueued_pending_path",
            "last_enqueued_at",
            "Último video enviado a cola",
        )

        for expected in required_copy:
            self.assertIn(expected, page)

    def test_webui_page_includes_runner_preflight_copy(self):
        page = Path("webui/pages/Kurukin_Render_Console.py").read_text(
            encoding="utf-8"
        )

        required_copy = (
            "Preflight",
            "Preflight de render",
            "Revisa si el worker está listo antes de ejecutar renders.",
            "Esta pantalla no ejecuta el runner. Solo revisa condiciones de seguridad.",
            "Tasks detectados",
            "Storage usado",
            "Runner detectado",
            "Checklist de seguridad",
            "Runner detectado",
            "Diagnóstico del preflight",
        )
        forbidden_buttons = (
            'st.button("Ejecutar runner"',
            'st.button("Renderizar ahora"',
            'st.button("Procesar trabajos"',
        )

        for expected in required_copy:
            self.assertIn(expected, page)
        for forbidden in forbidden_buttons:
            self.assertNotIn(forbidden, page)

    def test_webui_page_includes_controlled_runner_copy(self):
        page = Path("webui/pages/Kurukin_Render_Console.py").read_text(
            encoding="utf-8"
        )

        required_copy = (
            "Ejecutar",
            "Ejecución controlada",
            "Procesa trabajos pendientes solo cuando estés seguro.",
            "Procesar 1 trabajo ahora",
            "salta la ventana nocturna solo para una ejecución manual controlada",
            "CONTAINER_NIGHTLY_QUEUE_DIR",
            "CONTAINER_API_BASE_URL",
            "API Docker",
            "Máximo de trabajos",
            "Esta acción sí ejecutará el runner",
            "Ejecución desde UI deshabilitada por seguridad.",
            "KURUKIN_ENABLE_UI_RUNNER=1",
            "Comando seguro calculado",
            "Zona peligrosa: ejecutar runner",
            "Entiendo que esto ejecutará render real.",
            "Ejecutar runner controlado",
            "Diagnóstico de ejecución controlada",
        )
        forbidden_copy = ("cleanup", "retry", "delete", "cancel")

        for expected in required_copy:
            self.assertIn(expected, page)
        for forbidden in forbidden_copy:
            self.assertNotIn(forbidden, page.lower())
        self.assertNotIn('key="api_base_url"', page)
        self.assertNotIn('key="runner_api_base_url"', page)

    def test_webui_page_includes_results_tab_copy(self):
        page = Path("webui/pages/Kurukin_Render_Console.py").read_text(
            encoding="utf-8"
        )

        required_copy = (
            "Resultados",
            "Resultados generados",
            "Videos encontrados",
            "Jobs completados",
            "Jobs fallidos",
            "Último video generado",
            "Tu video más reciente",
            "Todos los videos generados",
            "Videos detectados",
            "Video para preview y descarga",
            "Preview",
            "Descargar MP4",
            "Detalles del resultado",
            "Todavía no hay videos generados. Crea un video, envíalo a cola ",
            "read_video_bytes_for_download",
            "VIDEO_PREVIEW_MAX_BYTES",
            "VIDEO_DOWNLOAD_MEMORY_MAX_BYTES",
            "Resultados de intenciones",
            "source: job_intent_v1",
            "output_exists",
            "queue_item_path",
        )

        for expected in required_copy:
            self.assertIn(expected, page)
        self.assertNotIn("st.text_input(\"Ruta", page)

    def test_nightly_runner_parser_keeps_window_by_default(self):
        runner = load_nightly_runner_module()

        args = runner.build_parser().parse_args([])

        self.assertFalse(args.ignore_window)
        self.assertEqual(args.max_jobs, 10)
        self.assertEqual(args.queue_dir, runner.default_queue_dir().as_posix())
        self.assertTrue(args.queue_dir.endswith("/storage/nightly_jobs"))
        self.assertFalse(
            runner.is_in_window(
                runner.dt.datetime(2026, 1, 1, 12, 0),
                runner.dt.time(0, 0),
                runner.dt.time(7, 0),
            )
        )

    def test_nightly_runner_parser_accepts_manual_window_override(self):
        runner = load_nightly_runner_module()

        args = runner.build_parser().parse_args(
            [
                "--ignore-window",
                "--max-jobs",
                "1",
                "--queue-dir",
                CONTAINER_NIGHTLY_QUEUE_DIR,
            ]
        )

        self.assertTrue(args.ignore_window)
        self.assertEqual(args.max_jobs, 1)
        self.assertEqual(args.queue_dir, CONTAINER_NIGHTLY_QUEUE_DIR)

    def test_nightly_runner_rejects_aroll_broll_before_api_payload(self):
        runner = load_nightly_runner_module()

        with self.assertRaises(runner.RunnerError) as raised:
            runner.validate_job(
                {
                    "job_id": "aroll-broll-001",
                    "video_subject": "Presenter edit",
                    "video_aspect": "9:16",
                    "render_mode": "aroll_broll",
                    "aroll_broll": {},
                    "runner": {"job_id": "aroll-broll-001"},
                }
            )

        self.assertIn(
            "A-roll/B-roll renderer execution is disabled",
            str(raised.exception),
        )

    def test_nightly_runner_default_queue_dir_uses_container_when_present(self):
        runner = load_nightly_runner_module()

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            container_queue = base / "MoneyPrinterTurbo" / "storage" / "nightly_jobs"
            container_queue.mkdir(parents=True)

            self.assertEqual(
                runner.default_queue_dir(
                    project_root=base / "repo",
                    container_queue=container_queue,
                ),
                container_queue,
            )

    def test_nightly_runner_default_queue_dir_falls_back_to_project_root(self):
        runner = load_nightly_runner_module()

        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "repo"
            missing_container_queue = Path(tmp) / "missing" / "storage" / "nightly_jobs"

            self.assertEqual(
                runner.default_queue_dir(
                    project_root=project_root,
                    container_queue=missing_container_queue,
                ),
                project_root / "storage" / "nightly_jobs",
            )

    def test_webui_page_uses_human_audio_summary_labels(self):
        page = Path("webui/pages/Kurukin_Render_Console.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("Sin audio propio", page)
        self.assertIn("Audio propio", page)
        self.assertNotIn('metric("Audio", operator.get("audio")', page)

    def test_asset_source_constants_are_importable_aliases(self):
        self.assertEqual(ASSET_SOURCE_ASSET_HUB, SOURCE_MODE_ASSET_HUB)
        self.assertEqual(ASSET_SOURCE_LOCAL, SOURCE_MODE_LOCAL)
        self.assertEqual(ASSET_SOURCE_STOCK, SOURCE_MODE_STOCK)

    def test_get_manifest_summary_for_ui_with_empty_path(self):
        self.assertEqual(
            get_manifest_summary_for_ui(""),
            {
                "exists": False,
                "status": "missing_path",
                "message": "No manifest path provided",
            },
        )

    def test_get_manifest_summary_for_ui_with_missing_path(self):
        summary = get_manifest_summary_for_ui("/data/job-assets/missing/manifest.json")

        self.assertEqual(summary["exists"], False)
        self.assertEqual(summary["status"], "not_found")
        self.assertEqual(summary["message"], "Manifest file not found")

    def test_get_manifest_summary_for_ui_with_valid_manifest(self):
        original_base = os.environ.get("ASSET_HUB_JOB_ASSETS_DIR")
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp) / "job-assets"
            bundle_dir = base_dir / "jab_test"
            image_path = bundle_dir / "scene-00" / "still-a.png"
            video_path = bundle_dir / "scene-01" / "clip-a.mp4"
            for asset_path in (image_path, video_path):
                asset_path.parent.mkdir(parents=True, exist_ok=True)
                asset_path.write_text("dummy", encoding="utf-8")
            manifest_path = bundle_dir / "manifests" / "renderer-manifest.json"
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(
                json.dumps(
                    {
                        "manifest_version": "1.0",
                        "generated_by": "kurukin-asset-hub",
                        "bundle_uid": "jab_test",
                        "job_id": "asset-job-001",
                        "scenes": [
                            {
                                "scene_index": 0,
                                "needs_human_review": True,
                                "assets": [
                                    {
                                        "type": "image",
                                        "filename": "still-a.png",
                                        "local_path": str(image_path),
                                        "duration_seconds": 3,
                                        "safe_for_subtitles": False,
                                    },
                                    {
                                        "type": "video",
                                        "filename": "clip-a.mp4",
                                        "local_path": str(video_path),
                                        "duration_seconds": 4,
                                        "safe_for_text_overlay": False,
                                    },
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            os.environ["ASSET_HUB_JOB_ASSETS_DIR"] = str(base_dir)

            summary = get_manifest_summary_for_ui(str(manifest_path))

        if original_base is None:
            os.environ.pop("ASSET_HUB_JOB_ASSETS_DIR", None)
        else:
            os.environ["ASSET_HUB_JOB_ASSETS_DIR"] = original_base

        self.assertEqual(summary["exists"], True)
        self.assertEqual(summary["status"], "ready")
        self.assertEqual(summary["bundle_uid"], "jab_test")
        self.assertEqual(summary["job_id"], "asset-job-001")
        self.assertEqual(summary["total_scenes"], 1)
        self.assertEqual(summary["total_assets"], 2)
        self.assertEqual(summary["asset_types"], {"image": 1, "video": 1})
        self.assertEqual(summary["duration_total_seconds"], 7.0)
        self.assertEqual(summary["preview_filenames"], ["still-a.png", "clip-a.mp4"])
        self.assertEqual(summary["needs_human_review_count"], 1)
        self.assertEqual(summary["safe_for_subtitles_false_count"], 1)
        self.assertEqual(summary["safe_for_text_overlay_false_count"], 1)

    def test_get_manifest_summary_for_ui_with_invalid_manifest(self):
        original_base = os.environ.get("ASSET_HUB_JOB_ASSETS_DIR")
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp) / "job-assets"
            manifest_path = base_dir / "jab_test" / "manifests" / "renderer-manifest.json"
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(
                json.dumps({"manifest_version": "2.0"}),
                encoding="utf-8",
            )
            os.environ["ASSET_HUB_JOB_ASSETS_DIR"] = str(base_dir)

            summary = get_manifest_summary_for_ui(str(manifest_path))

        if original_base is None:
            os.environ.pop("ASSET_HUB_JOB_ASSETS_DIR", None)
        else:
            os.environ["ASSET_HUB_JOB_ASSETS_DIR"] = original_base

        self.assertEqual(summary["exists"], True)
        self.assertEqual(summary["status"], "invalid")
        self.assertIn("manifest_version", summary["message"])

    def test_default_asset_hub_manifest_path_with_bundle_uid(self):
        self.assertEqual(
            default_asset_hub_manifest_path(BUNDLE_UID),
            f"/data/job-assets/{BUNDLE_UID}/manifests/renderer-manifest.json",
        )

    def test_default_asset_hub_manifest_path_rejects_path_traversal(self):
        with self.assertRaises(ValueError):
            default_asset_hub_manifest_path("../bundle")

    def test_build_render_console_spec_with_bundle_uid_creates_asset_hub(self):
        spec = make_spec()

        self.assertEqual(spec["asset_hub"]["bundle_uid"], BUNDLE_UID)
        self.assertTrue(
            spec["asset_hub"]["renderer_manifest_path"].endswith(
                "/manifests/renderer-manifest.json"
            )
        )

    def test_build_render_console_spec_with_local_assets_creates_selected_assets(self):
        spec = make_spec(
            asset_source_mode=ASSET_SOURCE_LOCAL,
            selected_local_assets=["clip-01.mp4", "still-02.png"],
        )

        self.assertNotIn("asset_hub", spec)
        self.assertEqual(
            spec["selectedAssets"],
            [
                {"file": "clip-01.mp4", "order": 1},
                {"file": "still-02.png", "order": 2},
            ],
        )

    def test_build_render_console_spec_local_assets_rejects_parent_path(self):
        with self.assertRaises(KurukinJobAdapterError):
            make_spec(
                asset_source_mode=ASSET_SOURCE_LOCAL,
                selected_local_assets=["../clip-01.mp4"],
            )

    def test_build_render_console_spec_stock_mode_is_not_available_yet(self):
        with self.assertRaises(ValueError) as raised:
            make_spec(asset_source_mode=ASSET_SOURCE_STOCK, stock_source="pexels")

        self.assertIn("Stock externo", str(raised.exception))

    def test_build_render_console_spec_with_audio_file_creates_audio(self):
        spec = make_spec(audio_file="audio-prueba.mp3")

        self.assertEqual(spec["audio"], {"file": "audio-prueba.mp3"})

    def test_build_render_console_spec_mode_none_creates_subtitles_none(self):
        spec = make_spec(subtitles_mode="none")

        self.assertEqual(spec["subtitles"], {"mode": "none"})
        self.assertFalse(spec["video"]["subtitle_enabled"])

    def test_build_render_console_spec_mode_custom_srt_includes_file(self):
        spec = make_spec(
            subtitles_mode="custom_srt",
            custom_subtitle_file="captions.srt",
        )

        self.assertEqual(spec["subtitles"]["mode"], "custom_srt")
        self.assertEqual(spec["subtitles"]["file"], "captions.srt")

    def test_build_render_console_spec_image_motion_enabled_creates_image_motion(self):
        spec = make_spec(
            image_motion_enabled=True,
            image_motion_preset="slow_zoom_in",
            image_motion_intensity=0.06,
        )

        self.assertEqual(
            spec["image_motion"],
            {"enabled": True, "preset": "slow_zoom_in", "intensity": 0.06},
        )

    def test_validate_and_build_payload_produces_asset_hub_fields(self):
        payload, _ = validate_and_build_payload_from_console_spec(make_spec())

        self.assertEqual(payload["asset_hub_bundle_uid"], BUNDLE_UID)
        self.assertTrue(
            payload["asset_hub_renderer_manifest_path"].endswith(
                "/renderer-manifest.json"
            )
        )
        self.assertEqual(payload["video_resolution"], "draft_720p")

    def test_validate_and_build_payload_produces_summary(self):
        _, summary = validate_and_build_payload_from_console_spec(make_spec())

        self.assertEqual(summary["job_id"], "render-console-test-001")
        self.assertEqual(summary["asset_hub_bundle_uid"], BUNDLE_UID)

    def test_build_operator_summary_for_asset_hub_manifest_notes_deferred_assets(self):
        payload, _ = validate_and_build_payload_from_console_spec(make_spec())
        operator = build_operator_summary(
            payload,
            {"status": "ready", "total_assets": 3, "bundle_uid": BUNDLE_UID},
        )

        self.assertEqual(operator["mode"], "Asset Hub manifest")
        self.assertEqual(operator["payload_material_count"], 0)
        self.assertEqual(operator["manifest_asset_count"], 3)
        self.assertEqual(
            operator["note"],
            "Los assets se resolverán desde el manifest cuando el worker inicie el render.",
        )

    def test_build_operator_summary_for_local_materials(self):
        payload = {
            "job_id": "local-job-001",
            "video_subject": "Local assets",
            "video_source": "local",
            "video_resolution": "draft_720p",
            "video_aspect": "16:9",
            "subtitle_enabled": False,
            "image_motion_enabled": False,
            "video_materials": [
                {"provider": "local", "url": "clip-01.mp4"},
                {"provider": "local", "url": "clip-02.mp4"},
            ],
            "runner": {},
        }

        operator = build_operator_summary(payload)

        self.assertEqual(operator["mode"], "Local selected assets")
        self.assertEqual(operator["payload_material_count"], 2)
        self.assertEqual(operator["manifest_asset_count"], 0)
        self.assertEqual(operator["note"], "")

    def test_no_selected_assets_in_basic_form_spec(self):
        self.assertNotIn("selectedAssets", make_spec())

    def test_safe_relative_path_rejects_absolute_path(self):
        with self.assertRaises(KurukinJobAdapterError):
            safe_relative_path(
                "/tmp/clip.mp4",
                allowed_extensions={"mp4"},
                label="asset",
            )

    def test_list_local_storage_files_returns_allowed_safe_filenames(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "clip-a.mp4").write_text("dummy", encoding="utf-8")
            (base / "notes.txt").write_text("dummy", encoding="utf-8")
            (base / "clip-b.png").write_text("dummy", encoding="utf-8")

            self.assertEqual(
                list_local_storage_files(base, allowed_extensions={"mp4", "png"}),
                ["clip-a.mp4", "clip-b.png"],
            )

    def test_list_pending_jobs_returns_empty_when_directory_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            pending_dir = Path(tmp) / "missing"

            self.assertEqual(list_pending_jobs(pending_dir), [])
            self.assertFalse(pending_dir.exists())

    def test_list_pending_jobs_reads_valid_pending_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            pending_dir = Path(tmp) / "pending"
            pending_dir.mkdir()
            pending_path = pending_dir / "20260707-120000-render-job-001.json"
            pending_path.write_text(
                json.dumps(
                    {
                        "job_id": "render-job-001",
                        "video_subject": "Demo claro",
                        "asset_hub_bundle_uid": BUNDLE_UID,
                        "video_resolution": "draft_720p",
                        "subtitle_enabled": False,
                    }
                ),
                encoding="utf-8",
            )

            jobs = list_pending_jobs(pending_dir)

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["job_id"], "render-job-001")
        self.assertEqual(jobs[0]["title"], "Demo claro")
        self.assertEqual(jobs[0]["asset_source"], "Asset Hub Bundle")
        self.assertEqual(jobs[0]["quality"], "draft_720p")
        self.assertEqual(jobs[0]["subtitles"], "Sin subtítulos")
        self.assertTrue(jobs[0]["valid_json"])
        self.assertTrue(jobs[0]["created_at_iso"].startswith("2026-07-07T12:00:00"))

    def test_list_pending_jobs_invalid_json_does_not_break(self):
        with tempfile.TemporaryDirectory() as tmp:
            pending_dir = Path(tmp) / "pending"
            pending_dir.mkdir()
            (pending_dir / "20260707-120000-bad.json").write_text(
                "{not-json",
                encoding="utf-8",
            )

            jobs = list_pending_jobs(pending_dir)

        self.assertEqual(len(jobs), 1)
        self.assertFalse(jobs[0]["valid_json"])
        self.assertIn("error", jobs[0])

    def test_list_task_summaries_returns_empty_when_directory_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            tasks_dir = Path(tmp) / "tasks"

            self.assertEqual(list_task_summaries(tasks_dir), [])
            self.assertFalse(tasks_dir.exists())

    def test_detect_task_outputs_finds_mp4_inside_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp) / "task-001"
            output_dir = task_dir / "final"
            output_dir.mkdir(parents=True)
            (output_dir / "final-1.mp4").write_bytes(b"mp4")

            outputs = detect_task_outputs(task_dir)

        self.assertEqual(len(outputs), 1)
        self.assertEqual(outputs[0]["name"], "final-1.mp4")
        self.assertEqual(outputs[0]["relative_path"], "final/final-1.mp4")
        self.assertEqual(outputs[0]["task_id"], "task-001")

    def test_infer_task_status_completed_when_mp4_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp) / "task-001"
            task_dir.mkdir()
            (task_dir / "final-1.mp4").write_bytes(b"mp4")

            status = infer_task_status(task_dir)

        self.assertEqual(status, "completed")

    def test_infer_task_status_failed_when_error_log_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp) / "task-001"
            task_dir.mkdir()
            (task_dir / "render.log").write_text(
                "Traceback: render failed",
                encoding="utf-8",
            )

            status = infer_task_status(task_dir)

        self.assertEqual(status, "failed")

    def test_list_task_summaries_uses_tempfile_without_storage_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            tasks_dir = Path(tmp) / "tasks"
            task_dir = tasks_dir / "task-001"
            task_dir.mkdir(parents=True)
            (task_dir / "final-1.mp4").write_bytes(b"mp4")

            summaries = list_task_summaries(tasks_dir)

        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]["task_id"], "task-001")
        self.assertEqual(summaries[0]["status"], "completed")
        self.assertEqual(summaries[0]["output_count"], 1)

    def test_safe_enqueue_pending_lifecycle_uses_temp_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            pending_dir = base / "pending"
            tasks_dir = base / "tasks"
            payload = {
                "job_id": "render-console-safe-enqueue-smoke-001",
                "video_subject": "Safe enqueue smoke",
                "asset_hub_bundle_uid": BUNDLE_UID,
                "video_resolution": "draft_720p",
                "subtitle_enabled": False,
                "runner": {"job_id": "render-console-safe-enqueue-smoke-001"},
            }

            pending_path = enqueue_moneyprinter_payload(
                payload,
                queue_dir=pending_dir,
                now=datetime(2026, 7, 7, 12, 0, 0, tzinfo=timezone.utc),
            )
            jobs = list_pending_jobs(pending_dir)
            lifecycle = get_job_lifecycle_summary(
                pending_dir=pending_dir,
                tasks_dir=tasks_dir,
            )

            self.assertTrue(pending_path.is_file())
            self.assertTrue(pending_path.is_relative_to(base))
            self.assertEqual(len(jobs), 1)
            self.assertEqual(
                jobs[0]["job_id"],
                "render-console-safe-enqueue-smoke-001",
            )
            self.assertEqual(lifecycle["counts"]["pending"], 1)
            self.assertEqual(lifecycle["counts"]["videos"], 0)
            self.assertEqual(lifecycle["tasks"], [])
            self.assertEqual(lifecycle["outputs"], [])

            pending_path.unlink()
            lifecycle_after_cleanup = get_job_lifecycle_summary(
                pending_dir=pending_dir,
                tasks_dir=tasks_dir,
            )

            self.assertEqual(lifecycle_after_cleanup["counts"]["pending"], 0)
            self.assertEqual(lifecycle_after_cleanup["tasks"], [])
            self.assertEqual(lifecycle_after_cleanup["outputs"], [])
            self.assertFalse(tasks_dir.exists())

    def test_storage_usage_summary_missing_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing_dir = Path(tmp) / "missing-storage"

            summary = get_storage_usage_summary(missing_dir)

        self.assertFalse(summary["exists"])
        self.assertEqual(summary["total_size_bytes"], 0)
        self.assertEqual(summary["file_count"], 0)
        self.assertIn("not found", summary["warning"])

    def test_storage_usage_summary_counts_small_temp_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage_dir = Path(tmp) / "storage"
            (storage_dir / "nested").mkdir(parents=True)
            (storage_dir / "a.txt").write_bytes(b"abc")
            (storage_dir / "nested" / "b.txt").write_bytes(b"de")

            summary = get_storage_usage_summary(storage_dir)

        self.assertTrue(summary["exists"])
        self.assertEqual(summary["total_size_bytes"], 5)
        self.assertEqual(summary["file_count"], 2)
        self.assertGreaterEqual(summary["dir_count"], 1)
        self.assertFalse(summary["scan_truncated"])

    def test_detect_runner_candidates_uses_existing_files_without_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scripts_dir = root / "scripts"
            scripts_dir.mkdir()
            runner = scripts_dir / "nightly_runner.py"
            runner.write_text("raise RuntimeError('must not execute')\n", encoding="utf-8")

            candidates = detect_runner_candidates(root)

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["name"], "Nightly runner")
        self.assertEqual(candidates[0]["suggested_command"], "python3 scripts/nightly_runner.py")
        self.assertEqual(candidates[0]["confidence"], "high")

    def test_preflight_checks_mark_no_pending_as_review(self):
        lifecycle = {
            "counts": {"pending": 0, "videos": 0},
            "tasks": [],
            "outputs": [],
        }
        storage = {"exists": True, "path": "storage", "scan_truncated": False}
        with tempfile.TemporaryDirectory() as tmp:
            pending_dir = Path(tmp) / "pending"
            checks = build_preflight_checks(
                lifecycle,
                [{"name": "Nightly runner"}],
                storage,
                pending_dir=pending_dir,
            )

        pending_check = next(
            item for item in checks if item["name"] == "Hay al menos un trabajo pendiente"
        )
        self.assertEqual(pending_check["status"], "Revisar")

    def test_preflight_summary_with_pending_marks_pending_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            pending_dir = base / "pending"
            tasks_dir = base / "tasks"
            storage_dir = base / "storage"
            project_root = base / "repo"
            (project_root / "scripts").mkdir(parents=True)
            (project_root / "scripts" / "nightly_runner.py").write_text(
                "# runner marker\n",
                encoding="utf-8",
            )
            storage_dir.mkdir()
            enqueue_moneyprinter_payload(
                {
                    "job_id": "preflight-pending-001",
                    "video_subject": "Preflight",
                    "video_resolution": "draft_720p",
                    "runner": {"job_id": "preflight-pending-001"},
                },
                queue_dir=pending_dir,
                now=datetime(2026, 7, 7, 12, 0, 0, tzinfo=timezone.utc),
            )

            summary = get_runner_preflight_summary(
                project_root=project_root,
                storage_dir=storage_dir,
                pending_dir=pending_dir,
                tasks_dir=tasks_dir,
            )

        pending_check = next(
            item
            for item in summary["checks"]
            if item["name"] == "Hay al menos un trabajo pendiente"
        )
        self.assertEqual(summary["counts"]["pending"], 1)
        self.assertEqual(summary["counts"]["tasks"], 0)
        self.assertEqual(summary["counts"]["videos"], 0)
        self.assertEqual(summary["counts"]["runner_candidates"], 1)
        self.assertEqual(pending_check["status"], "Listo")
        self.assertEqual(summary["runner_candidates"][0]["relative_path"], "scripts/nightly_runner.py")
        self.assertFalse(tasks_dir.exists())

    def test_is_ui_runner_enabled_defaults_to_false(self):
        self.assertFalse(is_ui_runner_enabled({}))

    def test_is_ui_runner_enabled_accepts_enabled_values(self):
        self.assertTrue(is_ui_runner_enabled({"KURUKIN_ENABLE_UI_RUNNER": "1"}))
        self.assertTrue(is_ui_runner_enabled({"KURUKIN_ENABLE_UI_RUNNER": "true"}))
        self.assertTrue(is_ui_runner_enabled({"KURUKIN_ENABLE_UI_RUNNER": "YES"}))

    def test_aroll_broll_feature_flags_default_to_false(self):
        self.assertFalse(is_aroll_broll_queue_enabled({}))
        self.assertFalse(is_aroll_broll_renderer_enabled({}))

    def test_aroll_broll_queue_flag_accepts_controlled_values(self):
        self.assertTrue(
            is_aroll_broll_queue_enabled({"KURUKIN_ENABLE_AROLL_BROLL_QUEUE": "1"})
        )
        self.assertTrue(
            is_aroll_broll_queue_enabled({"KURUKIN_ENABLE_AROLL_BROLL_QUEUE": "yes"})
        )

    def test_build_safe_runner_command_unavailable_without_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            command = build_safe_runner_command(Path(tmp))

        self.assertFalse(command["available"])
        self.assertEqual(command["command"], [])
        self.assertEqual(command["confidence"], "none")

    def test_build_safe_runner_command_reports_missing_container_mount(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "MoneyPrinterTurbo"
            root.mkdir()

            command = build_safe_runner_command(root)

        self.assertFalse(command["available"])
        self.assertIn("runner no está montado", command["reason"])

    def test_build_safe_runner_command_returns_command_list_for_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "scripts").mkdir()
            (root / "scripts" / "nightly_runner.py").write_text(
                "raise RuntimeError('must not execute')\n",
                encoding="utf-8",
            )

            command = build_safe_runner_command(root)

        self.assertTrue(command["available"])
        self.assertEqual(command["runner_name"], "Nightly runner")
        self.assertEqual(command["command"], ["python3", "scripts/nightly_runner.py"])
        self.assertEqual(command["confidence"], "high")
        self.assertIsInstance(command["command"], list)
        self.assertEqual(command["execution_mode"], "nightly_default")

    def test_build_safe_runner_command_returns_manual_override_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "scripts").mkdir()
            (root / "scripts" / "nightly_runner.py").write_text(
                "raise RuntimeError('must not execute')\n",
                encoding="utf-8",
            )

            command = build_safe_runner_command(
                root,
                manual_override=True,
                max_jobs=1,
            )

        self.assertTrue(command["available"])
        self.assertIsInstance(command["command"], list)
        self.assertEqual(
            command["command"],
            EXPECTED_MANUAL_RUNNER_COMMAND,
        )
        self.assertEqual(command["execution_mode"], MANUAL_RUNNER_EXECUTION_MODE)
        self.assertEqual(command["max_jobs"], MANUAL_RUNNER_MAX_JOBS)
        self.assertEqual(command["queue_dir"], CONTAINER_NIGHTLY_QUEUE_DIR)
        self.assertEqual(command["api_base_url"], CONTAINER_API_BASE_URL)
        self.assertIn("--api-base-url", command["command"])
        api_url = command["command"][command["command"].index("--api-base-url") + 1]
        self.assertEqual(api_url, CONTAINER_API_BASE_URL)
        self.assertNotIn("127.0.0.1:18080", api_url)
        self.assertNotIn((root / "storage/nightly_jobs").as_posix(), command["command"])

    def _runner_request_fixture(self):
        preflight = {
            "counts": {"pending": 1},
            "checks": [
                {"name": "El runner está disponible", "status": "Listo"},
                {"name": "El directorio de storage existe", "status": "Listo"},
            ],
        }
        command = {
            "available": True,
            "runner_name": "Nightly runner",
            "command": list(EXPECTED_MANUAL_RUNNER_COMMAND),
            "cwd": "/tmp/project",
            "reason": "ready",
            "confidence": "high",
            "execution_mode": MANUAL_RUNNER_EXECUTION_MODE,
            "max_jobs": MANUAL_RUNNER_MAX_JOBS,
            "queue_dir": CONTAINER_NIGHTLY_QUEUE_DIR,
            "api_base_url": CONTAINER_API_BASE_URL,
        }
        return preflight, command

    def test_validate_runner_execution_request_fails_when_flag_off(self):
        preflight, command = self._runner_request_fixture()

        result = validate_runner_execution_request(
            feature_enabled=False,
            preflight_summary=preflight,
            command_info=command,
            understood=True,
            confirm_text=RUNNER_CONFIRM_TEXT,
            queue_confirmation=RUNNER_QUEUE_CONFIRM_TEXT,
            execution_mode=MANUAL_RUNNER_EXECUTION_MODE,
            max_jobs=MANUAL_RUNNER_MAX_JOBS,
        )

        self.assertFalse(result["allowed"])
        self.assertIn("Ejecución desde UI deshabilitada", result["errors"][0])

    def test_validate_runner_execution_request_fails_without_pending(self):
        preflight, command = self._runner_request_fixture()
        preflight["counts"]["pending"] = 0

        result = validate_runner_execution_request(
            feature_enabled=True,
            preflight_summary=preflight,
            command_info=command,
            understood=True,
            confirm_text=RUNNER_CONFIRM_TEXT,
            queue_confirmation=RUNNER_QUEUE_CONFIRM_TEXT,
            execution_mode=MANUAL_RUNNER_EXECUTION_MODE,
            max_jobs=MANUAL_RUNNER_MAX_JOBS,
        )

        self.assertFalse(result["allowed"])
        self.assertIn("No hay trabajos pendientes.", result["errors"])

    def test_validate_runner_execution_request_fails_with_wrong_confirm_text(self):
        preflight, command = self._runner_request_fixture()

        result = validate_runner_execution_request(
            feature_enabled=True,
            preflight_summary=preflight,
            command_info=command,
            understood=True,
            confirm_text="EJECUTAR",
            queue_confirmation=RUNNER_QUEUE_CONFIRM_TEXT,
            execution_mode=MANUAL_RUNNER_EXECUTION_MODE,
            max_jobs=MANUAL_RUNNER_MAX_JOBS,
        )

        self.assertFalse(result["allowed"])
        self.assertIn("Texto de confirmación incorrecto.", result["errors"])

    def test_validate_runner_execution_request_fails_when_max_jobs_not_one(self):
        preflight, command = self._runner_request_fixture()
        command["max_jobs"] = 2

        result = validate_runner_execution_request(
            feature_enabled=True,
            preflight_summary=preflight,
            command_info=command,
            understood=True,
            confirm_text=RUNNER_CONFIRM_TEXT,
            queue_confirmation=RUNNER_QUEUE_CONFIRM_TEXT,
            execution_mode=MANUAL_RUNNER_EXECUTION_MODE,
            max_jobs=2,
        )

        self.assertFalse(result["allowed"])
        self.assertIn("La ejecución manual solo permite max_jobs=1.", result["errors"])

    def test_validate_runner_execution_request_fails_with_wrong_queue_dir(self):
        preflight, command = self._runner_request_fixture()
        command["command"] = [
            "python3",
            "scripts/nightly_runner.py",
            "--max-jobs",
            "1",
            "--ignore-window",
            "--queue-dir",
            "/tmp/not-the-ui-queue",
            "--api-base-url",
            CONTAINER_API_BASE_URL,
        ]
        command["queue_dir"] = "/tmp/not-the-ui-queue"

        result = validate_runner_execution_request(
            feature_enabled=True,
            preflight_summary=preflight,
            command_info=command,
            understood=True,
            confirm_text=RUNNER_CONFIRM_TEXT,
            queue_confirmation=RUNNER_QUEUE_CONFIRM_TEXT,
            execution_mode=MANUAL_RUNNER_EXECUTION_MODE,
            max_jobs=MANUAL_RUNNER_MAX_JOBS,
        )

        self.assertFalse(result["allowed"])
        self.assertIn("Queue dir manual seguro incorrecto.", result["errors"])

    def test_validate_runner_execution_request_passes_with_all_gates(self):
        preflight, command = self._runner_request_fixture()

        result = validate_runner_execution_request(
            feature_enabled=True,
            preflight_summary=preflight,
            command_info=command,
            understood=True,
            confirm_text=RUNNER_CONFIRM_TEXT,
            queue_confirmation=RUNNER_QUEUE_CONFIRM_TEXT,
            execution_mode=MANUAL_RUNNER_EXECUTION_MODE,
            max_jobs=MANUAL_RUNNER_MAX_JOBS,
        )

        self.assertTrue(result["allowed"])
        self.assertEqual(result["errors"], [])

    def test_run_controlled_runner_uses_fake_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "scripts").mkdir()
            (root / "scripts" / "nightly_runner.py").write_text(
                "raise RuntimeError('must not execute')\n",
                encoding="utf-8",
            )
            command = build_safe_runner_command(
                root,
                manual_override=True,
                max_jobs=1,
            )
            calls = []

            def fake_runner(*, command, cwd, timeout):
                calls.append({"command": command, "cwd": cwd, "timeout": timeout})
                return {
                    "returncode": 0,
                    "stdout": "fake ok",
                    "stderr": "",
                    "command": command,
                    "cwd": cwd,
                    "timed_out": False,
                }

            result = run_controlled_runner(
                command,
                runner=fake_runner,
                timeout_seconds=5,
            )

        self.assertEqual(result["returncode"], 0)
        self.assertEqual(result["stdout"], "fake ok")
        self.assertEqual(
            calls[0]["command"],
            EXPECTED_MANUAL_RUNNER_COMMAND,
        )
        self.assertEqual(calls[0]["timeout"], 5)

    def test_app_test_execute_tab_loads_with_flag_off_when_streamlit_available(self):
        try:
            from streamlit.testing.v1 import AppTest
        except ModuleNotFoundError:
            self.skipTest("streamlit is not installed in this Python environment")

        original_flag = os.environ.pop("KURUKIN_ENABLE_UI_RUNNER", None)
        original_queue_flag = os.environ.pop("KURUKIN_ENABLE_AROLL_BROLL_QUEUE", None)
        original_mpt_flag = os.environ.pop("KURUKIN_ENABLE_MPT_ENGINE_SUBMIT", None)
        original_cwd = Path.cwd()
        page_path = original_cwd / "webui/pages/Kurukin_Render_Console.py"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                os.chdir(tmp)
                at = AppTest.from_file(str(page_path))
                at.run(timeout=30)
        finally:
            os.chdir(original_cwd)
            if original_flag is not None:
                os.environ["KURUKIN_ENABLE_UI_RUNNER"] = original_flag
            if original_queue_flag is not None:
                os.environ["KURUKIN_ENABLE_AROLL_BROLL_QUEUE"] = original_queue_flag
            if original_mpt_flag is not None:
                os.environ["KURUKIN_ENABLE_MPT_ENGINE_SUBMIT"] = original_mpt_flag

        self.assertEqual(len(at.exception), 0)
        rendered_text = "\n".join(
            str(item.value)
            for collection in (
                at.title,
                at.markdown,
                at.info,
                at.warning,
                at.caption,
                at.json,
            )
            for item in collection
        )
        page = page_path.read_text(encoding="utf-8")
        for tab_label in ("Crear video", "Cola", "Resultados", "Preflight", "Ejecutar"):
            self.assertIn(tab_label, page)
        self.assertIn("Resultados generados", rendered_text)
        self.assertIn("Todavía no hay videos generados", rendered_text)
        self.assertIn("Ejecución controlada", rendered_text)
        self.assertIn("Motor MPT nativo", rendered_text)
        self.assertIn("Submit real MPT desactivado.", rendered_text)
        self.assertIn("Output pendiente/no encontrado todavía", rendered_text)
        self.assertIn("Procesar 1 trabajo ahora", rendered_text)
        self.assertIn(
            "salta la ventana nocturna solo para una ejecución manual controlada",
            rendered_text,
        )
        self.assertIn("Máximo de trabajos", rendered_text)
        self.assertIn(CONTAINER_NIGHTLY_QUEUE_DIR, rendered_text)
        self.assertIn(CONTAINER_API_BASE_URL, rendered_text)
        self.assertIn("--api-base-url", rendered_text)
        self.assertNotIn("<div", rendered_text)
        self.assertFalse(at.button(key="mpt_native_local_submit").disabled)
        self.assertTrue(at.button(key="controlled_runner_execute").disabled)

    def test_app_test_mpt_flag_off_button_does_not_call_submit_when_streamlit_available(self):
        try:
            from streamlit.testing.v1 import AppTest
        except ModuleNotFoundError:
            self.skipTest("streamlit is not installed in this Python environment")

        original_mpt_flag = os.environ.pop("KURUKIN_ENABLE_MPT_ENGINE_SUBMIT", None)
        original_cwd = Path.cwd()
        page_path = original_cwd / "webui/pages/Kurukin_Render_Console.py"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                os.chdir(tmp)
                with mock.patch(
                    "app.custom.kurukin_render_console.submit_mpt_native_local_job_from_console",
                    side_effect=AssertionError("submit real must stay blocked"),
                ) as fake_submit:
                    at = AppTest.from_file(str(page_path))
                    at.run(timeout=30)
                    at.button(key="mpt_native_local_submit").click()
                    at.run(timeout=30)
        finally:
            os.chdir(original_cwd)
            if original_mpt_flag is not None:
                os.environ["KURUKIN_ENABLE_MPT_ENGINE_SUBMIT"] = original_mpt_flag

        self.assertEqual(len(at.exception), 0)
        self.assertEqual(fake_submit.call_count, 0)
        rendered_text = "\n".join(
            str(getattr(item, "value", getattr(item, "label", item)))
            for collection in (
                at.warning,
                at.caption,
                at.json,
                at.text_input,
                at.text_area,
                at.button,
            )
            for item in collection
        )
        self.assertIn("Submit real MPT desactivado.", rendered_text)
        self.assertIn("mpt-native-ui-job", rendered_text)
        self.assertIn("storage/tasks/mpt-native-ui-job/final-1.mp4", rendered_text)

    def test_app_test_mpt_expected_output_preview_when_file_exists_if_streamlit_available(self):
        try:
            from streamlit.testing.v1 import AppTest
        except ModuleNotFoundError:
            self.skipTest("streamlit is not installed in this Python environment")

        original_mpt_flag = os.environ.pop("KURUKIN_ENABLE_MPT_ENGINE_SUBMIT", None)
        original_cwd = Path.cwd()
        page_path = original_cwd / "webui/pages/Kurukin_Render_Console.py"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                task_dir = tmp_path / "storage" / "tasks" / "mpt-native-ui-job"
                task_dir.mkdir(parents=True)
                (task_dir / "final-1.mp4").write_bytes(
                    b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"
                )
                os.chdir(tmp)
                at = AppTest.from_file(str(page_path))
                at.run(timeout=30)
        finally:
            os.chdir(original_cwd)
            if original_mpt_flag is not None:
                os.environ["KURUKIN_ENABLE_MPT_ENGINE_SUBMIT"] = original_mpt_flag

        self.assertEqual(len(at.exception), 0)
        rendered_text = "\n".join(
            str(getattr(item, "value", getattr(item, "label", item)))
            for collection in (
                at.markdown,
                at.info,
                at.success,
                at.warning,
                at.caption,
                at.button,
            )
            for item in collection
        )
        self.assertIn(
            "Output existe: storage/tasks/mpt-native-ui-job/final-1.mp4",
            rendered_text,
        )
        self.assertIn("Preview", rendered_text)
        self.assertIn("Descargar MP4", rendered_text)

    def test_app_test_aroll_broll_skeleton_does_not_enqueue_when_streamlit_available(self):
        try:
            from streamlit.testing.v1 import AppTest
        except ModuleNotFoundError:
            self.skipTest("streamlit is not installed in this Python environment")

        original_flag = os.environ.pop("KURUKIN_ENABLE_UI_RUNNER", None)
        original_pexels_flag = os.environ.pop("KURUKIN_ENABLE_PEXELS_SOURCE", None)
        original_cwd = Path.cwd()
        page_path = original_cwd / "webui/pages/Kurukin_Render_Console.py"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                os.chdir(tmp)
                at = AppTest.from_file(str(page_path))
                at.run(timeout=30)
                at.selectbox(key="video_type_label").set_value("Presentador + B-roll")
                at.run(timeout=30)
                at.radio(key="aroll_broll_source_label").set_value("Assets locales")
                at.run(timeout=30)
                at.text_area(key="aroll_broll_local_assets").set_value(
                    "storage/local_videos/one.mp4\n"
                    "storage/local_assets/two.mp4"
                )
                at.run(timeout=30)
                at.button(key="aroll_broll_validate").click()
                at.run(timeout=30)
                pending_dir_exists = (
                    tmp_path / "storage" / "nightly_jobs" / "pending"
                ).exists()
                tasks_dir_exists = (tmp_path / "storage" / "tasks").exists()
        finally:
            os.chdir(original_cwd)
            if original_flag is not None:
                os.environ["KURUKIN_ENABLE_UI_RUNNER"] = original_flag
            if original_pexels_flag is not None:
                os.environ["KURUKIN_ENABLE_PEXELS_SOURCE"] = original_pexels_flag

        self.assertEqual(len(at.exception), 0)
        rendered_text = "\n".join(
            str(getattr(item, "value", getattr(item, "label", item)))
            for collection in (
                at.title,
                at.markdown,
                at.info,
                at.success,
                at.warning,
                at.caption,
                at.json,
                at.selectbox,
                at.radio,
                at.button,
            )
            for item in collection
        )
        page = page_path.read_text(encoding="utf-8")
        for tab_label in ("Crear video", "Cola", "Preflight", "Ejecutar", "Resultados"):
            self.assertIn(tab_label, page)
        self.assertIn("Presentador + B-roll", rendered_text)
        self.assertIn("El audio del presentador manda", rendered_text)
        self.assertIn("B-roll muted", rendered_text)
        self.assertIn("B-roll assets: 2", rendered_text)
        self.assertIn("alternating_fullscreen", rendered_text)
        self.assertIn(
            "Renderer MVP planeado: alternating_fullscreen",
            rendered_text,
        )
        self.assertIn("Audio final: A-roll original", rendered_text)
        self.assertIn("B-roll audio: muted", rendered_text)
        self.assertIn(
            "La cola puede preparar un pending job protegido",
            rendered_text,
        )
        self.assertIn(
            "Cola A-roll/B-roll: protegida",
            rendered_text,
        )
        self.assertIn(
            "KURUKIN_ENABLE_AROLL_BROLL_QUEUE=<unset>",
            rendered_text,
        )
        self.assertIn("KURUKIN_ENABLE_PEXELS_SOURCE=<unset>", rendered_text)
        self.assertIn(
            "La cola A-roll/B-roll se activa después de una validación estricta",
            rendered_text,
        )
        self.assertNotIn("<div", rendered_text)
        self.assertFalse(pending_dir_exists)
        self.assertFalse(tasks_dir_exists)
        self.assertTrue(at.button(key="aroll_broll_enqueue_disabled").disabled)
        self.assertTrue(at.button(key="controlled_runner_execute").disabled)

    def test_app_test_prepare_broll_assets_visible_with_flags_off_when_streamlit_available(self):
        try:
            from streamlit.testing.v1 import AppTest
        except ModuleNotFoundError:
            self.skipTest("streamlit is not installed in this Python environment")

        original_runner_flag = os.environ.pop("KURUKIN_ENABLE_UI_RUNNER", None)
        original_queue_flag = os.environ.pop("KURUKIN_ENABLE_AROLL_BROLL_QUEUE", None)
        original_renderer_flag = os.environ.pop(
            "KURUKIN_ENABLE_AROLL_BROLL_RENDERER",
            None,
        )
        original_direct_flag = os.environ.pop(
            "KURUKIN_ENABLE_AROLL_BROLL_DIRECT_RENDER",
            None,
        )
        original_pexels_flag = os.environ.pop("KURUKIN_ENABLE_PEXELS_SOURCE", None)
        original_cwd = Path.cwd()
        page_path = original_cwd / "webui/pages/Kurukin_Render_Console.py"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                os.chdir(tmp)
                at = AppTest.from_file(str(page_path))
                at.run(timeout=30)
                at.selectbox(key="video_type_label").set_value("Presentador + B-roll")
                at.run(timeout=30)
                at.text_area(key="aroll_broll_prepare_local_candidates").set_value(
                    "storage/local_videos/one.mp4\n"
                    "storage/local_assets/two.mp4\n"
                    "storage/local_images/three.png"
                )
                at.run(timeout=30)
                at.button(key="aroll_broll_prepare_assets").click()
                at.run(timeout=30)
                pending_dir_exists = (
                    tmp_path / "storage" / "nightly_jobs" / "pending"
                ).exists()
                tasks_dir_exists = (tmp_path / "storage" / "tasks").exists()
        finally:
            os.chdir(original_cwd)
            if original_runner_flag is not None:
                os.environ["KURUKIN_ENABLE_UI_RUNNER"] = original_runner_flag
            if original_queue_flag is not None:
                os.environ["KURUKIN_ENABLE_AROLL_BROLL_QUEUE"] = original_queue_flag
            if original_renderer_flag is not None:
                os.environ["KURUKIN_ENABLE_AROLL_BROLL_RENDERER"] = (
                    original_renderer_flag
                )
            if original_direct_flag is not None:
                os.environ["KURUKIN_ENABLE_AROLL_BROLL_DIRECT_RENDER"] = (
                    original_direct_flag
                )
            if original_pexels_flag is not None:
                os.environ["KURUKIN_ENABLE_PEXELS_SOURCE"] = original_pexels_flag

        self.assertEqual(len(at.exception), 0)
        rendered_text = "\n".join(
            str(getattr(item, "value", getattr(item, "label", item)))
            for collection in (
                at.title,
                at.markdown,
                at.info,
                at.success,
                at.warning,
                at.caption,
                at.json,
                at.selectbox,
                at.radio,
                at.button,
                at.text_area,
                at.number_input,
            )
            for item in collection
        )
        page = page_path.read_text(encoding="utf-8")
        self.assertIn("Preparar B-roll", rendered_text)
        self.assertIn(
            "Pexels source: disponible solo con integración controlada/flag; no se usa por defecto.",
            rendered_text,
        )
        self.assertIn("KURUKIN_ENABLE_PEXELS_SOURCE=<unset>", rendered_text)
        self.assertIn("Local candidates", page)
        self.assertIn("Desired count", page)
        self.assertIn("B-roll assets preparados", rendered_text)
        self.assertIn("B-roll assets", rendered_text)
        self.assertIn("storage/local_videos/one.mp4", rendered_text)
        self.assertIn("storage/local_assets/two.mp4", rendered_text)
        self.assertIn("storage/local_images/three.png", rendered_text)
        self.assertNotIn("<div", rendered_text)
        self.assertFalse(pending_dir_exists)
        self.assertFalse(tasks_dir_exists)
        self.assertTrue(at.button(key="aroll_broll_enqueue_disabled").disabled)
        self.assertTrue(at.button(key="controlled_runner_execute").disabled)

    def test_app_test_results_tab_shows_temp_mp4_when_streamlit_available(self):
        try:
            from streamlit.testing.v1 import AppTest
        except ModuleNotFoundError:
            self.skipTest("streamlit is not installed in this Python environment")

        original_flag = os.environ.pop("KURUKIN_ENABLE_UI_RUNNER", None)
        original_cwd = Path.cwd()
        page_path = original_cwd / "webui/pages/Kurukin_Render_Console.py"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                task_dir = tmp_path / "storage" / "tasks" / "task-results-001"
                task_dir.mkdir(parents=True)
                (task_dir / "final-1.mp4").write_bytes(
                    b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"
                )
                (task_dir / "final-task.json").write_text(
                    json.dumps(
                        {
                            "state": "completed",
                            "progress": 100,
                            "videos": ["final-1.mp4"],
                            "job_id": "job-results-001",
                        }
                    ),
                    encoding="utf-8",
                )
                os.chdir(tmp)
                self.assertGreater(len(list_rendered_videos("storage/tasks")), 0)
                at = AppTest.from_file(str(page_path))
                at.run(timeout=30)
        finally:
            os.chdir(original_cwd)
            if original_flag is not None:
                os.environ["KURUKIN_ENABLE_UI_RUNNER"] = original_flag

        self.assertEqual(len(at.exception), 0)
        rendered_text = "\n".join(
            str(getattr(item, "value", getattr(item, "label", item)))
            for collection in (
                at.title,
                at.markdown,
                at.info,
                at.success,
                at.warning,
                at.caption,
                at.json,
                at.selectbox,
                at.button,
            )
            for item in collection
        )
        self.assertIn("Resultados generados", rendered_text)
        self.assertIn("Último video generado", rendered_text)
        self.assertIn("Todos los videos generados", rendered_text)
        self.assertIn("task-results-001", rendered_text)
        self.assertIn("final-1.mp4", rendered_text)
        self.assertIn("Preview", rendered_text)
        self.assertIn("Preview disponible", rendered_text)
        self.assertIn("Descargar MP4", rendered_text)
        self.assertNotIn("<div", rendered_text)
        self.assertTrue(at.button(key="controlled_runner_execute").disabled)
        self.assertFalse((Path(tmp) / "storage" / "nightly_jobs" / "pending").exists())

    def test_app_test_results_and_queue_show_aroll_broll_completed_job_when_streamlit_available(self):
        try:
            from streamlit.testing.v1 import AppTest
        except ModuleNotFoundError:
            self.skipTest("streamlit is not installed in this Python environment")

        original_flag = os.environ.pop("KURUKIN_ENABLE_UI_RUNNER", None)
        original_cwd = Path.cwd()
        page_path = original_cwd / "webui/pages/Kurukin_Render_Console.py"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                task_id = "aroll-broll-runner-smoke-003"
                job_id = "aroll-broll-runner-smoke-003"
                task_dir = tmp_path / "storage" / "tasks" / task_id
                completed_dir = (
                    tmp_path
                    / "storage"
                    / "nightly_jobs"
                    / "completed"
                    / "20260708-164846-aroll-broll-runner-smoke-003"
                )
                task_dir.mkdir(parents=True)
                completed_dir.mkdir(parents=True)
                (task_dir / "final-1.mp4").write_bytes(
                    b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"
                )
                (completed_dir / "job.json").write_text(
                    json.dumps(
                        {
                            "job_id": job_id,
                            "render_mode": "aroll_broll",
                            "aroll_broll": {
                                "layout": {"preset": "alternating_fullscreen"},
                                "b_roll": {
                                    "assets": [
                                        "storage/local_assets/one.mp4",
                                        "storage/local_assets/two.mp4",
                                        "storage/local_assets/three.mp4",
                                    ]
                                },
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                (completed_dir / "submit-response.json").write_text(
                    json.dumps(
                        {
                            "data": {"task_id": task_id},
                            "status": 200,
                            "render_mode": "aroll_broll",
                        }
                    ),
                    encoding="utf-8",
                )
                (completed_dir / "final-task.json").write_text(
                    json.dumps(
                        {
                            "status": 200,
                            "message": "success",
                            "data": {
                                "state": "completed",
                                "progress": 100,
                                "task_id": task_id,
                                "render_mode": "aroll_broll",
                                "layout_preset": "alternating_fullscreen",
                                "videos": [f"/tasks/{task_id}/final-1.mp4"],
                            }
                        }
                    ),
                    encoding="utf-8",
                )
                os.chdir(tmp)
                at = AppTest.from_file(str(page_path))
                at.session_state["last_enqueued_job_id"] = job_id
                at.run(timeout=30)
                pending_dir_exists = (
                    tmp_path / "storage" / "nightly_jobs" / "pending"
                ).exists()
        finally:
            os.chdir(original_cwd)
            if original_flag is not None:
                os.environ["KURUKIN_ENABLE_UI_RUNNER"] = original_flag

        self.assertEqual(len(at.exception), 0)
        rendered_text = "\n".join(
            str(getattr(item, "value", getattr(item, "label", item)))
            for collection in (
                at.title,
                at.markdown,
                at.info,
                at.success,
                at.warning,
                at.caption,
                at.json,
                at.selectbox,
                at.button,
            )
            for item in collection
        )
        self.assertIn("Tu video más reciente", rendered_text)
        self.assertIn("Cola y resultados", rendered_text)
        self.assertIn("Jobs completados", rendered_text)
        self.assertIn("Presentador + B-roll", rendered_text)
        self.assertIn("Tipo: Presentador + B-roll", rendered_text)
        self.assertIn("Layout: alternating_fullscreen", rendered_text)
        self.assertIn("Audio: A-roll original", rendered_text)
        self.assertIn("B-roll muted", rendered_text)
        self.assertIn("B-roll assets: 3", rendered_text)
        self.assertIn("Task ID: aroll-broll-runner-smoke-003", rendered_text)
        self.assertIn("Todos los videos generados", rendered_text)
        self.assertIn(job_id, rendered_text)
        self.assertIn(task_id, rendered_text)
        self.assertIn("final-1.mp4", rendered_text)
        self.assertIn("Preview", rendered_text)
        self.assertIn("Preview disponible", rendered_text)
        self.assertIn("Descargar MP4", rendered_text)
        self.assertNotIn("<div", rendered_text)
        self.assertFalse(pending_dir_exists)
        self.assertTrue(at.button(key="controlled_runner_execute").disabled)

    def test_build_workflow_payload_keeps_asset_hub_material_count_zero(self):
        payload = build_workflow_payload(
            {
                "job_id": "render-console-ui-smoke-001",
                "video_subject": "Smoke",
                "video_script": "Smoke script.",
                "render_quality": "draft_720p",
                "video_aspect": "9:16",
                "asset_hub_bundle_uid": BUNDLE_UID,
                "subtitles_mode": "none",
                "image_motion_enabled": True,
                "image_motion_preset": "slow_zoom_in",
            }
        )

        self.assertEqual(payload["job_id"], "render-console-ui-smoke-001")
        self.assertEqual(payload["asset_hub_bundle_uid"], BUNDLE_UID)
        self.assertEqual(payload["video_resolution"], "draft_720p")
        self.assertNotIn("video_materials", payload)


if __name__ == "__main__":
    unittest.main()
