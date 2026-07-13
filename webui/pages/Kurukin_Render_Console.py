import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st


ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
if ROOT_DIR not in sys.path:
    sys.path.append(ROOT_DIR)

from app.custom.aroll_broll_mode import (  # noqa: E402
    ALLOWED_LAYOUT_PRESETS,
    BROLL_SOURCE_ASSET_HUB_MANIFEST,
    BROLL_SOURCE_LOCAL_ASSETS,
    LAYOUT_ALTERNATING_FULLSCREEN,
    RENDER_MODE_AROLL_BROLL,
    SPEAKER_CROP_BOTTOM,
    SPEAKER_CROP_CENTER,
    SPEAKER_CROP_TOP,
    SUBTITLES_SOURCE_AROLL_AUDIO,
    SUBTITLES_SOURCE_CUSTOM_SRT,
    SUBTITLES_SOURCE_NONE,
    build_aroll_broll_preview_timeline,
    build_default_aroll_broll_config,
    normalize_broll_asset_values,
    summarize_aroll_broll_config,
    validate_aroll_broll_config,
)
from app.custom.aroll_broll_renderer import (  # noqa: E402
    ArollBrollRendererError,
    extract_broll_assets_from_manifest,
)
from app.custom.kurukin_job_adapter import (  # noqa: E402
    ALLOWED_AUDIO_EXTENSIONS,
    ALLOWED_EXTENSIONS,
    ALLOWED_SUBTITLE_EXTENSIONS,
    DEFAULT_LOCAL_AUDIOS_DIR,
    DEFAULT_LOCAL_SUBTITLES_DIR,
    DEFAULT_LOCAL_VIDEOS_DIR,
)
from app.custom.kurukin_batch_intents import enqueue_audio_batch_intents  # noqa: E402
from app.custom.kurukin_job_queue import (  # noqa: E402
    AROLL_BROLL_QUEUE_FLAG,
    CONTAINER_API_BASE_URL,
    CONTAINER_NIGHTLY_QUEUE_DIR,
    MANUAL_RUNNER_EXECUTION_MODE,
    MANUAL_RUNNER_MAX_JOBS,
    RUNNER_CONFIRM_TEXT,
    RUNNER_QUEUE_CONFIRM_TEXT,
    VIDEO_DOWNLOAD_MEMORY_MAX_BYTES,
    VIDEO_PREVIEW_MAX_BYTES,
    build_safe_runner_command,
    enqueue_moneyprinter_payload,
    get_job_lifecycle_summary,
    get_latest_rendered_video,
    get_recommended_result,
    get_runner_preflight_summary,
    is_aroll_broll_queue_enabled,
    is_ui_runner_enabled,
    list_intent_results,
    list_rendered_videos,
    read_video_bytes_for_download,
    run_controlled_runner,
    sanitize_job_id,
    validate_runner_execution_request,
)
from app.custom.kurukin_job_intent import (  # noqa: E402
    MODE_AUDIO_TO_VIDEO,
    MODE_SPEAKER_VIDEO_TO_ENHANCED_VIDEO,
    MODE_TOPIC_TO_VIDEO,
    STATUS_READY_TO_SUBMIT,
    compile_job_intent_to_mpt_spec,
    validate_job_intent,
)
from app.custom.kurukin_render_console import (  # noqa: E402
    ASSET_SOURCE_ASSET_HUB,
    ASSET_SOURCE_LOCAL,
    ASSET_SOURCE_STOCK,
    MPT_ENGINE_SUBMIT_FLAG,
    PEXELS_SOURCE_FLAG,
    build_aroll_broll_payload_from_console,
    build_operator_summary,
    build_render_console_spec,
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
from app.custom.asset_source_policy import (  # noqa: E402
    ASSET_SOURCE_MODE_EXCLUSIVE_BRAND_ASSETS,
    ASSET_SOURCE_MODE_LOCAL_ONLY,
    ASSET_SOURCE_MODE_OPEN_SOURCES,
)
from app.custom.pexels_source import create_pexels_downloader  # noqa: E402


DEFAULT_BUNDLE_UID = "jab_b28367fb22d44a40bae507c175f464c4"
VIDEO_TYPE_LABELS = {
    "Video normal con assets": "normal_assets",
    "Presentador + B-roll": RENDER_MODE_AROLL_BROLL,
}
VIDEO_TYPE_LABEL_BY_VALUE = {value: key for key, value in VIDEO_TYPE_LABELS.items()}
ASSET_SOURCE_LABELS = {
    "Asset Hub Bundle": ASSET_SOURCE_ASSET_HUB,
    "Assets locales": ASSET_SOURCE_LOCAL,
    "Stock externo": ASSET_SOURCE_STOCK,
}
ASSET_SOURCE_LABEL_BY_VALUE = {value: key for key, value in ASSET_SOURCE_LABELS.items()}
SUBTITLE_MODE_LABELS = {
    "Sin subtítulos": "none",
    "SRT propio": "custom_srt",
    "Whisper": "whisper",
    "Edge": "edge",
}
SUBTITLE_MODE_LABEL_BY_VALUE = {value: key for key, value in SUBTITLE_MODE_LABELS.items()}
QUALITY_LABELS = {
    "Borrador 720p": "draft_720p",
    "Estándar 1080p": "standard_1080p",
    "Premium 2K": "premium_2k",
}
QUALITY_LABEL_BY_VALUE = {value: key for key, value in QUALITY_LABELS.items()}
AROLL_BROLL_SOURCE_LABELS = {
    "Asset Hub Bundle": BROLL_SOURCE_ASSET_HUB_MANIFEST,
    "Assets locales": BROLL_SOURCE_LOCAL_ASSETS,
}
AROLL_BROLL_PREPARE_POLICY_LABELS = {
    "Solo locales": ASSET_SOURCE_MODE_LOCAL_ONLY,
    "Fuentes abiertas": ASSET_SOURCE_MODE_OPEN_SOURCES,
    "Marca exclusiva": ASSET_SOURCE_MODE_EXCLUSIVE_BRAND_ASSETS,
}
AROLL_BROLL_SUBTITLE_LABELS = {
    "none": SUBTITLES_SOURCE_NONE,
    "custom_srt": SUBTITLES_SOURCE_CUSTOM_SRT,
    "aroll_audio (futuro)": SUBTITLES_SOURCE_AROLL_AUDIO,
}
AROLL_BROLL_LAYOUTS = [
    LAYOUT_ALTERNATING_FULLSCREEN,
    "vertical_split_a_top",
    "vertical_split_b_top",
    "broll_fullscreen_speaker_bubble",
    "aroll_main_broll_lower_panel",
]
AROLL_BROLL_CROPS = [
    SPEAKER_CROP_CENTER,
    SPEAKER_CROP_TOP,
    SPEAKER_CROP_BOTTOM,
]
END_TO_END_FLOW_STEPS = (
    "Crear video",
    "Validar",
    "Enviar a cola",
    "Ejecutar runner controlado",
    "API Docker",
    "Render",
    "MP4",
    "Resultados",
    "Tu video más reciente",
    "Preview",
    "Descargar MP4",
)
MOTION_PROFILES = [
    "none",
    "slow_zoom_in",
    "slow_zoom_out",
    "pan_left",
    "pan_right",
    "pan_up",
    "pan_down",
    "subtle_pulse",
    "handheld_soft",
]
SUBTITLE_STYLE_PRESETS = [
    "clean_center_bold_safe",
    "clean_center_bold",
    "clean_bottom_bold",
    "boxed_bottom",
    "large_hook_center",
]
JOB_INTENT_MODE_LABELS = {
    "Tema a video": MODE_TOPIC_TO_VIDEO,
    "Audio a video": MODE_AUDIO_TO_VIDEO,
    "Presentador a video mejorado": MODE_SPEAKER_VIDEO_TO_ENHANCED_VIDEO,
}
JOB_INTENT_FORMATS = ["vertical", "horizontal", "square"]


def _default_job_id() -> str:
    return f"kurukin-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"


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


def _format_datetime(value):
    return value or "-"


def _show_error(exc):
    st.error(str(exc))


def _state_label(status):
    return {
        "ready": "Bundle listo",
        "invalid": "Requiere atención",
        "not_found": "No encontrado",
        "missing_path": "Pendiente",
    }.get(status, "Pendiente")


def _apply_page_style():
    st.markdown(
        """
        <style>
        .stApp {
            background: #f6f8fb;
            color: #111827;
        }
        [data-testid="stHeader"] {
            background: rgba(246, 248, 251, 0.86);
            backdrop-filter: blur(10px);
        }
        [data-testid="stToolbar"] {
            right: 1.25rem;
        }
        .block-container {
            max-width: 1180px;
            padding-top: 1.6rem;
            padding-bottom: 3rem;
        }
        h1, h2, h3 {
            color: #111827;
            letter-spacing: 0;
        }
        label, p, li, span, [data-testid="stMarkdownContainer"] {
            color: #111827;
        }
        [data-testid="stWidgetLabel"] p {
            color: #111827;
            font-weight: 650;
        }
        .stAlert {
            border-radius: 8px;
        }
        [data-testid="stMetric"] {
            background: #ffffff;
            border: 1px solid #e2e8f0;
            border-radius: 8px;
            padding: 0.85rem 0.9rem;
        }
        [data-testid="stMetricLabel"] {
            color: #64748b;
        }
        div.stButton > button {
            border-radius: 8px;
            border: 1px solid #2563eb;
            background: #2563eb;
            color: #ffffff;
            font-weight: 720;
            min-height: 2.8rem;
            box-shadow: 0 10px 18px rgba(37, 99, 235, 0.18);
        }
        div.stButton > button:hover {
            border-color: #1d4ed8;
            background: #1d4ed8;
            color: #ffffff;
        }
        [data-testid="stExpander"] {
            border: 1px solid #e2e8f0;
            border-radius: 8px;
            background: #ffffff;
            box-shadow: none;
        }
        [data-testid="stMarkdownContainer"] hr {
            border-color: #e2e8f0;
            margin: 0.65rem 0;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _index_for_value(labels, value, default_value):
    values = list(labels.values())
    try:
        return values.index(value)
    except ValueError:
        return values.index(default_value)


def _select_label_for_value(mapping, value, default_value):
    return mapping.get(value, mapping[default_value])


def _format_duration(seconds):
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        value = 0.0
    return f"{value:.2f} s aprox."


def _hero_block():
    st.title("Crear video Kurukin")
    st.write(
        "Prepara un video usando assets aprobados, audio propio y subtítulos, "
        "y envíalo a la cola de render cuando esté listo."
    )
    st.info(
        "Esta pantalla no renderiza inmediatamente. Solo prepara y envía trabajos "
        "a la cola."
    )


def _end_to_end_flow_block():
    st.markdown("### Flujo completo")
    st.caption(" -> ".join(END_TO_END_FLOW_STEPS))


def _first_video_guide():
    st.info(
        "Para crear tu primer video:\n\n"
        "1. Pega el código del paquete de assets.\n"
        "2. Revisa el título, audio, subtítulos y calidad.\n"
        "3. Presiona Validar video.\n"
        "4. Si todo está listo, presiona Enviar a cola.\n\n"
        "Validar video no crea archivos ni renderiza. Enviar a cola solo crea un trabajo pendiente."
    )


def _progress_steps():
    steps = (
        ("1", "Assets", "Elige visuales aprobados"),
        ("2", "Contenido", "Define título y guion"),
        ("3", "Audio", "Suma audio o subtítulos"),
        ("4", "Estilo", "Ajusta formato y movimiento"),
        ("5", "Revisión", "Valida antes de encolar"),
    )
    cols = st.columns(5)
    for col, (number, title, caption) in zip(cols, steps):
        with col:
            st.markdown(f"**{number}. {title}**")
            st.caption(caption)


def _asset_mode_description(selected_mode):
    if selected_mode == ASSET_SOURCE_ASSET_HUB:
        st.success(
            "Asset Hub Bundle recomendado: usa un paquete aprobado y su manifest "
            "derivado automáticamente."
        )
    elif selected_mode == ASSET_SOURCE_LOCAL:
        st.info("Assets locales: usa archivos ya disponibles en el worker.")
    else:
        st.warning(
            "Stock externo: disponible cuando el backend legacy esté configurado."
        )


def _recommended_test_mode_block():
    with st.container():
        st.markdown("### Modo recomendado para prueba")
        st.write("Para una primera prueba, deja las opciones por defecto y solo valida el video.")
        st.markdown(
            "- Asset Hub Bundle\n"
            "- Borrador 720p\n"
            "- Sin subtítulos\n"
            "- Sin audio propio\n"
            "- Movimiento de imágenes activado"
        )


def _initialize_form_state():
    st.session_state.setdefault("video_type_mode", "normal_assets")
    st.session_state.setdefault("job_id", _default_job_id())
    st.session_state.setdefault("video_subject", "Video Kurukin de prueba")
    st.session_state.setdefault("video_script", "Example script.")
    st.session_state.setdefault("video_aspect", "9:16")
    st.session_state.setdefault("render_quality", "draft_720p")
    st.session_state.setdefault("asset_source_mode", ASSET_SOURCE_ASSET_HUB)
    st.session_state.setdefault("asset_hub_bundle_uid", DEFAULT_BUNDLE_UID)
    st.session_state.setdefault("asset_hub_manifest_advanced", False)
    st.session_state.setdefault("asset_hub_renderer_manifest_path", "")
    st.session_state.setdefault("selected_local_assets", [])
    st.session_state.setdefault("manual_local_assets", "")
    st.session_state.setdefault("stock_source", "pexels")
    st.session_state.setdefault("audio_enabled", False)
    st.session_state.setdefault("audio_file", "")
    st.session_state.setdefault("manual_audio_file", "")
    st.session_state.setdefault("subtitles_mode", "none")
    st.session_state.setdefault("custom_subtitle_file", "")
    st.session_state.setdefault("manual_custom_subtitle_file", "")
    st.session_state.setdefault("subtitle_style_preset", "clean_center_bold_safe")
    st.session_state.setdefault("image_motion_enabled", True)
    st.session_state.setdefault("image_motion_preset", "slow_zoom_in")
    st.session_state.setdefault("image_motion_intensity", 0.06)
    st.session_state.setdefault("video_clip_duration", 4)
    st.session_state.setdefault("n_threads", 2)
    ar_defaults = build_default_aroll_broll_config()
    st.session_state.setdefault("aroll_broll_a_path", "")
    st.session_state.setdefault("aroll_broll_local_assets", "")
    st.session_state.setdefault(
        "aroll_broll_source",
        ar_defaults["b_roll"]["source"],
    )
    st.session_state.setdefault("aroll_broll_bundle_uid", "")
    st.session_state.setdefault(
        "aroll_broll_layout",
        ar_defaults["layout"]["preset"],
    )
    st.session_state.setdefault("aroll_broll_crop", ar_defaults["a_roll"]["crop"])
    st.session_state.setdefault(
        "aroll_broll_frequency",
        ar_defaults["b_roll"]["frequency"],
    )
    st.session_state.setdefault(
        "aroll_broll_clip_seconds",
        ar_defaults["b_roll"]["clip_seconds"],
    )
    st.session_state.setdefault(
        "aroll_broll_subtitles_source",
        ar_defaults["subtitles"]["source"],
    )
    st.session_state.setdefault("aroll_broll_custom_srt_path", "")
    st.session_state.setdefault("aroll_broll_quality", "draft_720p")
    st.session_state.setdefault("aroll_broll_duration_seconds", 0.0)
    st.session_state.setdefault("aroll_broll_count", 3)
    st.session_state.setdefault(
        "aroll_broll_prepare_policy",
        ASSET_SOURCE_MODE_LOCAL_ONLY,
    )
    st.session_state.setdefault("aroll_broll_prepare_query", "")
    st.session_state.setdefault("aroll_broll_prepare_desired_count", 3)
    st.session_state.setdefault("aroll_broll_prepare_local_candidates", "")
    st.session_state.setdefault("aroll_broll_prepare_bundle_uid", "")
    st.session_state.setdefault("aroll_broll_prepare_manifest_path", "")
    st.session_state.setdefault("aroll_broll_prepared_assets", {})
    st.session_state.setdefault("mpt_native_task_id", "mpt-native-ui-job")
    st.session_state.setdefault("mpt_native_video_local_path", "")
    st.session_state.setdefault("mpt_native_audio_local_path", "")
    st.session_state.setdefault("mpt_native_subject", "Kurukin MPT native render")
    st.session_state.setdefault(
        "mpt_native_script",
        "Render local-only con video y audio ya disponibles.",
    )
    st.session_state.setdefault("job_intent_mode", MODE_TOPIC_TO_VIDEO)
    st.session_state.setdefault("job_intent_task_id", "")
    st.session_state.setdefault("job_intent_topic", "")
    st.session_state.setdefault("job_intent_script", "")
    st.session_state.setdefault("job_intent_audio_path", "")
    st.session_state.setdefault("job_intent_video_path", "")
    st.session_state.setdefault("job_intent_language", "es")
    st.session_state.setdefault("job_intent_duration_seconds", 45)
    st.session_state.setdefault("job_intent_format", "vertical")
    st.session_state.setdefault("job_intent_preset", "educational")
    st.session_state.setdefault("job_intent_allow_low_relevance_visual", False)
    st.session_state.setdefault("batch_audio_folder", "")
    st.session_state.setdefault("batch_audio_paths", "")
    st.session_state.setdefault("batch_audio_topic", "")
    st.session_state.setdefault("batch_audio_task_id_prefix", "kurukin-batch-audio")
    st.session_state.setdefault("batch_audio_language", "es")
    st.session_state.setdefault("batch_audio_duration_seconds", 45)
    st.session_state.setdefault("batch_audio_format", "vertical")
    st.session_state.setdefault("batch_audio_preset", "educational")
    st.session_state.setdefault("batch_audio_max_items", 10)
    st.session_state.setdefault("intent_queue_process_max_items", 2)
    st.session_state.setdefault("intent_results_status_filter", "ALL")
    st.session_state.setdefault("intent_results_limit", 20)


def _current_manifest_path():
    bundle_uid = st.session_state.get("asset_hub_bundle_uid", "")
    if st.session_state.get("asset_hub_manifest_advanced"):
        return st.session_state.get("asset_hub_renderer_manifest_path", "")
    try:
        return default_asset_hub_manifest_path(bundle_uid)
    except ValueError:
        return ""


def _manifest_summary_block(summary):
    status = summary.get("status")
    label = _state_label(status)

    if status == "ready":
        st.success(f"{label}. Resumen del paquete de assets seleccionado.")
        total_scenes = summary.get("total_scenes", 0)
        total_assets = summary.get("total_assets", 0)
        cols = st.columns(5)
        cols[0].metric("Escenas", total_scenes)
        cols[1].metric("Assets", total_assets)
        cols[2].metric(
            "Duración aprox.",
            _format_duration(summary.get("duration_total_seconds")),
        )
        cols[3].metric("Avisos", summary.get("warnings_count", 0))
        cols[4].metric("Para revisar", summary.get("needs_human_review_count", 0))
        st.info(
            f"Este paquete está listo para validarse. Tiene {total_scenes} escenas "
            f"y {total_assets} assets."
        )
        st.caption(f"Código del paquete de assets: {summary.get('bundle_uid') or '-'}")
        asset_types = summary.get("asset_types") or {}
        if asset_types:
            st.caption(
                "Tipos detectados: "
                + ", ".join(f"{key}: {value}" for key, value in sorted(asset_types.items()))
            )
        filenames = summary.get("preview_filenames") or []
        if filenames:
            st.caption("Vista rápida: " + ", ".join(filenames))
        if summary.get("warnings_count"):
            st.warning(
                "Puedes continuar, pero revisa los avisos antes de producir el "
                "video final."
            )
        if summary.get("needs_human_review_count"):
            st.warning(
                "Hay elementos marcados para revisión. Para pruebas puedes validar, "
                "pero antes de publicar conviene revisarlos."
            )
    elif status == "not_found":
        st.warning(
            "No encontramos el paquete. Verifica que esté disponible dentro del "
            "contenedor WebUI."
        )
    elif status == "missing_path":
        st.info("Ingresa un código de paquete para derivar el manifest.")
    else:
        st.error(f"Requiere atención: {summary.get('message')}")

    st.info(
        "Nota técnica: en Asset Hub algunos materiales se resuelven al iniciar el "
        "render. Esto puede ser normal si el bundle está listo."
    )


def _operator_summary_block(operator):
    cols = st.columns(5)
    cols[0].metric("Video", operator.get("job_id") or "-")
    cols[1].metric("Calidad", operator.get("render_quality") or "-")
    cols[2].metric("Fuente", operator.get("mode") or "-")
    cols[3].metric("Subtítulos", operator.get("subtitles") or "none")
    cols[4].metric("Audio", _operator_audio_label(operator.get("audio")))
    st.caption(f"Título del video: {operator.get('subject') or '-'}")
    if operator.get("bundle_uid"):
        st.caption(f"Código del paquete de assets: {operator.get('bundle_uid')}")
    if operator.get("payload_material_count") == 0 and operator.get("mode") == "Asset Hub manifest":
        st.info(
            "Nota técnica: en Asset Hub algunos materiales se resuelven al iniciar "
            "el render. Esto puede ser normal si el bundle está listo."
        )
    elif operator.get("note"):
        st.info(operator["note"])


def _operator_audio_label(audio_value):
    if audio_value == "custom":
        return "Audio propio"
    if audio_value == "generated":
        return "Sin audio propio"
    return audio_value or "-"


def render_asset_source_step():
    st.markdown("### 1. Assets")
    source_label = st.radio(
        "Elige de dónde saldrán los materiales visuales",
        list(ASSET_SOURCE_LABELS),
        index=_index_for_value(
            ASSET_SOURCE_LABELS,
            st.session_state["asset_source_mode"],
            ASSET_SOURCE_ASSET_HUB,
        ),
        horizontal=True,
        key="asset_source_label",
    )
    st.session_state["asset_source_mode"] = ASSET_SOURCE_LABELS[source_label]
    _asset_mode_description(st.session_state["asset_source_mode"])

    manifest_summary = {"status": "missing_path", "exists": False}
    if st.session_state["asset_source_mode"] == ASSET_SOURCE_ASSET_HUB:
        st.text_input(
            "Código del paquete de assets (obligatorio)",
            key="asset_hub_bundle_uid",
            help="Identificador del bundle preparado por Asset Hub.",
        )
        derived_path = _current_manifest_path()
        if not st.session_state.get("asset_hub_manifest_advanced"):
            st.session_state["asset_hub_renderer_manifest_path"] = derived_path
        st.caption(f"Manifest derivado automáticamente: {derived_path or '-'}")

        with st.expander("Modo avanzado: editar ruta de manifest", expanded=False):
            st.checkbox(
                "Usar ruta manual de manifest",
                key="asset_hub_manifest_advanced",
            )
            if st.session_state.get("asset_hub_manifest_advanced"):
                st.text_input(
                    "renderer_manifest_path",
                    key="asset_hub_renderer_manifest_path",
                    help="Solo para diagnóstico. Debe mantenerse bajo /data/job-assets.",
                )

        manifest_summary = get_manifest_summary_for_ui(_current_manifest_path())
        _manifest_summary_block(manifest_summary)

    elif st.session_state["asset_source_mode"] == ASSET_SOURCE_LOCAL:
        st.caption(
            "Usa archivos ya presentes en el worker. No hay carga de archivos en "
            "esta fase."
        )
        local_assets = list_local_storage_files(
            DEFAULT_LOCAL_VIDEOS_DIR,
            allowed_extensions=ALLOWED_EXTENSIONS,
        )
        st.multiselect(
            "Assets disponibles en storage/local_videos",
            local_assets,
            key="selected_local_assets",
            help="El orden seleccionado se respeta al crear el trabajo.",
        )
        st.text_area(
            "Rutas relativas seguras adicionales",
            key="manual_local_assets",
            placeholder="clip-01.mp4\nstill-02.png",
            help="Solo filenames dentro de storage/local_videos. No se aceptan / ni ..",
            height=88,
        )
        st.info(
            "Estado: Listo si los archivos existen en storage/local_videos y pasan "
            "la validación de rutas seguras."
        )

    else:
        st.selectbox(
            "Proveedor de stock",
            ["pexels", "pixabay", "coverr"],
            key="stock_source",
        )
        st.warning(
            "Stock externo aparece como opción de producto, pero todavía se configura "
            "fuera de esta pantalla. Esta consola no modifica config.toml ni "
            "credenciales."
        )
    return manifest_summary


def render_content_step():
    st.markdown("### 2. Contenido")
    left, right = st.columns([1, 1])
    with left:
        st.text_input(
            "Título del video (opcional pero recomendado)",
            key="video_subject",
        )
    with right:
        with st.expander("Opciones avanzadas del trabajo", expanded=False):
            st.text_input(
                "Identificador del video",
                key="job_id",
                help=(
                    "Se genera automáticamente. Solo cámbialo si necesitas un "
                    "nombre específico para pruebas."
                ),
            )
            st.caption("Identificador interno del trabajo, generado automáticamente.")
    st.text_area(
        "Guion o descripción (opcional para este flujo si el bundle ya tiene escenas)",
        key="video_script",
        height=160,
        help="Texto base para voz, subtítulos o contexto del render.",
    )


def _manual_lines(value):
    return [line.strip() for line in str(value or "").splitlines() if line.strip()]


def _selected_local_assets():
    selected = list(st.session_state.get("selected_local_assets") or [])
    for item in _manual_lines(st.session_state.get("manual_local_assets")):
        safe = safe_relative_path(
            item,
            allowed_extensions=ALLOWED_EXTENSIONS,
            label="asset",
        )
        if safe not in selected:
            selected.append(safe)
    return selected


def render_audio_subtitles_step():
    st.markdown("### 3. Audio y subtítulos")
    st.info(
        "Puedes usar audio propio y subtítulos mejorados en todos los modos "
        "compatibles."
    )

    left, right = st.columns([1, 1])
    with left:
        st.checkbox("Usar audio propio", key="audio_enabled")
        audio_files = list_local_storage_files(
            DEFAULT_LOCAL_AUDIOS_DIR,
            allowed_extensions=ALLOWED_AUDIO_EXTENSIONS,
        )
        if st.session_state.get("audio_enabled"):
            st.selectbox(
                "Audio en storage/local_audios",
                [""] + audio_files,
                key="audio_file",
                format_func=lambda value: value or "Elegir audio",
            )
            st.text_input(
                "Audio relativo seguro",
                key="manual_audio_file",
                placeholder="audio-prueba.mp3",
                help="Solo filenames dentro de storage/local_audios.",
            )
        else:
            st.session_state["audio_file"] = ""
            st.session_state["manual_audio_file"] = ""

    with right:
        subtitle_label = st.selectbox(
            "Subtítulos",
            list(SUBTITLE_MODE_LABELS),
            index=_index_for_value(
                SUBTITLE_MODE_LABELS,
                st.session_state["subtitles_mode"],
                "none",
            ),
            key="subtitles_mode_label",
        )
        st.session_state["subtitles_mode"] = SUBTITLE_MODE_LABELS[subtitle_label]
        subtitle_files = list_local_storage_files(
            DEFAULT_LOCAL_SUBTITLES_DIR,
            allowed_extensions=ALLOWED_SUBTITLE_EXTENSIONS,
        )
        if st.session_state["subtitles_mode"] == "custom_srt":
            st.selectbox(
                "SRT en storage/local_subtitles",
                [""] + subtitle_files,
                key="custom_subtitle_file",
                format_func=lambda value: value or "Elegir SRT",
            )
            st.text_input(
                "SRT relativo seguro",
                key="manual_custom_subtitle_file",
                placeholder="subtitulos.srt",
            )
        else:
            st.session_state["custom_subtitle_file"] = ""
            st.session_state["manual_custom_subtitle_file"] = ""

    st.selectbox(
        "Estilo de subtítulos",
        SUBTITLE_STYLE_PRESETS,
        key="subtitle_style_preset",
    )


def render_quality_style_step():
    st.markdown("### 4. Calidad y estilo")
    left, right = st.columns([1, 1])
    with left:
        quality_label = st.selectbox(
            "Calidad del video",
            list(QUALITY_LABELS),
            index=_index_for_value(
                QUALITY_LABELS,
                st.session_state["render_quality"],
                "draft_720p",
            ),
            key="render_quality_label",
        )
        st.session_state["render_quality"] = QUALITY_LABELS[quality_label]
        st.selectbox("Formato del video", ["9:16", "16:9"], key="video_aspect")
        st.number_input(
            "Duración por visual",
            min_value=1,
            max_value=20,
            step=1,
            key="video_clip_duration",
        )
    with right:
        st.checkbox("Activar movimiento de imágenes", key="image_motion_enabled")
        st.selectbox(
            "Movimiento de imágenes",
            MOTION_PROFILES,
            key="image_motion_preset",
        )
        st.slider(
            "Intensidad de movimiento",
            min_value=0.0,
            max_value=0.20,
            step=0.01,
            key="image_motion_intensity",
        )
        with st.expander("Modo avanzado: rendimiento", expanded=False):
            st.number_input("Threads", min_value=1, max_value=4, step=1, key="n_threads")


def _effective_audio_file():
    if not st.session_state.get("audio_enabled"):
        return ""
    manual = st.session_state.get("manual_audio_file", "").strip()
    if manual:
        return safe_relative_path(
            manual,
            allowed_extensions=ALLOWED_AUDIO_EXTENSIONS,
            label="audio",
        )
    return st.session_state.get("audio_file", "")


def _effective_subtitle_file():
    if st.session_state.get("subtitles_mode") != "custom_srt":
        return ""
    manual = st.session_state.get("manual_custom_subtitle_file", "").strip()
    if manual:
        return safe_relative_path(
            manual,
            allowed_extensions=ALLOWED_SUBTITLE_EXTENSIONS,
            label="subtitle",
        )
    return st.session_state.get("custom_subtitle_file", "")


def _build_form_spec():
    return build_render_console_spec(
        job_id=st.session_state["job_id"],
        video_subject=st.session_state["video_subject"],
        video_script=st.session_state["video_script"],
        render_quality=st.session_state["render_quality"],
        video_aspect=st.session_state["video_aspect"],
        asset_source_mode=st.session_state["asset_source_mode"],
        asset_hub_bundle_uid=st.session_state["asset_hub_bundle_uid"],
        asset_hub_renderer_manifest_path=_current_manifest_path(),
        selected_local_assets=_selected_local_assets(),
        stock_source=st.session_state["stock_source"],
        audio_file=_effective_audio_file(),
        subtitles_mode=st.session_state["subtitles_mode"],
        custom_subtitle_file=_effective_subtitle_file(),
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


def render_human_summary(payload, manifest_summary):
    operator = build_operator_summary(payload, manifest_summary)
    st.markdown("**Resumen antes de encolar**")
    _operator_summary_block(operator)
    if manifest_summary.get("status") == "ready":
        st.caption("Paquete Asset Hub detectado y legible.")
    elif payload.get("asset_hub_renderer_manifest_path"):
        st.warning("El paquete no está listo o no se pudo leer desde esta pantalla.")


def _show_validation_result(spec, payload, manifest_summary, operator):
    st.success("Video validado. Está listo para enviarse a cola.")
    render_human_summary(payload, manifest_summary)
    with st.expander("Modo avanzado: ver payload JSON", expanded=False):
        st.json(payload)
    with st.expander("Diagnóstico: ver Kurukin Job Spec", expanded=False):
        st.json(spec)


def render_validate_enqueue_step(manifest_summary):
    st.markdown("### 5. Revisión")
    st.info(
        "Primero valida. Después envía a cola.\n\n"
        "Validar video: revisa que el trabajo esté completo. No crea pending job.\n\n"
        "Enviar a cola: crea un pending job. No renderiza inmediatamente."
    )

    validate_clicked = st.button("Validar video", key="form_validate")

    if validate_clicked:
        try:
            _validate_form_payload()
        except Exception as exc:
            _show_error(exc)

    if st.session_state.get("form_validated_payload"):
        _show_validation_result(
            st.session_state["form_validated_spec"],
            st.session_state["form_validated_payload"],
            st.session_state["form_manifest_summary"],
            st.session_state["form_operator_summary"],
        )
        if st.button("Enviar a cola", key="form_enqueue"):
            try:
                path = enqueue_moneyprinter_payload(
                    st.session_state["form_validated_payload"]
                )
                job_id = st.session_state["form_validated_payload"].get("job_id") or ""
                st.session_state["last_enqueued_job_id"] = str(job_id)
                st.session_state["last_enqueued_pending_path"] = path.as_posix()
                st.session_state["last_enqueued_at"] = datetime.now(
                    timezone.utc
                ).isoformat()
                st.success("Video enviado a cola.")
                st.info(
                    "El render no empezó todavía. Puedes revisar el estado en la "
                    "pestaña Cola."
                )
                st.caption("Abre la pestaña Cola y presiona Actualizar estado.")
                st.caption("Enviar a cola no renderiza inmediatamente.")
                st.caption(f"Pendiente creado: {path}")
            except Exception as exc:
                _show_error(exc)
    else:
        st.button(
            "Enviar a cola",
            key="form_enqueue_disabled",
            disabled=True,
            help="Primero valida el video para activar este paso.",
        )
        st.warning("Enviar a cola se activa después de una validación exitosa.")
        st.caption("Valida el video para ver el resumen y el JSON avanzado.")
        with st.expander("Modo avanzado: ver payload JSON", expanded=False):
            st.caption("El payload aparecerá aquí después de validar el video.")


def _mpt_native_expected_output_video(task_id):
    clean_task_id = str(task_id or "").strip()
    if not clean_task_id:
        return None
    expected_relative_path = f"{clean_task_id}/final-1.mp4"
    for video in list_rendered_videos():
        if video.get("relative_path") == expected_relative_path:
            return video
    return None


def render_mpt_native_local_submit_step():
    st.markdown("### Motor MPT nativo")
    st.caption(
        "Envía un job local-only directo al motor MPT nativo; no crea pending "
        "ni usa runner ni proveedores externos."
    )
    enabled = is_mpt_engine_submit_enabled()
    disabled_warning = (
        "Submit real MPT desactivado. Activa "
        f"{MPT_ENGINE_SUBMIT_FLAG}=1 para ejecutar."
    )
    if not enabled:
        st.warning(disabled_warning)

    left, right = st.columns([1, 1])
    with left:
        st.text_input("task_id", key="mpt_native_task_id")
        st.text_input(
            "video local path",
            key="mpt_native_video_local_path",
            placeholder="storage/local_videos/...",
        )
        st.text_input(
            "audio local path",
            key="mpt_native_audio_local_path",
            placeholder="storage/local_audios/...",
        )
    with right:
        st.text_input("subject", key="mpt_native_subject")
        st.text_area("script", key="mpt_native_script", height=108)

    task_id = st.session_state.get("mpt_native_task_id", "")
    expected_output = (
        f"storage/tasks/{task_id}/final-1.mp4" if task_id else "storage/tasks/<task_id>/final-1.mp4"
    )
    st.caption(f"Output esperado: {expected_output}")

    if st.button(
        "Enviar local-only a MPT",
        key="mpt_native_local_submit",
    ):
        if not enabled:
            st.session_state["mpt_native_last_submit_result"] = {
                "ok": False,
                "task_id": task_id,
                "expected_output": expected_output,
                "errors": [disabled_warning],
            }
            st.warning(disabled_warning)
        else:
            result = submit_mpt_native_local_job_from_console(
                task_id=task_id,
                video_local_path=st.session_state.get("mpt_native_video_local_path", ""),
                audio_local_path=st.session_state.get("mpt_native_audio_local_path", ""),
                video_subject=st.session_state.get("mpt_native_subject", ""),
                video_script=st.session_state.get("mpt_native_script", ""),
            )
            st.session_state["mpt_native_last_submit_result"] = result
            if result.get("ok"):
                st.success("Job enviado al motor MPT nativo.")
                st.caption(f"task_id: {result.get('task_id')}")
                st.caption(f"Output esperado: {result.get('expected_output')}")
            else:
                st.error("No se pudo enviar el job al motor MPT nativo.")
                st.caption(f"task_id: {result.get('task_id') or task_id or '-'}")
                st.caption(f"Output esperado: {result.get('expected_output') or expected_output}")
                st.json({"errors": result.get("errors", [])})

    output_video = _mpt_native_expected_output_video(task_id)
    if output_video:
        st.success(f"Output existe: {expected_output}")
        _result_preview_download(output_video, key_prefix="mpt_native_expected_output")
    else:
        st.info("Output pendiente/no encontrado todavía")

    if st.session_state.get("mpt_native_last_submit_result"):
        with st.expander("Resultado MPT nativo", expanded=False):
            st.json(st.session_state["mpt_native_last_submit_result"])


def _build_job_intent_from_state():
    return {
        "mode": st.session_state.get("job_intent_mode", MODE_TOPIC_TO_VIDEO),
        "task_id": st.session_state.get("job_intent_task_id", ""),
        "topic": st.session_state.get("job_intent_topic", ""),
        "script": st.session_state.get("job_intent_script", ""),
        "audio_path": st.session_state.get("job_intent_audio_path", ""),
        "video_path": st.session_state.get("job_intent_video_path", ""),
        "language": st.session_state.get("job_intent_language", "es"),
        "duration_seconds": int(
            st.session_state.get("job_intent_duration_seconds", 45)
        ),
        "format": st.session_state.get("job_intent_format", "vertical"),
        "preset": st.session_state.get("job_intent_preset", "educational"),
        "allow_low_relevance_visual": bool(
            st.session_state.get("job_intent_allow_low_relevance_visual", False)
        ),
    }


def _batch_audio_paths_from_state():
    value = st.session_state.get("batch_audio_paths", "")
    return [
        line.strip()
        for line in str(value or "").replace(",", "\n").splitlines()
        if line.strip()
    ]


def _job_intent_result_block(result):
    status = result.get("status") or "-"
    if status == STATUS_READY_TO_SUBMIT:
        st.success("Intención lista para preparar/enviar con insumos locales.")
    elif result.get("errors"):
        st.error("La intención requiere ajustes.")
    else:
        st.warning(f"Estado de intención: {status}")
    if result.get("reasons"):
        st.caption("Pendiente: " + ", ".join(result.get("reasons") or []))
    if result.get("errors"):
        st.json({"errors": result.get("errors")})


def _topic_plan_block(intent, *, key_prefix):
    plan = (intent or {}).get("topic_plan") or {}
    if not plan:
        return

    st.caption(
        "Topic-to-video v1 genera plan y script local. Si agregas audio_path local, "
        "puede quedar READY_TO_SUBMIT sin TTS."
    )
    script = plan.get("script") or (intent or {}).get("script", "")
    if script:
        st.text_area(
            "script generado",
            value=script,
            height=150,
            key=f"{key_prefix}_topic_plan_script",
        )
    scenes = plan.get("scenes") or (intent or {}).get("scenes") or []
    if scenes:
        with st.expander("Escenas generadas", expanded=False):
            st.json(scenes)
    visual_keywords = (
        plan.get("visual_keywords")
        or (intent or {}).get("visual_keywords")
        or []
    )
    if visual_keywords:
        st.caption("visual_keywords: " + ", ".join(str(item) for item in visual_keywords))


def render_job_intent_step():
    st.markdown("### Crear por intención")
    st.caption(
        "Convierte una intención simple a una spec MPT local-only; no llama OpenAI, "
        "TTS, stock externo, Asset Hub API ni runner."
    )

    left, right = st.columns([1, 1])
    with left:
        mode_label = st.selectbox(
            "mode",
            list(JOB_INTENT_MODE_LABELS),
            index=_index_for_value(
                JOB_INTENT_MODE_LABELS,
                st.session_state.get("job_intent_mode", MODE_TOPIC_TO_VIDEO),
                MODE_TOPIC_TO_VIDEO,
            ),
            key="job_intent_mode_label",
        )
        st.session_state["job_intent_mode"] = JOB_INTENT_MODE_LABELS[mode_label]
        st.text_input("task_id opcional", key="job_intent_task_id")
        st.text_input("topic", key="job_intent_topic")
        st.text_area("script opcional", key="job_intent_script", height=96)
        st.text_input(
            "audio_path local",
            key="job_intent_audio_path",
            placeholder="storage/local_audios/audio.mp3",
        )
        st.caption("Tema a video acepta audio_path local; no se genera TTS.")
    with right:
        st.text_input(
            "video_path (opcional para topic_to_video/audio_to_video)",
            key="job_intent_video_path",
            placeholder="storage/local_videos/visual.mp4",
        )
        st.caption(
            "Si no pasas video_path, Kurukin intentará usar un visual local automático."
        )
        st.checkbox(
            "Permitir visual local de baja relevancia",
            key="job_intent_allow_low_relevance_visual",
        )
        st.text_input("language", key="job_intent_language")
        st.number_input(
            "duration_seconds",
            min_value=4,
            max_value=300,
            step=1,
            key="job_intent_duration_seconds",
        )
        st.selectbox("format", JOB_INTENT_FORMATS, key="job_intent_format")
        st.text_input("preset", key="job_intent_preset")

    action_cols = st.columns([1, 1, 1, 1])
    with action_cols[0]:
        if st.button("Validar intención", key="job_intent_validate"):
            st.session_state["job_intent_last_validation"] = validate_job_intent(
                _build_job_intent_from_state()
            )
    with action_cols[1]:
        if st.button("Preparar spec MPT", key="job_intent_prepare_mpt_spec"):
            st.session_state["job_intent_last_compile"] = compile_job_intent_to_mpt_spec(
                _build_job_intent_from_state()
            )
    with action_cols[2]:
        if st.button("Agregar a cola", key="job_intent_enqueue"):
            result = enqueue_job_intent_from_console(_build_job_intent_from_state())
            st.session_state["job_intent_last_queue_result"] = result
            st.session_state["job_intent_last_compile"] = result.get("compiled") or {}
            if result.get("ok"):
                st.session_state["last_enqueued_job_id"] = str(result.get("job_id") or "")
                st.session_state["last_enqueued_pending_path"] = str(
                    result.get("pending_path") or ""
                )
                st.session_state["last_enqueued_at"] = datetime.now(
                    timezone.utc
                ).isoformat()
                st.success("Intención agregada a cola.")
                st.caption(f"task_id: {result.get('task_id')}")
                st.caption(f"status: {result.get('status')}")
            else:
                st.warning(f"No se encoló la intención: {result.get('status')}")
                if result.get("reasons"):
                    st.caption("Pendiente: " + ", ".join(result.get("reasons") or []))
    compiled = st.session_state.get("job_intent_last_compile") or {}
    ready_to_submit = compiled.get("status") == STATUS_READY_TO_SUBMIT
    submit_enabled = is_mpt_engine_submit_enabled()
    with action_cols[3]:
        if st.button(
            "Enviar a MPT nativo",
            key="job_intent_submit_mpt_native",
            disabled=not (ready_to_submit and submit_enabled),
            help=(
                "Requiere READY_TO_SUBMIT y "
                f"{MPT_ENGINE_SUBMIT_FLAG}=1."
            ),
        ):
            intent = compiled.get("intent") or _build_job_intent_from_state()
            result = submit_mpt_native_local_job_from_console(
                task_id=intent.get("task_id") or _default_job_id(),
                video_local_path=intent.get("video_path", ""),
                audio_local_path=intent.get("audio_path", ""),
                video_subject=intent.get("topic") or intent.get("script", ""),
                video_script=intent.get("script", ""),
            )
            st.session_state["job_intent_last_submit_result"] = result
            if result.get("ok"):
                st.success("Intención enviada al motor MPT nativo.")
            else:
                st.error("No se pudo enviar la intención al motor MPT nativo.")

    validation = st.session_state.get("job_intent_last_validation")
    if validation:
        with st.expander("Resultado de intención", expanded=False):
            _job_intent_result_block(validation)
            validation_intent = validation.get("intent") or {}
            _topic_plan_block(validation_intent, key_prefix="validation")
            st.json(validation_intent)

    if compiled:
        _job_intent_result_block(compiled)
        compiled_intent = compiled.get("intent") or {}
        _topic_plan_block(compiled_intent, key_prefix="compiled")
        resolved_visual = (
            compiled_intent.get("resolved_visual_path")
            or compiled_intent.get("video_path", "")
        )
        if resolved_visual:
            st.caption(f"Visual local resuelto: {resolved_visual}")
        visual_source = compiled_intent.get("visual_autofill_source", "")
        if visual_source:
            st.caption(f"Source visual: {visual_source}")
        relevance_confidence = compiled_intent.get("visual_relevance_confidence", "")
        if relevance_confidence:
            st.caption(
                "Visual relevance: "
                f"{compiled_intent.get('visual_relevance_score', 0)} "
                f"({relevance_confidence})"
            )
            st.caption(
                "Visual relevance reason: "
                f"{compiled_intent.get('visual_relevance_reason', '-')}"
            )
        if "needs_relevant_local_visual_asset" in (compiled.get("reasons") or []):
            st.warning(
                "No encontré un visual local suficientemente relacionado. "
                "Agrega video_path/visual_path o permite fallback."
            )
        with st.expander("Spec MPT preparada por intención", expanded=False):
            st.json(compiled.get("mpt_spec") or {})
        if ready_to_submit and not submit_enabled:
            st.warning(
                "READY_TO_SUBMIT detectado, pero el envío real requiere "
                f"{MPT_ENGINE_SUBMIT_FLAG}=1."
            )
    elif not submit_enabled:
        st.caption(f"Enviar a MPT nativo bloqueado por {MPT_ENGINE_SUBMIT_FLAG}.")

    submit_result = st.session_state.get("job_intent_last_submit_result")
    if submit_result:
        with st.expander("Resultado submit por intención", expanded=False):
            st.json(submit_result)

    queue_result = st.session_state.get("job_intent_last_queue_result")
    if queue_result:
        with st.expander("Resultado cola por intención", expanded=False):
            st.json(queue_result)


def render_batch_audio_intent_step():
    st.markdown("### Crear lote desde audios")
    left, right = st.columns([1, 1])
    with left:
        st.text_input(
            "audio_folder",
            key="batch_audio_folder",
            placeholder="storage/local_audios",
        )
        st.text_area(
            "audio_paths",
            key="batch_audio_paths",
            height=96,
            placeholder="storage/local_audios/audio-1.mp3\nstorage/local_audios/audio-2.wav",
        )
        st.text_input("topic lote", key="batch_audio_topic")
        st.text_input("task_id_prefix", key="batch_audio_task_id_prefix")
    with right:
        st.text_input("language lote", key="batch_audio_language")
        st.number_input(
            "duration_seconds lote",
            min_value=4,
            max_value=300,
            step=1,
            key="batch_audio_duration_seconds",
        )
        st.selectbox("format lote", JOB_INTENT_FORMATS, key="batch_audio_format")
        st.text_input("preset lote", key="batch_audio_preset")
        st.number_input(
            "max_items",
            min_value=1,
            max_value=10,
            step=1,
            key="batch_audio_max_items",
        )

    if st.button("Agregar lote a cola", key="batch_audio_enqueue"):
        result = enqueue_audio_batch_intents(
            audio_folder=st.session_state.get("batch_audio_folder", ""),
            audio_paths=_batch_audio_paths_from_state(),
            topic=st.session_state.get("batch_audio_topic", ""),
            task_id_prefix=st.session_state.get("batch_audio_task_id_prefix", ""),
            language=st.session_state.get("batch_audio_language", "es"),
            duration_seconds=int(
                st.session_state.get("batch_audio_duration_seconds", 45)
            ),
            format=st.session_state.get("batch_audio_format", "vertical"),
            preset=st.session_state.get("batch_audio_preset", "educational"),
            max_items=int(st.session_state.get("batch_audio_max_items", 10)),
        )
        st.session_state["batch_audio_last_queue_result"] = result
        if result.get("created"):
            first_item = (result.get("items") or [{}])[0]
            st.session_state["last_enqueued_job_id"] = str(
                first_item.get("task_id") or ""
            )
            st.session_state["last_enqueued_pending_path"] = str(
                first_item.get("queue_item_path") or ""
            )
            st.session_state["last_enqueued_at"] = datetime.now(
                timezone.utc
            ).isoformat()
            st.success(f"Lote agregado a cola: {result.get('created')} creado(s).")
        else:
            st.warning("No se creó ningún item de lote.")

    batch_result = st.session_state.get("batch_audio_last_queue_result")
    if batch_result:
        st.caption(
            f"created: {batch_result.get('created', 0)} | "
            f"skipped: {batch_result.get('skipped', 0)}"
        )
        items = batch_result.get("items") or []
        if items:
            st.dataframe(
                [
                    {
                        "task_id": item.get("task_id", ""),
                        "status": item.get("status", ""),
                        "audio_path": item.get("audio_path", ""),
                        "resolved_visual_path": item.get("resolved_visual_path", ""),
                        "queue_item_path": item.get("queue_item_path", ""),
                    }
                    for item in items
                ],
                use_container_width=True,
                hide_index=True,
            )
        if batch_result.get("errors"):
            with st.expander("Errores lote", expanded=False):
                st.json(batch_result.get("errors") or [])


def _current_aroll_broll_config():
    config = build_default_aroll_broll_config()
    prepared = st.session_state.get("aroll_broll_prepared_assets") or {}
    prepared_assets = prepared.get("b_roll_assets") if prepared.get("ok") else []
    config["a_roll"]["path"] = st.session_state.get("aroll_broll_a_path", "")
    config["a_roll"]["crop"] = st.session_state.get(
        "aroll_broll_crop",
        SPEAKER_CROP_CENTER,
    )
    config["b_roll"]["source"] = st.session_state.get(
        "aroll_broll_source",
        BROLL_SOURCE_ASSET_HUB_MANIFEST,
    )
    if prepared_assets:
        config["b_roll"]["source"] = BROLL_SOURCE_LOCAL_ASSETS
        config["b_roll"]["assets"] = normalize_aroll_broll_local_asset_paths(
            prepared_assets
        )
    elif config["b_roll"]["source"] == BROLL_SOURCE_LOCAL_ASSETS:
        config["b_roll"]["assets"] = normalize_aroll_broll_local_asset_paths(
            st.session_state.get("aroll_broll_local_assets", "")
        )
    config["b_roll"]["bundle_uid"] = st.session_state.get(
        "aroll_broll_bundle_uid",
        "",
    )
    config["b_roll"]["clip_seconds"] = int(
        st.session_state.get("aroll_broll_clip_seconds", 4)
    )
    config["b_roll"]["frequency"] = st.session_state.get(
        "aroll_broll_frequency",
        "medium",
    )
    config["layout"]["preset"] = st.session_state.get(
        "aroll_broll_layout",
        LAYOUT_ALTERNATING_FULLSCREEN,
    )
    config["subtitles"]["source"] = st.session_state.get(
        "aroll_broll_subtitles_source",
        SUBTITLES_SOURCE_NONE,
    )
    config["subtitles"]["custom_srt_path"] = st.session_state.get(
        "aroll_broll_custom_srt_path",
        "",
    )
    return config


def _aroll_broll_summary_block(config):
    summary = summarize_aroll_broll_config(config)
    cols = st.columns(5)
    cols[0].metric("Audio", summary["audio"])
    cols[1].metric("B-roll", summary["b-roll"])
    cols[2].metric("Subtítulos", summary["subtitles"])
    cols[3].metric("Layout", summary["layout"])
    cols[4].metric("Crop", summary["crop"])
    st.caption(summary["renderer"])
    st.caption(summary["asset_policy"])
    asset_count = len(config.get("b_roll", {}).get("assets") or [])
    if asset_count:
        st.caption(f"B-roll assets: {asset_count}")


def _aroll_broll_manifest_assets_block(config):
    b_roll = config.get("b_roll", {})
    if b_roll.get("source") != BROLL_SOURCE_ASSET_HUB_MANIFEST:
        return
    manifest_path = b_roll.get("manifest_path")
    if not manifest_path:
        return
    try:
        result = extract_broll_assets_from_manifest(
            manifest_path,
            project_root=ROOT_DIR,
        )
    except (ArollBrollRendererError, OSError, json.JSONDecodeError):
        return
    if result.get("assets"):
        st.caption(f"B-roll assets: {len(result['assets'])}")
    for warning in result.get("warnings", [])[:2]:
        st.caption(f"Manifest read-only: {warning}")


def _aroll_broll_validation_block(result):
    if result["ok"]:
        st.success("A-roll / B-roll validado para foundation.")
    else:
        st.error("Revisa los errores antes de continuar.")
    for error in result.get("errors", []):
        st.error(error)
    for warning in result.get("warnings", []):
        st.warning(warning)
    with st.expander("Diagnóstico A-roll / B-roll", expanded=False):
        st.json(result["normalized"])


def _aroll_broll_timeline_block(config):
    duration = float(st.session_state.get("aroll_broll_duration_seconds", 0.0) or 0.0)
    if duration <= 0:
        st.info(
            "Ingresa una duración manual del A-roll si quieres ver un timeline conceptual."
        )
        return
    timeline = build_aroll_broll_preview_timeline(
        duration,
        int(st.session_state.get("aroll_broll_count", 0)),
        int(config["b_roll"]["clip_seconds"]),
        config["b_roll"]["frequency"],
        config["layout"]["preset"],
    )
    st.markdown("### Timeline conceptual")
    st.dataframe(timeline, use_container_width=True, hide_index=True)


def _build_aroll_broll_queue_payload(config):
    return build_aroll_broll_payload_from_console(
        config,
        job_id=st.session_state["job_id"],
        project_root=ROOT_DIR,
        render_quality=st.session_state.get("aroll_broll_quality", "draft_720p"),
        title="A-roll/B-roll",
        task_id=st.session_state["job_id"],
        created_by=st.session_state.get("aroll_broll_created_by", "render_console_ui"),
    )


def _aroll_broll_queue_flag_label():
    value = os.environ.get(AROLL_BROLL_QUEUE_FLAG)
    if value is None:
        return f"{AROLL_BROLL_QUEUE_FLAG}=<unset>"
    return f"{AROLL_BROLL_QUEUE_FLAG}={value}"


def _pexels_source_flag_label():
    value = os.environ.get(PEXELS_SOURCE_FLAG)
    if value is None:
        return f"{PEXELS_SOURCE_FLAG}=<unset>"
    return f"{PEXELS_SOURCE_FLAG}={value}"


def _current_prepare_broll_policy():
    mode = st.session_state.get(
        "aroll_broll_prepare_policy",
        ASSET_SOURCE_MODE_LOCAL_ONLY,
    )
    policy = {"mode": mode}
    if mode == ASSET_SOURCE_MODE_EXCLUSIVE_BRAND_ASSETS:
        policy["brand_asset_bundle_uid"] = st.session_state.get(
            "aroll_broll_prepare_bundle_uid",
            "",
        )
        policy["manifest_path"] = st.session_state.get(
            "aroll_broll_prepare_manifest_path",
            "",
        )
    return policy


def _prepared_broll_result_block(result):
    if not result:
        return
    if result.get("ok"):
        st.success(result.get("message") or "B-roll assets preparados")
    else:
        st.error(result.get("error") or "No se pudieron preparar assets B-roll.")
    cols = st.columns(3)
    cols[0].metric("B-roll assets", int(result.get("b_roll_asset_count") or 0))
    cols[1].metric("Fuente", result.get("source_provider") or "-")
    cols[2].metric("Policy", result.get("asset_policy_label") or "-")
    if result.get("query"):
        st.caption(f"Query: {result['query']}")
    assets = result.get("b_roll_assets") or []
    if assets:
        st.caption("Paths locales preparados")
        st.code("\n".join(assets), language="text")


def _prepare_broll_assets_block():
    st.markdown("### Preparar B-roll")
    st.caption(
        "Materializa una lista local de assets; no encola, no renderiza y no ejecuta runner."
    )
    st.info(
        "Pexels source: disponible solo con integración controlada/flag; no se usa por defecto."
    )
    st.caption("Flag Pexels: KURUKIN_ENABLE_PEXELS_SOURCE")
    st.caption(_pexels_source_flag_label())
    cols = st.columns([1, 1, 1])
    with cols[0]:
        policy_label = st.selectbox(
            "Asset policy",
            list(AROLL_BROLL_PREPARE_POLICY_LABELS),
            index=_index_for_value(
                AROLL_BROLL_PREPARE_POLICY_LABELS,
                st.session_state.get(
                    "aroll_broll_prepare_policy",
                    ASSET_SOURCE_MODE_LOCAL_ONLY,
                ),
                ASSET_SOURCE_MODE_LOCAL_ONLY,
            ),
            key="aroll_broll_prepare_policy_label",
        )
        st.session_state["aroll_broll_prepare_policy"] = (
            AROLL_BROLL_PREPARE_POLICY_LABELS[policy_label]
        )
    with cols[1]:
        st.text_input(
            "Query / tema visual",
            key="aroll_broll_prepare_query",
            placeholder="ciudad, producto, lifestyle...",
        )
    with cols[2]:
        st.number_input(
            "Desired count",
            min_value=1,
            max_value=8,
            step=1,
            key="aroll_broll_prepare_desired_count",
        )

    if (
        st.session_state.get("aroll_broll_prepare_policy")
        == ASSET_SOURCE_MODE_EXCLUSIVE_BRAND_ASSETS
    ):
        brand_cols = st.columns([1, 1])
        with brand_cols[0]:
            st.text_input(
                "Brand bundle UID",
                key="aroll_broll_prepare_bundle_uid",
                placeholder="jab_...",
            )
        with brand_cols[1]:
            st.text_input(
                "Manifest path local",
                key="aroll_broll_prepare_manifest_path",
                placeholder="/data/job-assets/<uid>/manifests/renderer-manifest.json",
            )

    st.text_area(
        "Local candidates",
        key="aroll_broll_prepare_local_candidates",
        placeholder=(
            "storage/local_videos/cutaway.mp4\n"
            "storage/local_assets/visual.mp4"
        ),
        help="Un path por línea bajo storage/local_videos, storage/local_assets o storage/local_images.",
        height=84,
    )

    if st.button("Preparar B-roll", key="aroll_broll_prepare_assets"):
        pexels_downloader = None
        if is_pexels_source_enabled() and (
            st.session_state.get("aroll_broll_prepare_policy")
            == ASSET_SOURCE_MODE_OPEN_SOURCES
        ):

            def pexels_downloader(request_data):
                return create_pexels_downloader()(request_data)

        result = prepare_broll_assets_from_console(
            project_root=Path(ROOT_DIR),
            asset_policy=_current_prepare_broll_policy(),
            query=st.session_state.get("aroll_broll_prepare_query", ""),
            desired_count=int(
                st.session_state.get("aroll_broll_prepare_desired_count", 1)
            ),
            local_candidates=st.session_state.get(
                "aroll_broll_prepare_local_candidates",
                "",
            ),
            pexels_downloader=pexels_downloader,
            environ=dict(os.environ),
        )
        st.session_state["aroll_broll_prepared_assets"] = result
        if result.get("ok"):
            st.session_state["aroll_broll_source"] = BROLL_SOURCE_LOCAL_ASSETS
            st.session_state["aroll_broll_local_assets"] = "\n".join(
                result.get("b_roll_assets") or []
            )
            st.session_state.pop("aroll_broll_validation", None)

    _prepared_broll_result_block(
        st.session_state.get("aroll_broll_prepared_assets") or {}
    )


def _aroll_broll_view():
    st.markdown("### Modo Presentador + B-roll")
    queue_enabled = is_aroll_broll_queue_enabled()
    st.info(
        "El audio del presentador manda.\n\n"
        "El B-roll se usa como apoyo visual y se silencia por defecto.\n\n"
        "Los subtítulos pueden generarse desde el audio del presentador, usar SRT propio o desactivarse."
    )
    st.caption("A-roll / B-roll")
    st.caption("B-roll muted")
    st.caption("alternating_fullscreen")
    st.caption("Renderer preparado: alternating_fullscreen")
    st.caption("Cola A-roll/B-roll: protegida")
    st.caption(_aroll_broll_queue_flag_label())
    st.caption(
        "Activa KURUKIN_ENABLE_AROLL_BROLL_QUEUE=1 solo para pruebas controladas."
    )
    st.info(
        "Renderer MVP planeado: alternating_fullscreen\n\n"
        "Audio final: A-roll original\n\n"
        "B-roll audio: muted\n\n"
        "La cola puede preparar un pending job protegido; el runner lo rechaza "
        "hasta habilitar la integración E2E."
    )

    _prepare_broll_assets_block()

    left, right = st.columns([1, 1])
    with left:
        st.text_input(
            "Identificador del video",
            key="job_id",
            help=(
                "Se usa también como task_id para pruebas controladas "
                "A-roll/B-roll."
            ),
        )
        st.text_input(
            "Ruta del video A-roll local",
            key="aroll_broll_a_path",
            placeholder="storage/local_videos/presentador.mp4",
        )
        source_label = st.radio(
            "Fuente B-roll",
            list(AROLL_BROLL_SOURCE_LABELS),
            index=_index_for_value(
                AROLL_BROLL_SOURCE_LABELS,
                st.session_state["aroll_broll_source"],
                BROLL_SOURCE_ASSET_HUB_MANIFEST,
            ),
            horizontal=True,
            key="aroll_broll_source_label",
        )
        st.session_state["aroll_broll_source"] = AROLL_BROLL_SOURCE_LABELS[source_label]
        prepared_assets = (
            st.session_state.get("aroll_broll_prepared_assets") or {}
        ).get("b_roll_assets")
        if (
            st.session_state["aroll_broll_source"] == BROLL_SOURCE_ASSET_HUB_MANIFEST
            and not prepared_assets
        ):
            st.text_input("Bundle UID", key="aroll_broll_bundle_uid")
        else:
            if prepared_assets:
                st.caption(
                    "Usando assets preparados como B-roll local para el siguiente paso."
                )
            st.info(
                "Usa uno o varios paths B-roll locales (1..8), una ruta por línea."
            )
            st.text_area(
                "Uno o varios paths B-roll locales",
                key="aroll_broll_local_assets",
                placeholder=(
                    "storage/local_videos/cutaway.mp4\n"
                    "storage/local_assets/visual.mp4"
                ),
                help=(
                    "Entre 1 y 8 rutas, una por línea, bajo storage/local_videos, "
                    "storage/local_assets o storage/local_images. Los duplicados "
                    "exactos se ignoran."
                ),
                height=88,
            )
            local_asset_count = len(
                normalize_broll_asset_values(
                    st.session_state.get("aroll_broll_local_assets", "")
                )
            )
            st.caption(f"B-roll assets: {local_asset_count}")
        st.selectbox("Layout preset", AROLL_BROLL_LAYOUTS, key="aroll_broll_layout")
        st.selectbox("Crop del presentador", AROLL_BROLL_CROPS, key="aroll_broll_crop")

    with right:
        st.selectbox(
            "Frecuencia B-roll",
            ["low", "medium", "high"],
            key="aroll_broll_frequency",
        )
        st.number_input(
            "Duración promedio por clip",
            min_value=2,
            max_value=12,
            step=1,
            key="aroll_broll_clip_seconds",
        )
        subtitle_label_by_value = {
            value: label for label, value in AROLL_BROLL_SUBTITLE_LABELS.items()
        }
        subtitle_label = st.selectbox(
            "Subtítulos",
            list(AROLL_BROLL_SUBTITLE_LABELS),
            index=list(AROLL_BROLL_SUBTITLE_LABELS).index(
                subtitle_label_by_value.get(
                    st.session_state["aroll_broll_subtitles_source"],
                    "none",
                )
            ),
            key="aroll_broll_subtitles_label",
        )
        st.session_state["aroll_broll_subtitles_source"] = (
            AROLL_BROLL_SUBTITLE_LABELS[subtitle_label]
        )
        if st.session_state["aroll_broll_subtitles_source"] == SUBTITLES_SOURCE_AROLL_AUDIO:
            st.warning("aroll_audio queda marcado como futuro hasta la fase renderer.")
        if st.session_state["aroll_broll_subtitles_source"] == SUBTITLES_SOURCE_CUSTOM_SRT:
            st.text_input(
                "Ruta SRT propia",
                key="aroll_broll_custom_srt_path",
                placeholder="storage/local_subtitles/subtitulos.srt",
            )
        quality_label = st.selectbox(
            "Calidad",
            list(QUALITY_LABELS),
            index=_index_for_value(
                QUALITY_LABELS,
                st.session_state["aroll_broll_quality"],
                "draft_720p",
            ),
            key="aroll_broll_quality_label",
        )
        st.session_state["aroll_broll_quality"] = QUALITY_LABELS[quality_label]

    st.markdown("### Preview")
    preview_cols = st.columns([1, 1])
    with preview_cols[0]:
        st.number_input(
            "Duración manual A-roll (segundos, opcional)",
            min_value=0.0,
            step=1.0,
            key="aroll_broll_duration_seconds",
        )
    with preview_cols[1]:
        st.number_input(
            "Cantidad conceptual de B-roll",
            min_value=0,
            max_value=30,
            step=1,
            key="aroll_broll_count",
        )

    config = _current_aroll_broll_config()
    read_only_validation = validate_aroll_broll_config(
        config,
        project_root=ROOT_DIR,
        strict=False,
    )
    _aroll_broll_manifest_assets_block(read_only_validation["normalized"])
    if st.button("Validar A-roll / B-roll", key="aroll_broll_validate"):
        st.session_state["aroll_broll_validation"] = read_only_validation

    validation = st.session_state.get("aroll_broll_validation")
    if validation:
        _aroll_broll_summary_block(validation["normalized"])
        _aroll_broll_validation_block(validation)
        _aroll_broll_timeline_block(validation["normalized"])
        try:
            queue_payload = _build_aroll_broll_queue_payload(validation["normalized"])
        except Exception as exc:
            st.button(
                "Enviar A-roll/B-roll a cola",
                key="aroll_broll_enqueue_disabled",
                disabled=True,
                help="Completa la validación estricta antes de crear el pending.",
            )
            st.warning(f"Queue protegido pendiente: {exc}")
        else:
            st.info(
                "Enviar a cola creará un pending job A-roll/B-roll protegido. "
                "El runner no lo renderiza todavía."
            )
            with st.expander("Modo avanzado: ver payload A-roll/B-roll JSON", expanded=False):
                st.json(queue_payload)
            if not queue_enabled:
                st.button(
                    "Enviar A-roll/B-roll a cola",
                    key="aroll_broll_enqueue_disabled",
                    disabled=True,
                    help=(
                        "Activa KURUKIN_ENABLE_AROLL_BROLL_QUEUE=1 solo para "
                        "pruebas controladas."
                    ),
                )
                st.warning(
                    "Cola A-roll/B-roll protegida: flag de cola apagado; no se crea pending."
                )
            elif st.button("Enviar A-roll/B-roll a cola", key="aroll_broll_enqueue"):
                try:
                    result = enqueue_aroll_broll_from_console(
                        validation["normalized"],
                        job_id=st.session_state["job_id"],
                        project_root=ROOT_DIR,
                        render_quality=st.session_state.get(
                            "aroll_broll_quality",
                            "draft_720p",
                        ),
                        title="A-roll/B-roll",
                        task_id=st.session_state["job_id"],
                        created_by=st.session_state.get(
                            "aroll_broll_created_by",
                            "render_console_ui",
                        ),
                    )
                    st.session_state["last_enqueued_job_id"] = str(result["job_id"])
                    st.session_state["last_enqueued_pending_path"] = str(
                        result["pending_path"]
                    )
                    st.session_state["last_enqueued_at"] = datetime.now(
                        timezone.utc
                    ).isoformat()
                    st.success("A-roll/B-roll enviado a cola protegida.")
                    st.info(
                        "El render no empezó. El runner rechazará este modo hasta "
                        "la fase de integración E2E."
                    )
                    st.caption(f"Pendiente protegido creado: {result['pending_path']}")
                except Exception as exc:
                    _show_error(exc)
    else:
        st.button(
            "Enviar A-roll/B-roll a cola",
            key="aroll_broll_enqueue_disabled",
            disabled=True,
            help="Primero valida A-roll/B-roll para activar este paso.",
        )
        st.info(
            "La cola A-roll/B-roll se activa después de una validación estricta "
            "y con KURUKIN_ENABLE_AROLL_BROLL_QUEUE=1."
        )


def _new_render_view():
    _initialize_form_state()
    _hero_block()
    _end_to_end_flow_block()
    video_type_label = st.selectbox(
        "Tipo de video",
        list(VIDEO_TYPE_LABELS),
        index=_index_for_value(
            VIDEO_TYPE_LABELS,
            st.session_state["video_type_mode"],
            "normal_assets",
        ),
        key="video_type_label",
    )
    st.session_state["video_type_mode"] = VIDEO_TYPE_LABELS[video_type_label]

    if st.session_state["video_type_mode"] == RENDER_MODE_AROLL_BROLL:
        _aroll_broll_view()
        return

    render_job_intent_step()
    render_batch_audio_intent_step()
    _first_video_guide()
    _progress_steps()
    _recommended_test_mode_block()
    manifest_summary = render_asset_source_step()
    render_content_step()
    render_audio_subtitles_step()
    render_quality_style_step()
    render_validate_enqueue_step(manifest_summary)
    render_mpt_native_local_submit_step()


def _status_label(status):
    return {
        "completed": "Completado",
        "failed": "Fallido",
        "processing": "En proceso",
        "unknown": "Sin estado claro",
    }.get(status, "Sin estado claro")


def _aroll_broll_visibility_block(item):
    if item.get("render_mode") != RENDER_MODE_AROLL_BROLL:
        return
    st.caption(f"Tipo: {item.get('render_mode_label') or 'Presentador + B-roll'}")
    st.caption(f"Layout: {item.get('layout_preset') or 'alternating_fullscreen'}")
    st.caption(f"Audio: {item.get('audio_summary') or 'A-roll original'}")
    st.caption(item.get("broll_summary") or "B-roll muted")
    if item.get("asset_policy_short_label"):
        st.caption(item["asset_policy_short_label"])
    if item.get("asset_materialization_source_label"):
        st.caption(f"Fuente: {item['asset_materialization_source_label']}")
    if item.get("asset_materialization_query"):
        st.caption(f"Query: {item['asset_materialization_query']}")
    if item.get("b_roll_asset_count"):
        st.caption(f"B-roll assets: {item['b_roll_asset_count']}")
    task_id = item.get("task_id")
    job_id = item.get("job_id") or item.get("completed_job_id")
    if task_id:
        st.caption(f"Task ID: {task_id}")
    if job_id:
        st.caption(f"Job ID: {job_id}")


def _pending_job_block(job):
    title = job.get("title") or "-"
    job_id = job.get("job_id") or job.get("filename") or "Trabajo pendiente"
    with st.expander(f"{job_id} - {title}", expanded=False):
        _aroll_broll_visibility_block(job)
        cols = st.columns(4)
        cols[0].metric("Calidad", job.get("quality") or "-")
        cols[1].metric("Fuente", job.get("asset_source") or "-")
        cols[2].metric("Subtítulos", job.get("subtitles") or "-")
        cols[3].metric("Tamaño", _human_bytes(job.get("size_bytes")))
        st.caption(f"Fecha aproximada: {_format_datetime(job.get('created_at_iso') or job.get('modified_at_iso'))}")
        st.caption(f"Ruta del pending json: {job.get('path')}")
        if job.get("valid_json"):
            with st.expander("Detalles JSON", expanded=False):
                st.json(job.get("raw") or {})
        else:
            st.warning("Este pending json no se pudo leer completo.")
            if job.get("error"):
                st.caption(job["error"])


def _task_block(task):
    label = _status_label(task.get("status"))
    with st.expander(f"{task.get('task_id')} - {label}", expanded=False):
        _aroll_broll_visibility_block(task)
        cols = st.columns(4)
        cols[0].metric("Estado", label)
        cols[1].metric("Videos", task.get("output_count", 0))
        cols[2].metric("Logs", task.get("log_count", 0))
        cols[3].metric("Tamaño", _human_bytes(task.get("size_bytes")))
        st.caption(f"Ruta del task: {task.get('path')}")
        st.caption(f"Última modificación: {_format_datetime(task.get('modified_at_iso'))}")
        if task.get("status") == "failed":
            st.warning("Este trabajo parece fallido. Revisa el diagnóstico.")
        if task.get("outputs"):
            st.markdown("**Videos detectados**")
            st.dataframe(
                [
                    {
                        "archivo": item.get("name"),
                        "tipo": item.get("render_mode_label") or "Video normal",
                        "tamaño": _human_bytes(item.get("size_bytes")),
                        "modificado": _format_datetime(item.get("modified_at_iso")),
                        "ruta": item.get("relative_path"),
                    }
                    for item in task["outputs"]
                ],
                use_container_width=True,
                hide_index=True,
            )
        if task.get("errors"):
            with st.expander("Diagnóstico de errores", expanded=False):
                for item in task["errors"]:
                    st.caption(f"{item.get('relative_path')} - {_human_bytes(item.get('size_bytes'))}")
                    if item.get("preview"):
                        st.code(item["preview"])
        if task.get("logs"):
            with st.expander("Logs detectados", expanded=False):
                for item in task["logs"]:
                    st.caption(f"{item.get('relative_path')} - {_human_bytes(item.get('size_bytes'))}")
                    if item.get("preview"):
                        st.code(item["preview"])


def _completed_job_block(job):
    label = job.get("render_mode_label") or "Video normal"
    title = job.get("job_id") or job.get("task_id") or job.get("completed_dir")
    with st.expander(f"{title} - {label}", expanded=False):
        _aroll_broll_visibility_block(job)
        cols = st.columns(4)
        cols[0].metric("Estado", job.get("state") or "-")
        cols[1].metric("Progreso", job.get("progress") or "-")
        cols[2].metric("Videos", len(job.get("videos") or []))
        cols[3].metric("Directorio", job.get("completed_dir") or "-")
        st.caption(f"Completado: {_format_datetime(job.get('completed_at'))}")
        if job.get("final_video_paths"):
            st.caption("Outputs: " + ", ".join(job.get("final_video_paths") or []))


def _outputs_table(outputs):
    if not outputs:
        st.info("Todavía no hay renders completados.")
        return
    st.dataframe(
        [
            {
                "archivo": item.get("name"),
                "task": item.get("task_id"),
                "tipo": item.get("render_mode_label") or "Video normal",
                "tamaño": _human_bytes(item.get("size_bytes")),
                "modificado": _format_datetime(item.get("modified_at_iso")),
                "ruta": item.get("path"),
            }
            for item in outputs
        ],
        use_container_width=True,
        hide_index=True,
    )


def _last_enqueued_pending_job(pending_jobs):
    last_job_id = st.session_state.get("last_enqueued_job_id")
    last_pending_path = st.session_state.get("last_enqueued_pending_path")
    if not last_job_id and not last_pending_path:
        return None
    for job in pending_jobs:
        if last_pending_path and job.get("path") == last_pending_path:
            return job
        if last_job_id and job.get("job_id") == last_job_id:
            return job
    return None


def _intent_queue_jobs(pending_jobs):
    jobs = []
    for job in pending_jobs:
        raw = job.get("raw") or {}
        if raw.get("source") != "job_intent_v1":
            continue
        if str(raw.get("status") or "").upper() not in {"QUEUED", "PENDING"}:
            continue
        jobs.append(job)
    return jobs


def _manual_intent_queue_process_block(pending_jobs):
    intent_jobs = _intent_queue_jobs(pending_jobs)
    submit_enabled = is_mpt_engine_submit_enabled()
    st.markdown("### Intenciones en cola")
    if not intent_jobs:
        st.caption("No hay intenciones job_intent_v1 en estado QUEUED.")
        return
    if not submit_enabled:
        st.warning(
            "Procesar con MPT nativo bloqueado por "
            f"{MPT_ENGINE_SUBMIT_FLAG}=1."
        )
    batch_cols = st.columns([1, 2])
    with batch_cols[0]:
        st.number_input(
            "max_items proceso lote",
            min_value=1,
            max_value=5,
            step=1,
            key="intent_queue_process_max_items",
        )
    with batch_cols[1]:
        if st.button(
            "Procesar lote con MPT nativo",
            key="process_intent_queue_batch",
            disabled=not submit_enabled,
            help=f"Requiere {MPT_ENGINE_SUBMIT_FLAG}=1.",
        ):
            result = process_queued_intent_batch_with_mpt_native(
                [job.get("path", "") for job in intent_jobs],
                max_items=int(st.session_state.get("intent_queue_process_max_items", 2)),
            )
            st.session_state["intent_queue_last_batch_process_result"] = result
            if result.get("ok"):
                st.success("Lote procesado con MPT nativo.")
            else:
                st.warning("El proceso de lote terminó con errores.")
    batch_result = st.session_state.get("intent_queue_last_batch_process_result") or {}
    if batch_result:
        metric_cols = st.columns(3)
        metric_cols[0].metric("processed", batch_result.get("processed", 0))
        metric_cols[1].metric("done", batch_result.get("done", 0))
        metric_cols[2].metric("failed", batch_result.get("failed", 0))
        if batch_result.get("items"):
            st.dataframe(
                [
                    {
                        "task_id": item.get("task_id", ""),
                        "status": item.get("status", ""),
                        "output_path": item.get("output_path", ""),
                        "queue_item_path": item.get("queue_item_path", ""),
                        "error": item.get("error", ""),
                    }
                    for item in batch_result.get("items", [])
                ],
                use_container_width=True,
                hide_index=True,
            )
    for index, job in enumerate(intent_jobs):
        raw = job.get("raw") or {}
        with st.expander(f"{raw.get('task_id') or job.get('job_id')} - job_intent_v1", expanded=False):
            st.caption(f"task_id: {raw.get('task_id')}")
            st.caption(f"status: {raw.get('status')}")
            st.caption(f"source: {raw.get('source')}")
            st.caption(f"mode: {raw.get('mode')}")
            st.caption(f"resolved_visual_path: {raw.get('resolved_visual_path')}")
            if raw.get("output_path"):
                st.caption(f"output_path: {raw.get('output_path')}")
            if raw.get("error"):
                st.warning(str(raw.get("error")))
            key = f"process_intent_queue_{index}_{sanitize_job_id(str(raw.get('task_id') or job.get('job_id') or 'intent'))}"
            if st.button(
                "Procesar con MPT nativo",
                key=key,
                disabled=not submit_enabled,
                help=f"Requiere {MPT_ENGINE_SUBMIT_FLAG}=1.",
            ):
                result = process_queued_intent_with_mpt_native(job.get("path", ""))
                st.session_state["intent_queue_last_process_result"] = result
                if result.get("ok"):
                    st.success("Intención procesada con MPT nativo.")
                else:
                    st.error("No se pudo procesar la intención en cola.")
            result = st.session_state.get("intent_queue_last_process_result") or {}
            if result.get("pending_path") == job.get("path"):
                st.json(result)


def _intent_result_video(result):
    output_path = result.get("output_path")
    if not output_path:
        return None
    path = Path(str(output_path))
    try:
        absolute_path = path.resolve(strict=True)
    except (FileNotFoundError, OSError):
        return None
    if not absolute_path.is_file() or absolute_path.suffix.lower() != ".mp4":
        return None
    tasks_root = Path("storage/tasks").resolve(strict=False)
    try:
        relative_path = absolute_path.relative_to(tasks_root).as_posix()
    except ValueError:
        relative_path = absolute_path.name
    size_bytes = absolute_path.stat().st_size
    return {
        "absolute_path": absolute_path.as_posix(),
        "relative_path": relative_path,
        "file_name": absolute_path.name,
        "task_id": result.get("task_id"),
        "kind": "final" if absolute_path.name.startswith("final-") else "other",
        "size_bytes": size_bytes,
        "size_label": _human_bytes(size_bytes),
        "is_previewable": size_bytes <= VIDEO_PREVIEW_MAX_BYTES,
    }


def _intent_results_panel():
    st.markdown("### Resultados de intenciones")
    st.caption("source: job_intent_v1")
    filter_cols = st.columns([1, 1, 2])
    with filter_cols[0]:
        status_filter = st.selectbox(
            "status intenciones",
            ["ALL", "QUEUED", "PROCESSING", "DONE", "FAILED"],
            key="intent_results_status_filter",
        )
    with filter_cols[1]:
        limit = st.number_input(
            "limit intenciones",
            min_value=1,
            max_value=100,
            step=1,
            key="intent_results_limit",
        )

    results = list_intent_results(status=status_filter, limit=int(limit))
    if not results:
        st.info("No hay resultados de intenciones para el filtro seleccionado.")
        return

    st.dataframe(
        [
            {
                "task_id": result.get("task_id", ""),
                "mode": result.get("mode", ""),
                "status": result.get("status", ""),
                "output_exists": result.get("output_exists", False),
                "output_path": result.get("output_path", ""),
                "queue_item_path": result.get("queue_item_path", ""),
                "error": result.get("error", ""),
            }
            for result in results
        ],
        use_container_width=True,
        hide_index=True,
    )

    submit_enabled = is_mpt_engine_submit_enabled()
    for index, result in enumerate(results):
        task_id = result.get("task_id") or "intent"
        status = str(result.get("status") or "").upper()
        with st.expander(f"{task_id} - {status}", expanded=False):
            st.caption(f"queue_item_path: {result.get('queue_item_path')}")
            st.caption(f"resolved_visual_path: {result.get('resolved_visual_path')}")
            st.caption(f"output_path: {result.get('output_path') or '-'}")
            if result.get("error"):
                st.warning(str(result.get("error")))

            video = _intent_result_video(result)
            if video is not None:
                _result_preview_download(
                    video,
                    key_prefix=(
                        f"intent_result_{index}_"
                        f"{sanitize_job_id(str(task_id))}"
                    ),
                )

            if status in {"QUEUED", "PENDING", "FAILED"}:
                if not submit_enabled:
                    st.warning(
                        "Procesar con MPT nativo bloqueado por "
                        f"{MPT_ENGINE_SUBMIT_FLAG}=1."
                    )
                label = (
                    "Reintentar con MPT nativo"
                    if status == "FAILED"
                    else "Procesar con MPT nativo"
                )
                if st.button(
                    label,
                    key=(
                        f"intent_result_process_{index}_"
                        f"{sanitize_job_id(str(task_id))}"
                    ),
                    disabled=not submit_enabled,
                    help=f"Requiere {MPT_ENGINE_SUBMIT_FLAG}=1.",
                ):
                    process_result = process_queued_intent_with_mpt_native(
                        result.get("queue_item_path", "")
                    )
                    st.session_state["intent_results_last_process_result"] = process_result
                    if process_result.get("ok"):
                        st.success("Intención procesada con MPT nativo.")
                    else:
                        st.error("No se pudo procesar la intención.")
            process_result = st.session_state.get("intent_results_last_process_result") or {}
            if process_result.get("pending_path") == result.get("queue_item_path"):
                st.json(process_result)


def _queue_storage_view():
    st.title("Cola y resultados")
    st.write("Revisa trabajos pendientes, resultados generados y errores sin entrar por terminal.")
    st.info("Esta pantalla solo consulta estado. No ejecuta renders ni modifica archivos.")
    st.caption(
        "Después de Enviar a cola, ejecuta el runner controlado para que use la API Docker, "
        "haga el render y deje el MP4 en Resultados."
    )
    st.button("Actualizar estado", key="refresh_queue")

    lifecycle = get_job_lifecycle_summary()
    counts = lifecycle.get("counts", {})
    pending_jobs = lifecycle.get("pending_jobs", [])
    completed_jobs = lifecycle.get("completed_jobs", [])
    tasks = lifecycle.get("tasks", [])
    outputs = lifecycle.get("outputs", [])
    failed_tasks = [task for task in tasks if task.get("status") == "failed"]

    metric_cols = st.columns(5)
    metric_cols[0].metric("Pendientes", counts.get("pending", 0))
    metric_cols[1].metric("En proceso", counts.get("processing", 0))
    metric_cols[2].metric("Completados", counts.get("completed", 0))
    metric_cols[3].metric("Fallidos", counts.get("failed", 0))
    metric_cols[4].metric("Videos detectados", counts.get("videos", 0))

    st.markdown("### Trabajos pendientes")
    last_pending_job = _last_enqueued_pending_job(pending_jobs)
    if last_pending_job:
        st.success(f"Último video enviado a cola: {last_pending_job.get('job_id')}")
    _manual_intent_queue_process_block(pending_jobs)
    _intent_results_panel()
    if pending_jobs:
        for job in pending_jobs:
            _pending_job_block(job)
    else:
        st.info("No hay trabajos pendientes. Cuando envíes un video a cola, aparecerá aquí.")

    st.markdown("### Jobs completados")
    if completed_jobs:
        for job in completed_jobs:
            _completed_job_block(job)
    else:
        st.info("No hay jobs completados en la cola nocturna.")

    st.markdown("### Trabajos/tasks detectados")
    if tasks:
        for task in tasks:
            _task_block(task)
    else:
        st.info("Cuando envíes un video a cola y el runner lo procese, aparecerá aquí.")

    st.markdown("### Outputs detectados")
    _outputs_table(outputs)

    st.markdown("### Errores y fallos")
    if failed_tasks:
        for task in failed_tasks:
            st.warning(f"{task.get('task_id')}: Este trabajo parece fallido. Revisa el diagnóstico.")
    else:
        st.info("No hay errores detectados.")

    with st.expander("Diagnóstico de cola", expanded=False):
        st.caption("Lectura local de cola y tasks. No modifica archivos.")
        st.json(lifecycle)


def _video_option_label(video):
    kind_label = {"final": "Final", "combined": "Combinado"}.get(
        video.get("kind"),
        "Video",
    )
    mode_label = video.get("render_mode_label") or "Video normal"
    return (
        f"{kind_label} - {mode_label} - {video.get('task_id')} - {video.get('file_name')} "
        f"({_human_bytes(video.get('size_bytes'))})"
    )


def _safe_download_filename(video):
    task_id = sanitize_job_id(video.get("task_id") or "task")
    file_name = sanitize_job_id(Path(video.get("file_name") or "video").stem)
    return f"kurukin-{task_id}-{file_name}.mp4"


def _results_details(video):
    _aroll_broll_visibility_block(video)
    st.caption(f"Ruta relativa: {video.get('relative_path')}")
    st.caption(f"Tamaño: {video.get('size_label')}")
    st.caption(f"Modificado: {_format_datetime(video.get('modified_at'))}")
    st.caption(f"task_id: {video.get('task_id')}")
    if video.get("completed_job_id"):
        st.caption(f"job_id inferido: {video.get('completed_job_id')}")

    final_task_summary = video.get("final_task_summary") or {}
    if final_task_summary:
        st.markdown("**final-task.json**")
        st.json(
            {
                "state": final_task_summary.get("state"),
                "progress": final_task_summary.get("progress"),
                "videos": final_task_summary.get("videos"),
            }
        )
    else:
        st.info("No se detectó final-task.json asociado.")

    if video.get("error_summary"):
        st.warning(f"Error detectado: {video.get('error_summary')}")

    with st.expander("Diagnóstico avanzado", expanded=False):
        st.caption("Ruta absoluta interna validada bajo storage/tasks.")
        st.json(
            {
                "absolute_path": video.get("absolute_path"),
                "kind": video.get("kind"),
                "is_previewable": video.get("is_previewable"),
            }
        )


def _result_preview_download(video, *, key_prefix):
    st.markdown("### Preview")
    if video.get("is_previewable"):
        st.video(video.get("absolute_path"))
        st.success("Preview disponible.")
    else:
        st.warning(
            "El video supera el límite de preview automático de "
            f"{_human_bytes(VIDEO_PREVIEW_MAX_BYTES)}."
        )

    st.markdown("### Descargar")
    if video.get("size_bytes", 0) > VIDEO_DOWNLOAD_MEMORY_MAX_BYTES:
        st.warning(
            "El video supera el límite de descarga directa de "
            f"{_human_bytes(VIDEO_DOWNLOAD_MEMORY_MAX_BYTES)}."
        )
        return

    data = read_video_bytes_for_download(video)
    if data is None:
        st.warning("No se pudo preparar la descarga del video seleccionado.")
        return

    st.download_button(
        "Descargar MP4",
        data=data,
        file_name=_safe_download_filename(video),
        mime="video/mp4",
        key=f"{key_prefix}_download_mp4",
    )


def _highlighted_result_block(video):
    if video.get("recommendation") == "last_job":
        title = "Tu video más reciente"
        st.success(title)
    else:
        title = "Último video generado"
        st.info(title)

    cols = st.columns(5)
    cols[0].metric("job_id", video.get("job_id") or video.get("completed_job_id") or "-")
    cols[1].metric("task_id", video.get("task_id") or "-")
    cols[2].metric("Archivo final", video.get("file_name") or "-")
    cols[3].metric("Tamaño", video.get("size_label") or _human_bytes(video.get("size_bytes")))
    cols[4].metric("Estado", "Generado correctamente")
    st.caption(f"Fecha: {_format_datetime(video.get('modified_at') or video.get('completed_at'))}")
    _aroll_broll_visibility_block(video)

    _result_preview_download(video, key_prefix="recommended_result")
    with st.expander("Detalles del resultado destacado", expanded=False):
        _results_details(video)


def _results_view():
    st.title("Resultados generados")
    st.write("Reproduce y descarga videos ya generados bajo storage/tasks.")
    st.info("Esta pestaña solo lee resultados existentes. No ejecuta runner ni crea trabajos.")

    videos = list_rendered_videos()
    latest_video = get_latest_rendered_video()
    recommended_result = get_recommended_result(
        last_job_id=st.session_state.get("last_enqueued_job_id")
    )
    lifecycle = get_job_lifecycle_summary()
    counts = lifecycle.get("counts", {})

    metric_cols = st.columns(4)
    metric_cols[0].metric("Videos encontrados", len(videos))
    metric_cols[1].metric("Jobs completados", counts.get("completed", 0))
    metric_cols[2].metric("Jobs fallidos", counts.get("failed", 0))
    metric_cols[3].metric(
        "Último video generado",
        latest_video.get("file_name") if latest_video else "-",
    )

    if not videos:
        st.info(
            "Todavía no hay videos generados. Crea un video, envíalo a cola "
            "y ejecútalo desde la pestaña Ejecutar."
        )
        return

    if recommended_result:
        _highlighted_result_block(recommended_result)

    st.markdown("### Todos los videos generados")
    st.dataframe(
        [
            {
                "tipo": video.get("render_mode_label") or "Video normal",
                "archivo_tipo": video.get("kind"),
                "task_id": video.get("task_id"),
                "archivo": video.get("file_name"),
                "tamaño": video.get("size_label"),
                "modificado": _format_datetime(video.get("modified_at")),
                "ruta": video.get("relative_path"),
            }
            for video in videos
        ],
        use_container_width=True,
        hide_index=True,
    )

    option_labels = [_video_option_label(video) for video in videos]
    default_index = 0
    default_video = recommended_result or latest_video
    if default_video:
        latest_relative_path = default_video.get("relative_path")
        for index, video in enumerate(videos):
            if video.get("relative_path") == latest_relative_path:
                default_index = index
                break

    selected_label = st.selectbox(
        "Video para preview y descarga",
        option_labels,
        index=default_index,
        key="results_selected_video",
    )
    selected_index = option_labels.index(selected_label)
    selected_video = videos[selected_index]

    _result_preview_download(selected_video, key_prefix="results")

    with st.expander("Detalles del resultado", expanded=False):
        _results_details(selected_video)


def _check_state_message(check):
    status = check.get("status") or "Revisar"
    text = f"{status}: {check.get('name')} - {check.get('detail')}"
    if status == "Listo":
        st.success(text)
    elif status == "No disponible":
        st.error(text)
    else:
        st.warning(text)


def _runner_candidates_block(candidates):
    st.markdown("### Runner detectado")
    if not candidates:
        st.warning("No detectado. Revisa que el runner exista en el repo antes de habilitar ejecución.")
        return
    for candidate in candidates:
        with st.expander(candidate.get("name") or "Runner", expanded=False):
            st.caption(f"Ruta: {candidate.get('path')}")
            st.caption(f"Comando sugerido para futuro uso controlado: {candidate.get('suggested_command')}")
            st.caption(f"Confianza: {candidate.get('confidence')}")
            st.info(candidate.get("notes") or "Detectado por existencia de archivo.")


def _preflight_view():
    st.title("Preflight de render")
    st.write("Revisa si el worker está listo antes de ejecutar renders.")
    st.info("Esta pantalla no ejecuta el runner. Solo revisa condiciones de seguridad.")

    preflight = get_runner_preflight_summary(project_root=ROOT_DIR)
    counts = preflight.get("counts", {})
    storage = preflight.get("storage", {})
    runners = preflight.get("runner_candidates", [])

    metric_cols = st.columns(5)
    metric_cols[0].metric("Pendientes", counts.get("pending", 0))
    metric_cols[1].metric("Tasks detectados", counts.get("tasks", 0))
    metric_cols[2].metric("Videos detectados", counts.get("videos", 0))
    metric_cols[3].metric("Storage usado", _human_bytes(storage.get("size_bytes")))
    metric_cols[4].metric("Runner detectado", "Sí" if runners else "No")

    st.markdown("### Checklist de seguridad")
    for check in preflight.get("checks", []):
        _check_state_message(check)

    _runner_candidates_block(runners)

    st.markdown("### Próximo paso")
    st.info(
        "Cuando habilitemos ejecución controlada, aquí aparecerá un botón con "
        "confirmación doble para procesar trabajos pendientes."
    )

    st.markdown("### Riesgos")
    st.warning(
        "Ejecutar render puede consumir CPU, aumentar storage y tardar varios "
        "minutos. No debe ejecutarse si hay jobs no revisados."
    )

    with st.expander("Diagnóstico del preflight", expanded=False):
        st.caption("Resumen read-only. No ejecuta runner ni modifica archivos.")
        st.json(preflight)


def _controlled_runner_view():
    st.title("Ejecución controlada")
    st.write("Procesa trabajos pendientes solo cuando estés seguro.")
    st.markdown("### Procesar 1 trabajo ahora")
    st.info(
        "Este modo salta la ventana nocturna solo para una ejecución manual controlada."
        f"\n\nCola controlada: {CONTAINER_NIGHTLY_QUEUE_DIR}"
        f"\n\nAPI Docker: {CONTAINER_API_BASE_URL}"
    )
    st.error(
        "Esta acción sí ejecutará el runner y puede consumir CPU, tiempo y storage. "
        "Usa este modo solo para una prueba autorizada."
    )

    preflight = get_runner_preflight_summary(project_root=ROOT_DIR)
    command_info = build_safe_runner_command(
        project_root=ROOT_DIR,
        manual_override=True,
        max_jobs=MANUAL_RUNNER_MAX_JOBS,
    )
    feature_enabled = is_ui_runner_enabled()
    counts = preflight.get("counts", {})
    storage = preflight.get("storage", {})

    if feature_enabled:
        st.success("Habilitada para prueba controlada.")
    else:
        st.warning(
            "Ejecución desde UI deshabilitada por seguridad. Activa "
            "KURUKIN_ENABLE_UI_RUNNER=1 solo para una prueba controlada."
        )

    metric_cols = st.columns(5)
    metric_cols[0].metric("Pendientes", counts.get("pending", 0))
    metric_cols[1].metric("Runner detectado", "Sí" if command_info.get("available") else "No")
    metric_cols[2].metric("Storage usado", _human_bytes(storage.get("size_bytes")))
    metric_cols[3].metric("Feature flag", "Activo" if feature_enabled else "Inactivo")
    metric_cols[4].metric("Máximo de trabajos", MANUAL_RUNNER_MAX_JOBS)

    with st.expander("Comando seguro calculado", expanded=False):
        st.json(command_info)

    st.markdown("### Checklist")
    for check in preflight.get("checks", []):
        _check_state_message(check)

    st.warning(
        "Ejecutar render puede consumir CPU, aumentar storage y tardar varios "
        "minutos. No lo ejecutes si hay jobs no revisados."
    )

    with st.expander("Zona peligrosa: ejecutar runner", expanded=False):
        understood = st.checkbox(
            "Entiendo que esto ejecutará render real.",
            key="runner_understood_real_render",
        )
        confirm_text = st.text_input(
            "Escribe exactamente EJECUTAR RENDER",
            key="runner_confirm_text",
        )
        queue_confirmation = st.text_input(
            "Escribe procesar cola pendiente",
            key="runner_queue_confirmation",
        )
        validation = validate_runner_execution_request(
            feature_enabled=feature_enabled,
            preflight_summary=preflight,
            command_info=command_info,
            understood=understood,
            confirm_text=confirm_text,
            queue_confirmation=queue_confirmation,
            execution_mode=MANUAL_RUNNER_EXECUTION_MODE,
            max_jobs=MANUAL_RUNNER_MAX_JOBS,
        )
        if validation.get("allowed"):
            st.success("Todas las confirmaciones están listas.")
        else:
            for error in validation.get("errors", []):
                st.caption(error)
        if st.button(
            "Ejecutar runner controlado",
            key="controlled_runner_execute",
            disabled=not validation.get("allowed"),
        ):
            try:
                result = run_controlled_runner(command_info)
                st.json(result)
            except Exception as exc:
                _show_error(exc)

    with st.expander("Diagnóstico de ejecución controlada", expanded=False):
        st.json(
            {
                "preflight_counts": counts,
                "command": command_info,
                "feature_enabled": feature_enabled,
                "execution_mode": MANUAL_RUNNER_EXECUTION_MODE,
                "max_jobs": MANUAL_RUNNER_MAX_JOBS,
                "queue_dir": CONTAINER_NIGHTLY_QUEUE_DIR,
                "api_base_url": CONTAINER_API_BASE_URL,
                "required_confirm_text": RUNNER_CONFIRM_TEXT,
                "required_queue_confirmation": RUNNER_QUEUE_CONFIRM_TEXT,
            }
        )


def _diagnostics_expander():
    with st.expander("Diagnóstico", expanded=False):
        lifecycle = get_job_lifecycle_summary()
        st.caption("Lectura local de cola y storage. No modifica archivos.")
        st.json(
            {
                "queue_counts": lifecycle.get("counts", {}),
                "task_count": len(lifecycle.get("tasks", [])),
                "local_dirs": {
                    "videos": DEFAULT_LOCAL_VIDEOS_DIR,
                    "audios": DEFAULT_LOCAL_AUDIOS_DIR,
                    "subtitles": DEFAULT_LOCAL_SUBTITLES_DIR,
                },
                "asset_source_mode": st.session_state.get("asset_source_mode"),
            }
        )


st.set_page_config(page_title="Crear video Kurukin", layout="wide")
_apply_page_style()

tab_new, tab_queue, tab_results, tab_preflight, tab_execute = st.tabs(
    ["Crear video", "Cola", "Resultados", "Preflight", "Ejecutar"]
)
with tab_new:
    _new_render_view()
with tab_queue:
    _queue_storage_view()
with tab_results:
    _results_view()
with tab_preflight:
    _preflight_view()
with tab_execute:
    _controlled_runner_view()

_diagnostics_expander()
