"""Pure helpers for the Kurukin Render Console Streamlit page."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from app.custom.kurukin_job_adapter import (
    build_moneyprinter_payload,
    summarize_payload,
)


DEFAULT_VOICE_NAME = "es-MX-DaliaNeural-Female"


def _clean_text(value: str) -> str:
    return str(value or "").strip()


def _validate_safe_bundle_uid(bundle_uid: str) -> str:
    value = _clean_text(bundle_uid)
    if not value:
        return ""
    path = PurePosixPath(value)
    if path.is_absolute() or "/" in value or "\\" in value or ".." in path.parts:
        raise ValueError("bundle_uid cannot contain path separators or parent paths")
    return value


def default_asset_hub_manifest_path(bundle_uid: str) -> str:
    """Return the canonical Asset Hub renderer manifest path for a bundle uid."""

    safe_bundle_uid = _validate_safe_bundle_uid(bundle_uid)
    if not safe_bundle_uid:
        return ""
    return f"/data/job-assets/{safe_bundle_uid}/manifests/renderer-manifest.json"


def build_render_console_spec(
    *,
    job_id: str,
    video_subject: str,
    video_script: str,
    render_quality: str,
    video_aspect: str,
    asset_hub_bundle_uid: str = "",
    asset_hub_renderer_manifest_path: str = "",
    audio_file: str = "",
    subtitles_mode: str = "none",
    custom_subtitle_file: str = "",
    subtitle_style_preset: str = "clean_center_bold_safe",
    image_motion_enabled: bool = False,
    image_motion_preset: str = "slow_zoom_in",
    image_motion_intensity: float = 0.06,
    video_clip_duration: int = 4,
    n_threads: int = 2,
) -> dict[str, Any]:
    """Build a Kurukin Job Spec from Render Console form fields."""

    safe_bundle_uid = _validate_safe_bundle_uid(asset_hub_bundle_uid)
    manifest_path = _clean_text(asset_hub_renderer_manifest_path)
    if safe_bundle_uid and not manifest_path:
        manifest_path = default_asset_hub_manifest_path(safe_bundle_uid)

    subtitles_mode = _clean_text(subtitles_mode).lower() or "none"
    subtitles: dict[str, Any] = {"mode": subtitles_mode}
    if subtitles_mode == "custom_srt":
        subtitles["file"] = _clean_text(custom_subtitle_file)

    spec: dict[str, Any] = {
        "job_id": _clean_text(job_id),
        "description": "Render Console job",
        "render_quality": _clean_text(render_quality),
        "subtitle_style_preset": _clean_text(subtitle_style_preset),
        "subtitles": subtitles,
        "video": {
            "video_subject": _clean_text(video_subject),
            "video_script": _clean_text(video_script),
            "video_aspect": _clean_text(video_aspect),
            "video_concat_mode": "sequential",
            "video_transition_mode": "None",
            "video_clip_duration": int(video_clip_duration),
            "video_count": 1,
            "voice_name": DEFAULT_VOICE_NAME,
            "voice_volume": 1.0,
            "voice_rate": 1.0,
            "bgm_type": "none",
            "subtitle_enabled": subtitles_mode != "none",
            "n_threads": int(n_threads),
            "paragraph_number": 1,
        },
    }

    if manifest_path:
        spec["asset_hub"] = {
            "renderer_manifest_path": manifest_path,
            "bundle_uid": safe_bundle_uid,
            "scene_mode": "ordered",
            "strict": True,
        }

    clean_audio_file = _clean_text(audio_file)
    if clean_audio_file:
        spec["audio"] = {"file": clean_audio_file}

    if image_motion_enabled:
        spec["image_motion"] = {
            "enabled": True,
            "preset": _clean_text(image_motion_preset) or "slow_zoom_in",
            "intensity": float(image_motion_intensity),
        }

    return spec


def validate_and_build_payload_from_console_spec(
    spec: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate a console spec through the adapter and return payload + summary."""

    payload = build_moneyprinter_payload(spec, media_probe=False)
    return payload, summarize_payload(payload)
