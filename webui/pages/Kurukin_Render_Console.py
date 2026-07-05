import json
import os
import sys

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
    build_render_console_spec,
    default_asset_hub_manifest_path,
    validate_and_build_payload_from_console_spec,
)


EXAMPLE_SPEC = {
    "job_id": "render-console-example-001",
    "description": "Render Console example",
    "asset_hub": {
        "renderer_manifest_path": (
            "/data/job-assets/jab_b28367fb22d44a40bae507c175f464c4/"
            "manifests/renderer-manifest.json"
        ),
        "bundle_uid": "jab_b28367fb22d44a40bae507c175f464c4",
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


def _show_validation_result(spec):
    payload, summary = validate_and_build_payload_from_console_spec(spec)
    st.success("Payload valido para encolar.")
    st.subheader("Summary")
    st.json(summary)
    st.subheader("Kurukin Job Spec")
    st.json(spec)
    st.subheader("MoneyPrinterTurbo Payload")
    st.json(payload)
    return payload, summary


def _show_error(exc):
    st.error(str(exc))


def _new_render_tab():
    st.subheader("Nuevo render")

    col_a, col_b = st.columns(2)
    with col_a:
        job_id = st.text_input("job_id", value="render-console-001")
        video_subject = st.text_input("video_subject", value="Render Console Example")
        render_quality = st.selectbox(
            "render_quality",
            ["draft_720p", "standard_1080p", "premium_2k"],
        )
        video_aspect = st.selectbox("video_aspect", ["9:16", "16:9"])
        audio_file = st.text_input("audio_file", placeholder="audio-prueba.mp3")
        subtitles_mode = st.selectbox(
            "subtitles_mode",
            ["none", "whisper", "edge", "custom_srt"],
        )
        custom_subtitle_file = ""
        if subtitles_mode == "custom_srt":
            custom_subtitle_file = st.text_input("custom_subtitle_file")

    with col_b:
        asset_hub_bundle_uid = st.text_input("asset_hub_bundle_uid")
        default_manifest_path = ""
        if asset_hub_bundle_uid.strip():
            try:
                default_manifest_path = default_asset_hub_manifest_path(
                    asset_hub_bundle_uid,
                )
            except ValueError as exc:
                st.warning(str(exc))
        asset_hub_renderer_manifest_path = st.text_input(
            "asset_hub_renderer_manifest_path",
            value=default_manifest_path,
        )
        subtitle_style_preset = st.selectbox(
            "subtitle_style_preset",
            [
                "clean_center_bold_safe",
                "clean_center_bold",
                "clean_bottom_bold",
                "boxed_bottom",
                "large_hook_center",
            ],
        )
        image_motion_enabled = st.checkbox("image_motion_enabled")
        image_motion_preset = st.selectbox(
            "image_motion_preset",
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
            index=1,
        )
        image_motion_intensity = st.slider(
            "image_motion_intensity",
            min_value=0.0,
            max_value=0.20,
            value=0.06,
            step=0.01,
        )
        video_clip_duration = st.number_input(
            "video_clip_duration",
            min_value=1,
            max_value=20,
            value=4,
            step=1,
        )
        n_threads = st.number_input(
            "n_threads",
            min_value=1,
            max_value=4,
            value=2,
            step=1,
        )

    video_script = st.text_area("video_script", value="Example script.", height=180)

    actions = st.columns([1, 1, 4])
    validate_clicked = actions[0].button("Build / validar payload", key="form_validate")
    enqueue_clicked = actions[1].button("Encolar job", key="form_enqueue")

    if validate_clicked or enqueue_clicked:
        try:
            spec = build_render_console_spec(
                job_id=job_id,
                video_subject=video_subject,
                video_script=video_script,
                render_quality=render_quality,
                video_aspect=video_aspect,
                asset_hub_bundle_uid=asset_hub_bundle_uid,
                asset_hub_renderer_manifest_path=asset_hub_renderer_manifest_path,
                audio_file=audio_file,
                subtitles_mode=subtitles_mode,
                custom_subtitle_file=custom_subtitle_file,
                subtitle_style_preset=subtitle_style_preset,
                image_motion_enabled=image_motion_enabled,
                image_motion_preset=image_motion_preset,
                image_motion_intensity=image_motion_intensity,
                video_clip_duration=int(video_clip_duration),
                n_threads=int(n_threads),
            )
            payload, _ = _show_validation_result(spec)
            if enqueue_clicked:
                path = enqueue_moneyprinter_payload(payload)
                st.success(f"Job encolado: {path}")
        except Exception as exc:
            _show_error(exc)


def _advanced_json_tab():
    st.subheader("JSON avanzado")
    raw_spec = st.text_area(
        "Kurukin Job Spec JSON",
        value=json.dumps(EXAMPLE_SPEC, indent=2, ensure_ascii=False),
        height=520,
    )

    actions = st.columns([1, 1, 4])
    validate_clicked = actions[0].button("Validate JSON", key="json_validate")
    enqueue_clicked = actions[1].button("Enqueue JSON", key="json_enqueue")

    if validate_clicked or enqueue_clicked:
        try:
            spec = json.loads(raw_spec)
            if not isinstance(spec, dict):
                raise ValueError("JSON must be an object")
            payload, _ = _show_validation_result(spec)
            if enqueue_clicked:
                path = enqueue_moneyprinter_payload(payload)
                st.success(f"Job encolado: {path}")
        except Exception as exc:
            _show_error(exc)


def _queue_table(title, rows):
    st.markdown(f"**{title}**")
    if rows:
        st.dataframe(rows, use_container_width=True)
    else:
        st.info("Sin entradas.")


def _queue_storage_tab():
    st.subheader("Cola y storage")
    st.button("Refresh", key="refresh_queue")

    queue = list_nightly_queue()
    for group in ("pending", "processing", "completed", "failed", "logs"):
        _queue_table(group, queue.get(group, []))

    _queue_table("tasks", list_render_tasks())
    st.markdown("**storage summary**")
    st.json(get_storage_summary())


st.set_page_config(page_title="Kurukin Render Console", layout="wide")
st.title("Kurukin Render Console")
st.write("MVP interno: valida y encola jobs para el nightly runner.")

tab_new, tab_json, tab_queue = st.tabs(["Nuevo render", "JSON avanzado", "Cola y storage"])
with tab_new:
    _new_render_tab()
with tab_json:
    _advanced_json_tab()
with tab_queue:
    _queue_storage_tab()
