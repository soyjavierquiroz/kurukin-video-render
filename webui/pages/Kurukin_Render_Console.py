import json
import os
import sys
from datetime import datetime, timezone

import streamlit as st


ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
if ROOT_DIR not in sys.path:
    sys.path.append(ROOT_DIR)

from app.custom.kurukin_job_adapter import (  # noqa: E402
    ALLOWED_AUDIO_EXTENSIONS,
    ALLOWED_EXTENSIONS,
    ALLOWED_SUBTITLE_EXTENSIONS,
    DEFAULT_LOCAL_AUDIOS_DIR,
    DEFAULT_LOCAL_SUBTITLES_DIR,
    DEFAULT_LOCAL_VIDEOS_DIR,
)
from app.custom.kurukin_job_queue import (  # noqa: E402
    enqueue_moneyprinter_payload,
    get_storage_summary,
    list_nightly_queue,
    list_render_tasks,
)
from app.custom.kurukin_render_console import (  # noqa: E402
    ASSET_SOURCE_ASSET_HUB,
    ASSET_SOURCE_LOCAL,
    ASSET_SOURCE_STOCK,
    build_operator_summary,
    build_render_console_spec,
    default_asset_hub_manifest_path,
    get_manifest_summary_for_ui,
    list_local_storage_files,
    safe_relative_path,
    validate_and_build_payload_from_console_spec,
)


DEFAULT_BUNDLE_UID = "jab_b28367fb22d44a40bae507c175f464c4"
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


def _initialize_form_state():
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
        cols = st.columns(5)
        cols[0].metric("Escenas", summary.get("total_scenes", 0))
        cols[1].metric("Assets", summary.get("total_assets", 0))
        cols[2].metric(
            "Duración aprox.",
            _format_duration(summary.get("duration_total_seconds")),
        )
        cols[3].metric("Avisos", summary.get("warnings_count", 0))
        cols[4].metric("Para revisar", summary.get("needs_human_review_count", 0))
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
                "Encontramos avisos en el paquete. Puedes continuar, pero conviene "
                "revisarlos."
            )
        if summary.get("needs_human_review_count"):
            st.warning("Hay elementos que requieren revisión antes de publicar.")
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
    cols[4].metric("Audio", operator.get("audio") or "-")
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
            "Código del paquete de assets",
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
            "Identificador del video",
            key="job_id",
            help="Identificador humano para encontrar el trabajo en la cola.",
        )
    with right:
        st.text_input("Título del video", key="video_subject")
    st.text_area(
        "Guion o descripción",
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
        "Enviar a cola no renderiza inmediatamente. El video queda pendiente para "
        "que el runner lo procese cuando se ejecute."
    )

    actions = st.columns([1, 1, 3])
    validate_clicked = actions[0].button("Validar video", key="form_validate")
    enqueue_clicked = actions[1].button("Enviar a cola", key="form_enqueue")

    if validate_clicked or enqueue_clicked:
        try:
            spec, payload, manifest_summary, operator = _validate_form_payload()
            _show_validation_result(spec, payload, manifest_summary, operator)
            if enqueue_clicked:
                path = enqueue_moneyprinter_payload(payload)
                st.success("Video enviado a cola. No se ejecutó render.")
                st.caption(f"Pendiente creado: {path}")
        except Exception as exc:
            _show_error(exc)
    elif st.session_state.get("form_validated_payload"):
        _show_validation_result(
            st.session_state["form_validated_spec"],
            st.session_state["form_validated_payload"],
            st.session_state["form_manifest_summary"],
            st.session_state["form_operator_summary"],
        )
    else:
        st.caption("Valida el video para ver el resumen y el JSON avanzado.")
        with st.expander("Modo avanzado: ver payload JSON", expanded=False):
            st.caption("El payload aparecerá aquí después de validar el video.")


def _new_render_view():
    _initialize_form_state()
    _hero_block()
    _progress_steps()

    manifest_summary = render_asset_source_step()
    render_content_step()
    render_audio_subtitles_step()
    render_quality_style_step()
    render_validate_enqueue_step(manifest_summary)


def _queue_table(title, rows):
    st.markdown(f"**{title}**")
    if rows:
        st.dataframe(rows, use_container_width=True)
    else:
        st.info("Sin trabajos")


def _queue_storage_view():
    st.subheader("Cola de render")
    st.button("Actualizar", key="refresh_queue")

    queue = list_nightly_queue()
    tasks = list_render_tasks()
    storage_summary = get_storage_summary()

    final_tasks = [item for item in tasks if item.get("has_final_video")]
    metric_cols = st.columns(6)
    metric_cols[0].metric("Pendiente", len(queue.get("pending", [])))
    metric_cols[1].metric("Procesando", len(queue.get("processing", [])))
    metric_cols[2].metric("Completado", len(queue.get("completed", [])))
    metric_cols[3].metric("Fallido", len(queue.get("failed", [])))
    metric_cols[4].metric("Videos finales", len(final_tasks))
    metric_cols[5].metric("Storage", _human_bytes(storage_summary.get("size_bytes")))

    for group, label in (
        ("pending", "Pendiente"),
        ("processing", "Procesando"),
        ("completed", "Completado"),
        ("failed", "Fallido"),
    ):
        _queue_table(label, queue.get(group, []))


def _diagnostics_expander():
    with st.expander("Diagnóstico", expanded=False):
        queue = list_nightly_queue()
        tasks = list_render_tasks()
        storage_summary = get_storage_summary()
        st.caption("Lectura local de cola y storage. No modifica archivos.")
        st.json(
            {
                "queue_counts": {group: len(items) for group, items in queue.items()},
                "task_count": len(tasks),
                "storage_size": _human_bytes(storage_summary.get("size_bytes")),
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

tab_new, tab_queue = st.tabs(["Crear video", "Cola"])
with tab_new:
    _new_render_view()
with tab_queue:
    _queue_storage_view()

_diagnostics_expander()
