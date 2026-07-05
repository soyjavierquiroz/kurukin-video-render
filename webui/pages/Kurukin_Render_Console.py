import json
import os
import sys
from datetime import datetime, timezone

import streamlit as st


ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
if ROOT_DIR not in sys.path:
    sys.path.append(ROOT_DIR)

from app.custom.kurukin_job_queue import (  # noqa: E402
    enqueue_moneyprinter_payload,
    get_storage_summary,
    list_nightly_queue,
    list_render_tasks,
)
from app.custom.kurukin_render_console import (  # noqa: E402
    build_operator_summary,
    build_render_console_spec,
    default_asset_hub_manifest_path,
    get_manifest_summary_for_ui,
    validate_and_build_payload_from_console_spec,
)


DEFAULT_BUNDLE_UID = "jab_b28367fb22d44a40bae507c175f464c4"

EXAMPLE_SPEC = {
    "job_id": "render-console-example-001",
    "description": "Render Console example",
    "asset_hub": {
        "renderer_manifest_path": (
            "/data/job-assets/jab_b28367fb22d44a40bae507c175f464c4/"
            "manifests/renderer-manifest.json"
        ),
        "bundle_uid": DEFAULT_BUNDLE_UID,
        "scene_mode": "ordered",
        "strict": True,
    },
    "render_quality": "draft_720p",
    "subtitles": {"mode": "none"},
    "video": {
        "video_subject": "Render Console Example",
        "video_script": "Example script.",
        "video_aspect": "9:16",
        "video_concat_mode": "sequential",
        "video_transition_mode": "None",
        "video_clip_duration": 4,
        "video_count": 1,
        "voice_name": "es-MX-DaliaNeural-Female",
        "voice_volume": 1.0,
        "voice_rate": 1.0,
        "bgm_type": "none",
        "subtitle_enabled": False,
        "n_threads": 2,
        "paragraph_number": 1,
    },
}


def _default_job_id() -> str:
    return f"render-console-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"


def _human_bytes(size_bytes):
    try:
        value = float(size_bytes)
    except (TypeError, ValueError):
        return "0 B"
    units = ("B", "KB", "MB", "GB", "TB")
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TB"


def _show_error(exc):
    st.error(str(exc))


def _manifest_summary_block(summary):
    status = summary.get("status")
    if status == "ready":
        st.success("Manifest encontrado y válido. El worker resolverá estos assets al iniciar el render.")
        cols = st.columns(5)
        cols[0].metric("Escenas", summary.get("total_scenes", 0))
        cols[1].metric("Assets", summary.get("total_assets", 0))
        cols[2].metric(
            "Duración aprox.",
            f"{summary.get('duration_total_seconds', 0)} s",
        )
        cols[3].metric("Warnings", summary.get("warnings_count", 0))
        cols[4].metric("Revisión humana", summary.get("needs_human_review_count", 0))
        st.caption(f"Bundle UID: {summary.get('bundle_uid') or '-'}")
        asset_types = summary.get("asset_types") or {}
        if asset_types:
            st.caption(
                "Tipos: "
                + ", ".join(f"{key}: {value}" for key, value in sorted(asset_types.items()))
            )
        filenames = summary.get("preview_filenames") or []
        if filenames:
            st.caption("Primeros assets: " + ", ".join(filenames))
    elif status == "not_found":
        st.warning("Manifest no encontrado. Revisa que el bundle esté materializado en /data/job-assets.")
    elif status == "missing_path":
        st.info("Ingresa un renderer manifest para ver el resumen.")
    else:
        st.error(f"Manifest inválido: {summary.get('message')}")


def _operator_summary_block(operator):
    cols = st.columns(6)
    cols[0].metric("Job ID", operator.get("job_id") or "-")
    cols[1].metric("Calidad", operator.get("render_quality") or "-")
    cols[2].metric("Modo", operator.get("mode") or "-")
    cols[3].metric("Assets manifest", operator.get("manifest_asset_count", 0))
    cols[4].metric("Subtítulos", operator.get("subtitles") or "none")
    cols[5].metric("Audio", operator.get("audio") or "-")
    st.caption(f"Asunto: {operator.get('subject') or '-'}")
    if operator.get("bundle_uid"):
        st.caption(f"Bundle UID: {operator.get('bundle_uid')}")
    if operator.get("note"):
        st.info(operator["note"])
    if (
        operator.get("mode") == "Asset Hub manifest"
        and operator.get("payload_material_count") == 0
        and operator.get("manifest_asset_count", 0) > 0
    ):
        st.success("Correcto: el payload no expande assets. El worker los leerá del manifest.")


