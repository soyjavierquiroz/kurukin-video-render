"""Pure helpers for the Kurukin Render Console Streamlit page."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any

from app.custom.asset_hub_manifest import (
    load_asset_hub_renderer_manifest,
    summarize_asset_hub_manifest,
    validate_asset_hub_renderer_manifest,
)
from app.custom.kurukin_job_adapter import (
    ALLOWED_AUDIO_EXTENSIONS,
    ALLOWED_SUBTITLE_EXTENSIONS,
    ALLOWED_EXTENSIONS,
    DEFAULT_LOCAL_AUDIOS_DIR,
    DEFAULT_LOCAL_SUBTITLES_DIR,
    DEFAULT_LOCAL_VIDEOS_DIR,
    build_moneyprinter_payload,
    summarize_payload,
    validate_asset_filename,
    validate_local_filename,
)


DEFAULT_VOICE_NAME = "es-MX-DaliaNeural-Female"
SOURCE_MODE_ASSET_HUB = "asset_hub_bundle"
SOURCE_MODE_LOCAL = "local_assets"
SOURCE_MODE_STOCK = "stock_external"
ASSET_SOURCE_ASSET_HUB = SOURCE_MODE_ASSET_HUB
ASSET_SOURCE_LOCAL = SOURCE_MODE_LOCAL
ASSET_SOURCE_STOCK = SOURCE_MODE_STOCK
STOCK_SOURCES = {"pexels", "pixabay", "coverr"}


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


derive_manifest_path = default_asset_hub_manifest_path


def safe_relative_path(
    value: str,
    *,
    allowed_extensions: set[str],
    label: str,
) -> str:
    """Validate a UI-supplied storage filename without resolving filesystem state."""

    if allowed_extensions == ALLOWED_EXTENSIONS:
        return validate_asset_filename(value)
    return validate_local_filename(
        value,
        allowed_extensions=allowed_extensions,
        label=label,
    )


def list_local_storage_files(
    directory: str | Path,
    *,
    allowed_extensions: set[str],
) -> list[str]:
    """List safe filenames already present in a local storage directory."""

    base_dir = Path(directory)
    if not base_dir.exists() or not base_dir.is_dir():
        return []

    filenames = []
    for path in sorted(base_dir.iterdir(), key=lambda item: item.name.lower()):
        if not path.is_file():
            continue
        extension = path.suffix.lower().lstrip(".")
        if extension not in allowed_extensions:
            continue
        try:
            if allowed_extensions == ALLOWED_EXTENSIONS:
                filenames.append(validate_asset_filename(path.name))
            else:
                filenames.append(
                    validate_local_filename(
                        path.name,
                        allowed_extensions=allowed_extensions,
                        label="local",
                    )
                )
        except Exception:
            continue
    return filenames


def build_render_console_spec(
    *,
    job_id: str,
    video_subject: str,
    video_script: str,
    render_quality: str,
    video_aspect: str,
    asset_hub_bundle_uid: str = "",
    asset_hub_renderer_manifest_path: str = "",
    asset_source_mode: str = ASSET_SOURCE_ASSET_HUB,
    selected_local_assets: list[str] | None = None,
    stock_source: str = "pexels",
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

    source_mode = _clean_text(asset_source_mode) or ASSET_SOURCE_ASSET_HUB
    if source_mode not in {
        ASSET_SOURCE_ASSET_HUB,
        ASSET_SOURCE_LOCAL,
        ASSET_SOURCE_STOCK,
    }:
        raise ValueError("asset_source_mode is not supported")
    if source_mode == ASSET_SOURCE_STOCK:
        normalized_stock_source = _clean_text(stock_source).lower()
        if normalized_stock_source not in STOCK_SOURCES:
            raise ValueError("stock_source must be pexels, pixabay, or coverr")
        raise ValueError(
            "El modo Stock externo todavía requiere configuración desde la UI legacy "
            "de MoneyPrinterTurbo. Esta consola no modifica config.toml ni credenciales."
        )

    safe_bundle_uid = _validate_safe_bundle_uid(asset_hub_bundle_uid)
    manifest_path = _clean_text(asset_hub_renderer_manifest_path)
    if source_mode == ASSET_SOURCE_ASSET_HUB and safe_bundle_uid and not manifest_path:
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

    if source_mode == ASSET_SOURCE_LOCAL:
        assets = selected_local_assets or []
        if not assets:
            raise ValueError("Selecciona al menos un asset local para crear el trabajo.")
        spec["selectedAssets"] = [
            {
                "file": safe_relative_path(
                    item,
                    allowed_extensions=ALLOWED_EXTENSIONS,
                    label="asset",
                ),
                "order": index + 1,
            }
            for index, item in enumerate(assets)
        ]
        spec.pop("asset_hub", None)

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


def build_workflow_payload(state: dict[str, Any]) -> dict[str, Any]:
    """Build a MoneyPrinterTurbo payload from the guided workflow state."""

    spec = build_render_console_spec(
        job_id=state.get("job_id", ""),
        video_subject=state.get("video_subject", ""),
        video_script=state.get("video_script", ""),
        render_quality=state.get("render_quality", "draft_720p"),
        video_aspect=state.get("video_aspect", "9:16"),
        asset_source_mode=state.get("asset_source_mode", ASSET_SOURCE_ASSET_HUB),
        asset_hub_bundle_uid=state.get("asset_hub_bundle_uid", ""),
        asset_hub_renderer_manifest_path=state.get(
            "asset_hub_renderer_manifest_path",
            "",
        ),
        selected_local_assets=state.get("selected_local_assets") or [],
        stock_source=state.get("stock_source", "pexels"),
        audio_file=state.get("audio_file", ""),
        subtitles_mode=state.get("subtitles_mode", "none"),
        custom_subtitle_file=state.get("custom_subtitle_file", ""),
        subtitle_style_preset=state.get(
            "subtitle_style_preset",
            "clean_center_bold_safe",
        ),
        image_motion_enabled=bool(state.get("image_motion_enabled")),
        image_motion_preset=state.get("image_motion_preset", "slow_zoom_in"),
        image_motion_intensity=float(state.get("image_motion_intensity", 0.06)),
        video_clip_duration=int(state.get("video_clip_duration", 4)),
        n_threads=int(state.get("n_threads", 2)),
    )
    payload, _ = validate_and_build_payload_from_console_spec(spec)
    return payload


def validate_and_build_payload_from_console_spec(
    spec: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate a console spec through the adapter and return payload + summary."""

    payload = build_moneyprinter_payload(spec, media_probe=False)
    return payload, summarize_payload(payload)


