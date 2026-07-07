import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.custom.kurukin_job_adapter import KurukinJobAdapterError
from app.custom.kurukin_job_queue import (
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
    is_ui_runner_enabled,
    list_pending_jobs,
    list_task_summaries,
    run_controlled_runner,
    validate_runner_execution_request,
)
from app.custom.kurukin_render_console import (
    ASSET_SOURCE_LOCAL,
    ASSET_SOURCE_ASSET_HUB,
    ASSET_SOURCE_STOCK,
    SOURCE_MODE_ASSET_HUB,
    SOURCE_MODE_LOCAL,
    SOURCE_MODE_STOCK,
    build_operator_summary,
    build_render_console_spec,
    build_workflow_payload,
    default_asset_hub_manifest_path,
    get_manifest_summary_for_ui,
    list_local_storage_files,
    safe_relative_path,
    validate_and_build_payload_from_console_spec,
)


BUNDLE_UID = "jab_b28367fb22d44a40bae507c175f464c4"


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
            "command": ["python3", "scripts/nightly_runner.py"],
            "cwd": "/tmp/project",
            "reason": "ready",
            "confidence": "high",
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
        )

        self.assertFalse(result["allowed"])
        self.assertIn("Texto de confirmación incorrecto.", result["errors"])

    def test_validate_runner_execution_request_passes_with_all_gates(self):
        preflight, command = self._runner_request_fixture()

        result = validate_runner_execution_request(
            feature_enabled=True,
            preflight_summary=preflight,
            command_info=command,
            understood=True,
            confirm_text=RUNNER_CONFIRM_TEXT,
            queue_confirmation=RUNNER_QUEUE_CONFIRM_TEXT,
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
            command = build_safe_runner_command(root)
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
        self.assertEqual(calls[0]["command"], ["python3", "scripts/nightly_runner.py"])
        self.assertEqual(calls[0]["timeout"], 5)

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