def _show_validation_result(spec, payload, manifest_summary, operator):
    st.success("Payload válido para encolar.")
    _operator_summary_block(operator)
    with st.expander("Ver Kurukin Job Spec", expanded=False):
        st.json(spec)
    with st.expander("Ver MoneyPrinterTurbo Payload", expanded=False):
        st.json(payload)


def _build_form_spec():
    return build_render_console_spec(
        job_id=st.session_state["job_id"],
        video_subject=st.session_state["video_subject"],
        video_script=st.session_state["video_script"],
        render_quality=st.session_state["render_quality"],
        video_aspect=st.session_state["video_aspect"],
        asset_hub_bundle_uid=st.session_state["asset_hub_bundle_uid"],
        asset_hub_renderer_manifest_path=st.session_state[
            "asset_hub_renderer_manifest_path"
        ],
        audio_file=st.session_state["audio_file"],
        subtitles_mode=st.session_state["subtitles_mode"],
        custom_subtitle_file=st.session_state.get("custom_subtitle_file", ""),
        subtitle_style_preset=st.session_state["subtitle_style_preset"],
        image_motion_enabled=st.session_state["image_motion_enabled"],
        image_motion_preset=st.session_state["image_motion_preset"],
        image_motion_intensity=st.session_state["image_motion_intensity"],
        video_clip_duration=int(st.session_state["video_clip_duration"]),
        n_threads=int(st.session_state["n_threads"]),
    )


def _validate_form_payload():
    spec = _build_form_spec()
    payload, _ = validate_and_build_payload_from_console_spec(spec)
    manifest_path = payload.get("asset_hub_renderer_manifest_path") or ""
    manifest_summary = get_manifest_summary_for_ui(manifest_path)
    operator = build_operator_summary(payload, manifest_summary)
    st.session_state["form_validated_spec"] = spec
    st.session_state["form_validated_payload"] = payload
    st.session_state["form_manifest_summary"] = manifest_summary
    st.session_state["form_operator_summary"] = operator
    return spec, payload, manifest_summary, operator


def _initialize_form_state():
    st.session_state.setdefault("job_id", _default_job_id())
    st.session_state.setdefault("video_subject", "Render Console Example")
    st.session_state.setdefault("render_quality", "draft_720p")
    st.session_state.setdefault("video_aspect", "9:16")
    st.session_state.setdefault("audio_file", "")
    st.session_state.setdefault("subtitles_mode", "none")
    st.session_state.setdefault("custom_subtitle_file", "")
    st.session_state.setdefault("asset_hub_bundle_uid", DEFAULT_BUNDLE_UID)
    st.session_state.setdefault("subtitle_style_preset", "clean_center_bold_safe")
    st.session_state.setdefault("image_motion_enabled", True)
    st.session_state.setdefault("image_motion_preset", "slow_zoom_in")
    st.session_state.setdefault("image_motion_intensity", 0.06)
    st.session_state.setdefault("video_clip_duration", 4)
    st.session_state.setdefault("n_threads", 2)
    st.session_state.setdefault("video_script", "Example script.")

    bundle_uid = st.session_state.get("asset_hub_bundle_uid", "")
    previous_bundle_uid = st.session_state.get("_manifest_bundle_uid")
    if bundle_uid and bundle_uid != previous_bundle_uid:
        try:
            st.session_state["asset_hub_renderer_manifest_path"] = (
                default_asset_hub_manifest_path(bundle_uid)
            )
            st.session_state["_manifest_bundle_uid"] = bundle_uid
        except ValueError as exc:
            st.warning(str(exc))
    st.session_state.setdefault(
        "asset_hub_renderer_manifest_path",
        default_asset_hub_manifest_path(DEFAULT_BUNDLE_UID),
    )