def _safe_error_message(exc: Exception) -> str:
    return str(exc) or exc.__class__.__name__


def _iter_manifest_assets(manifest: dict[str, Any]):
    scenes = manifest.get("scenes")
    if not isinstance(scenes, list):
        return
    for scene in scenes:
        if not isinstance(scene, dict):
            continue
        for asset in scene.get("assets") or []:
            if isinstance(asset, dict):
                yield asset


def get_manifest_summary_for_ui(manifest_path: str) -> dict[str, Any]:
    """Return a safe, compact renderer manifest summary for operator UI."""

    clean_path = _clean_text(manifest_path)
    if not clean_path:
        return {
            "exists": False,
            "status": "missing_path",
            "message": "No manifest path provided",
        }

    if not Path(clean_path).exists():
        return {
            "exists": False,
            "status": "not_found",
            "message": "Manifest file not found",
        }

    try:
        manifest = load_asset_hub_renderer_manifest(clean_path)
        validate_asset_hub_renderer_manifest(manifest)
        base_summary = summarize_asset_hub_manifest(manifest)
    except Exception as exc:
        return {
            "exists": True,
            "status": "invalid",
            "message": _safe_error_message(exc),
        }

    asset_types: dict[str, int] = {}
    duration_total_seconds = 0.0
    preview_filenames = []
    for asset in _iter_manifest_assets(manifest):
        asset_type = asset.get("type")
        if isinstance(asset_type, str) and asset_type:
            asset_types[asset_type] = asset_types.get(asset_type, 0) + 1

        duration = asset.get("duration_seconds")
        if isinstance(duration, (int, float)) and not isinstance(duration, bool):
            duration_total_seconds += float(duration)

        filename = asset.get("filename")
        if isinstance(filename, str) and filename and len(preview_filenames) < 5:
            preview_filenames.append(filename)

    return {
        "exists": True,
        "status": "ready",
        "message": "Manifest ready",
        "bundle_uid": base_summary.get("bundle_uid"),
        "job_id": base_summary.get("job_id"),
        "total_scenes": base_summary.get("total_scenes", 0),
        "total_assets": base_summary.get("total_assets", 0),
        "warnings_count": base_summary.get("warnings_count", 0),
        "needs_human_review_count": base_summary.get(
            "needs_human_review_count",
            0,
        ),
        "safe_for_subtitles_false_count": base_summary.get(
            "safe_for_subtitles_false_count",
            0,
        ),
        "safe_for_text_overlay_false_count": base_summary.get(
            "safe_for_text_overlay_false_count",
            0,
        ),
        "asset_types": asset_types,
        "duration_total_seconds": round(duration_total_seconds, 2),
        "preview_filenames": preview_filenames,
    }


load_manifest_summary = get_manifest_summary_for_ui


def build_operator_summary(
    payload: dict[str, Any],
    manifest_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Adapt the safe payload summary into operator-friendly UI fields."""

    payload_summary = summarize_payload(payload)
    payload_material_count = int(payload_summary.get("material_count") or 0)
    manifest_asset_count = 0
    if isinstance(manifest_summary, dict):
        manifest_asset_count = int(manifest_summary.get("total_assets") or 0)

    has_asset_hub = bool(payload.get("asset_hub_renderer_manifest_path"))
    has_local_materials = isinstance(payload.get("video_materials"), list)
    if has_asset_hub:
        mode = "Asset Hub manifest"
    elif has_local_materials:
        mode = "Local selected assets"
    else:
        mode = "Unknown"

    summary = {
        "job_id": payload_summary.get("job_id"),
        "subject": payload_summary.get("video_subject"),
        "mode": mode,
        "render_quality": payload_summary.get("video_resolution"),
        "aspect": payload.get("video_aspect"),
        "subtitles": (
            payload_summary.get("subtitle_provider")
            or ("enabled" if payload_summary.get("subtitle_enabled") else "none")
        ),
        "audio": "custom" if payload_summary.get("has_custom_audio") else "generated",
        "image_motion": (
            "enabled" if payload_summary.get("image_motion_enabled") else "disabled"
        ),
        "bundle_uid": payload_summary.get("asset_hub_bundle_uid"),
        "payload_material_count": payload_material_count,
        "manifest_asset_count": manifest_asset_count,
        "note": "",
    }

    if has_asset_hub and payload_material_count == 0:
        summary["note"] = (
            "Los assets se resolverán desde el manifest cuando el worker "
            "inicie el render."
        )
    return summary