def _new_render_tab():
    st.subheader("Nuevo render")

    status_cols = st.columns(3)
    status_cols[0].metric("Modo actual", "Asset Hub manifest-first")
    status_cols[1].metric("Queue target", "storage/nightly_jobs/pending")
    status_cols[2].metric("Ejecución", "Solo encola")
    st.warning("Encolar no ejecuta render. El nightly runner procesa después.")

    _initialize_form_state()

    col_a, col_b = st.columns(2)
    with col_a:
        st.text_input("ID del job", key="job_id")
        st.text_input("Título / asunto del video", key="video_subject")
        st.selectbox(
            "Calidad",
            ["draft_720p", "standard_1080p", "premium_2k"],
            key="render_quality",
        )
        st.selectbox("Formato", ["9:16", "16:9"], key="video_aspect")
        st.text_input("Audio propio opcional", key="audio_file", placeholder="audio-prueba.mp3")
        st.selectbox(
            "Subtítulos",
            ["none", "whisper", "edge", "custom_srt"],
            key="subtitles_mode",
        )
        if st.session_state["subtitles_mode"] == "custom_srt":
            st.text_input("Archivo .srt propio", key="custom_subtitle_file")

    with col_b:
        st.text_input("Bundle UID de Asset Hub", key="asset_hub_bundle_uid")
        st.caption("Bundle de prueba para entorno dev/local.")
        st.text_input(
            "Ruta del renderer manifest",
            key="asset_hub_renderer_manifest_path",
        )
        st.selectbox(
            "Estilo de subtítulos",
            [
                "clean_center_bold_safe",
                "clean_center_bold",
                "clean_bottom_bold",
                "boxed_bottom",
                "large_hook_center",
            ],
            key="subtitle_style_preset",
        )
        st.checkbox("Animar imágenes", key="image_motion_enabled")
        st.selectbox(
            "Preset de animación",
            [
                "none",
                "slow_zoom_in",
                "slow_zoom_out",
                "pan_left",
                "pan_right",
                "pan_up",
                "pan_down",
                "subtle_pulse",
                "handheld_soft",
            ],
            key="image_motion_preset",
        )
        st.slider(
            "Intensidad de animación",
            min_value=0.0,
            max_value=0.20,
            step=0.01,
            key="image_motion_intensity",
        )
        st.number_input(
            "Duración por asset",
            min_value=1,
            max_value=20,
            step=1,
            key="video_clip_duration",
        )
        st.number_input("Threads", min_value=1, max_value=4, step=1, key="n_threads")

    st.text_area("Guion o descripción del render", height=180, key="video_script")

    st.markdown("**Resumen del manifest**")
    manifest_summary = get_manifest_summary_for_ui(
        st.session_state.get("asset_hub_renderer_manifest_path", "")
    )
    _manifest_summary_block(manifest_summary)

    actions = st.columns([1, 1, 4])
    validate_clicked = actions[0].button("Validar payload", key="form_validate")
    enqueue_clicked = actions[1].button("Encolar job", key="form_enqueue")

    if validate_clicked or enqueue_clicked:
        try:
            spec, payload, manifest_summary, operator = _validate_form_payload()
            _show_validation_result(spec, payload, manifest_summary, operator)
            if enqueue_clicked:
                path = enqueue_moneyprinter_payload(payload)
                st.success("Job encolado. No se ejecutó render.")
                st.code(str(path))
                st.info(
                    "Para procesar, ejecutar nightly_runner manualmente o esperar "
                    "ventana nocturna."
                )
        except Exception as exc:
            _show_error(exc)
    elif st.session_state.get("form_validated_payload"):
        _show_validation_result(
            st.session_state["form_validated_spec"],
            st.session_state["form_validated_payload"],
            st.session_state["form_manifest_summary"],
            st.session_state["form_operator_summary"],
        )


def _advanced_json_tab():
    st.subheader("JSON avanzado")
    st.write("Pega un Kurukin Job Spec completo. Úsalo para casos avanzados o pruebas.")
    raw_spec = st.text_area(
        "Kurukin Job Spec JSON",
        value=json.dumps(EXAMPLE_SPEC, indent=2, ensure_ascii=False),
        height=420,
    )

    actions = st.columns([1, 1, 4])
    validate_clicked = actions[0].button("Validar JSON", key="json_validate")
    enqueue_clicked = actions[1].button("Encolar JSON validado", key="json_enqueue")

    if validate_clicked or enqueue_clicked:
        try:
            spec = json.loads(raw_spec)
            if not isinstance(spec, dict):
                raise ValueError("JSON must be an object")
            payload, _ = validate_and_build_payload_from_console_spec(spec)
            manifest_summary = get_manifest_summary_for_ui(
                payload.get("asset_hub_renderer_manifest_path") or ""
            )
            operator = build_operator_summary(payload, manifest_summary)
            st.session_state["json_validated_payload"] = payload
            st.session_state["json_validated_spec"] = spec
            st.session_state["json_manifest_summary"] = manifest_summary
            st.session_state["json_operator_summary"] = operator

            st.success("JSON válido para encolar.")
            if manifest_summary.get("status") != "missing_path":
                _manifest_summary_block(manifest_summary)
            _operator_summary_block(operator)
            with st.expander("Ver MoneyPrinterTurbo Payload", expanded=False):
                st.json(payload)
            if enqueue_clicked:
                path = enqueue_moneyprinter_payload(payload)
                st.success("Job encolado. No se ejecutó render.")
                st.code(str(path))
        except Exception as exc:
            _show_error(exc)
    elif st.session_state.get("json_validated_payload"):
        if st.session_state["json_manifest_summary"].get("status") != "missing_path":
            _manifest_summary_block(st.session_state["json_manifest_summary"])
        _operator_summary_block(st.session_state["json_operator_summary"])
        with st.expander("Ver MoneyPrinterTurbo Payload", expanded=False):
            st.json(st.session_state["json_validated_payload"])


def _queue_table(title, rows):
    st.markdown(f"**{title}**")
    if rows:
        st.dataframe(rows, use_container_width=True)
    else:
        st.info("Sin jobs")


def _queue_storage_tab():
    st.subheader("Cola y storage")
    st.button("Actualizar", key="refresh_queue")

    queue = list_nightly_queue()
    tasks = list_render_tasks()
    storage_summary = get_storage_summary()

    final_tasks = [item for item in tasks if item.get("has_final_video")]
    metric_cols = st.columns(6)
    metric_cols[0].metric("Pending", len(queue.get("pending", [])))
    metric_cols[1].metric("Processing", len(queue.get("processing", [])))
    metric_cols[2].metric("Completed", len(queue.get("completed", [])))
    metric_cols[3].metric("Failed", len(queue.get("failed", [])))
    metric_cols[4].metric("Tasks con video final", len(final_tasks))
    metric_cols[5].metric("Storage total", _human_bytes(storage_summary.get("size_bytes")))

    for group in ("pending", "processing", "completed", "failed"):
        _queue_table(group.capitalize(), queue.get(group, []))

    _queue_table("Tasks", tasks)
    st.markdown("**Storage**")
    subdirs = storage_summary.get("subdirs") or []
    if subdirs:
        rows = [
            {
                "name": item.get("name"),
                "path": item.get("path"),
                "size": _human_bytes(item.get("size_bytes")),
                "size_bytes": item.get("size_bytes"),
                "modified_at_iso": item.get("modified_at_iso"),
            }
            for item in subdirs
        ]
        st.dataframe(rows, use_container_width=True)
    else:
        st.info("Sin jobs")
    st.info("Limpieza destructiva se implementará en una fase posterior.")


st.set_page_config(page_title="Kurukin Render Console", layout="wide")
st.title("Kurukin Render Console")
st.write(
    "Crea, valida y encola jobs para el worker nocturno. "
    "Esta pantalla no renderiza directamente."
)

tab_new, tab_json, tab_queue = st.tabs(["Nuevo render", "JSON avanzado", "Cola y storage"])
with tab_new:
    _new_render_tab()
with tab_json:
    _advanced_json_tab()
with tab_queue:
    _queue_storage_tab()
