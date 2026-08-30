"""Reusable Kurukin job spec to MoneyPrinterTurbo payload adapter."""

from __future__ import annotations

import json
import math
from pathlib import Path
import subprocess
from typing import Any

from app.custom import subtitle_style_presets


DEFAULT_LOCAL_VIDEOS_DIR = "storage/local_videos"
DEFAULT_LOCAL_AUDIOS_DIR = "storage/local_audios"
DEFAULT_LOCAL_SUBTITLES_DIR = "storage/local_subtitles"
DEFAULT_FONTS_DIR = "resource/fonts"
DEFAULT_ASSET_HUB_JOB_ASSETS_DIR = "/data/job-assets"
ALLOWED_EXTENSIONS = {"mp4", "mov", "avi", "flv", "mkv", "jpg", "jpeg", "png"}
ALLOWED_AUDIO_EXTENSIONS = {"mp3", "wav", "m4a", "aac", "flac", "ogg"}
ALLOWED_SUBTITLE_EXTENSIONS = {"srt"}
ALLOWED_VIDEO_ASPECTS = {"9:16", "16:9"}
ALLOWED_SUBTITLE_MODES = {"whisper", "edge", "custom_srt", "none"}
ALLOWED_SUBTITLE_PROVIDERS = {"whisper", "edge"}
ALLOWED_ASSET_HUB_SCENE_MODES = {"ordered"}
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_KURUKIN_TASKS_DIR = PROJECT_ROOT.parent / "storage" / "tasks"
RENDER_QUALITY_ALIASES = {
    "draft": "draft_720p",
    "draft_720p": "draft_720p",
    "720p": "draft_720p",
    "standard": "standard_1080p",
    "standard_1080p": "standard_1080p",
    "1080p": "standard_1080p",
    "premium": "premium_2k",
    "premium_2k": "premium_2k",
    "2k": "premium_2k",
    "1440p": "premium_2k",
}
IMAGE_EXTENSIONS = {"jpg", "jpeg", "png"}
IMAGE_MOTION_PRESETS = {
    "none",
    "slow_zoom_in",
    "slow_zoom_out",
    "pan_left",
    "pan_right",
    "pan_up",
    "pan_down",
    "subtle_pulse",
    "handheld_soft",
}
IMAGE_MOTION_ALIASES = {
    "zoom_in": "slow_zoom_in",
    "zoom_out": "slow_zoom_out",
    "ken_burns": "slow_zoom_in",
    "pulse": "subtle_pulse",
    "handheld": "handheld_soft",
}
DEFAULT_IMAGE_MOTION_INTENSITY = 0.06
MAX_IMAGE_MOTION_INTENSITY = 0.20


class KurukinJobAdapterError(Exception):
    """Expected Kurukin job spec validation error."""


LocalJobWrapperError = KurukinJobAdapterError


def load_kurukin_job_spec(path: str | Path) -> dict[str, Any]:
    job_path = Path(path)
    try:
        with job_path.open("r", encoding="utf-8") as handle:
            spec = json.load(handle)
    except FileNotFoundError as exc:
        raise KurukinJobAdapterError(f"job spec not found: {job_path}") from exc
    except json.JSONDecodeError as exc:
        raise KurukinJobAdapterError(f"job spec is not valid JSON: {exc}") from exc

    if not isinstance(spec, dict):
        raise KurukinJobAdapterError("job spec must be a JSON object")
    return spec


load_job_spec = load_kurukin_job_spec


def validate_asset_filename(filename: Any) -> str:
    if not isinstance(filename, str) or not filename.strip():
        raise LocalJobWrapperError("asset file is required and must be non-empty")

    value = filename.strip()
    if Path(value).is_absolute() or "/" in value or "\\" in value:
        raise LocalJobWrapperError(f"asset file must be a filename only: {filename!r}")
    if value in {".", ".."} or ".." in Path(value).parts:
        raise LocalJobWrapperError(f"asset file cannot use parent paths: {filename!r}")
    if Path(value).name != value:
        raise LocalJobWrapperError(f"asset file must be a filename only: {filename!r}")

    extension = Path(value).suffix.lower().lstrip(".")
    if extension not in ALLOWED_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_EXTENSIONS))
        raise LocalJobWrapperError(
            f"asset file has unsupported extension {extension!r}; allowed: {allowed}"
        )
    return value


def validate_local_filename(
    filename: Any,
    *,
    allowed_extensions: set[str],
    label: str,
) -> str:
    if not isinstance(filename, str) or not filename.strip():
        raise LocalJobWrapperError(f"{label} file is required and must be non-empty")

    value = filename.strip()
    if Path(value).is_absolute() or "/" in value or "\\" in value:
        raise LocalJobWrapperError(f"{label} file must be a filename only: {filename!r}")
    if value in {".", ".."} or ".." in Path(value).parts:
        raise LocalJobWrapperError(f"{label} file cannot use parent paths: {filename!r}")
    if Path(value).name != value:
        raise LocalJobWrapperError(f"{label} file must be a filename only: {filename!r}")

    extension = Path(value).suffix.lower().lstrip(".")
    if extension not in allowed_extensions:
        allowed = ", ".join(f".{item}" for item in sorted(allowed_extensions))
        raise LocalJobWrapperError(
            f"{label} file has unsupported extension {extension!r}; allowed: {allowed}"
        )
    return value


def resolve_local_asset(filename: str, local_videos_dir: str | Path) -> Path:
    safe_filename = validate_asset_filename(filename)
    base_dir = Path(local_videos_dir).resolve()
    candidate = base_dir / safe_filename
    if not candidate.exists():
        raise LocalJobWrapperError(f"local asset does not exist: {candidate}")

    resolved = candidate.resolve()
    try:
        resolved.relative_to(base_dir)
    except ValueError as exc:
        raise LocalJobWrapperError(
            f"local asset resolves outside local videos dir: {safe_filename}"
        ) from exc

    if not resolved.is_file():
        raise LocalJobWrapperError(f"local asset is not a file: {candidate}")
    return resolved


def resolve_local_file(
    filename: Any,
    local_dir: str | Path,
    *,
    allowed_extensions: set[str],
    label: str,
) -> Path:
    safe_filename = validate_local_filename(
        filename,
        allowed_extensions=allowed_extensions,
        label=label,
    )
    base_dir = Path(local_dir).resolve()
    candidate = base_dir / safe_filename
    if not candidate.exists():
        raise LocalJobWrapperError(f"{label} file does not exist: {candidate}")

    resolved = candidate.resolve()
    try:
        resolved.relative_to(base_dir)
    except ValueError as exc:
        raise LocalJobWrapperError(
            f"{label} file resolves outside local directory: {safe_filename}"
        ) from exc

    if not resolved.is_file():
        raise LocalJobWrapperError(f"{label} file is not a file: {candidate}")
    return resolved


def _payload_path_for_resolved_file(resolved_file: Path) -> str:
    try:
        return resolved_file.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(resolved_file.resolve())


def _server_side_path(payload_path: str) -> Path:
    candidate = Path(payload_path)
    if candidate.is_absolute():
        return candidate.resolve()
    return (PROJECT_ROOT / candidate).resolve()


def _ensure_legacy_field_matches(
    *,
    field_name: str,
    generated_value: str,
    legacy_value: Any,
) -> None:
    if legacy_value in (None, ""):
        return
    if not isinstance(legacy_value, str):
        raise LocalJobWrapperError(f"video.{field_name} must be a string when provided")

    generated_path = _server_side_path(generated_value)
    legacy_path = _server_side_path(legacy_value.strip())
    if generated_path != legacy_path:
        raise LocalJobWrapperError(
            f"top-level file conflicts with video.{field_name}: "
            f"{generated_value!r} != {legacy_value!r}"
        )


def _optional_bool(value: Any, *, default: bool, label: str) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    raise LocalJobWrapperError(f"{label} must be a boolean")


def normalize_asset_hub_manifest_path(
    value: Any,
    *,
    base_dir: str | Path | None = None,
    label: str,
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LocalJobWrapperError(f"{label} is required and must be a string")

    base = Path(base_dir or DEFAULT_ASSET_HUB_JOB_ASSETS_DIR).resolve()
    requested = Path(value.strip())
    candidate = requested if requested.is_absolute() else base / requested
    resolved = candidate.resolve(strict=False)
    task_root = DEFAULT_KURUKIN_TASKS_DIR.resolve()
    try:
        resolved.relative_to(base)
    except ValueError:
        # Asset Hub materializations are immutable inputs.  Kurukin's rebuilt
        # renderer manifest is the one approved derived artifact allowed here.
        try:
            resolved.relative_to(task_root)
        except ValueError as exc:
            raise LocalJobWrapperError(
                f"{label} must stay under {base} or {task_root}"
            ) from exc

    if resolved.suffix.lower() != ".json":
        raise LocalJobWrapperError(f"{label} must point to a .json file")
    if resolved.exists() and not resolved.is_file():
        raise LocalJobWrapperError(f"{label} exists but is not a file: {resolved}")
    return resolved.as_posix()


def normalize_asset_hub_scene_mode(value: Any, *, label: str) -> str:
    if value in (None, ""):
        return "ordered"
    if not isinstance(value, str):
        raise LocalJobWrapperError(f"{label} must be a string when provided")
    mode = value.strip()
    if mode not in ALLOWED_ASSET_HUB_SCENE_MODES:
        raise LocalJobWrapperError(f"{label} only supports ordered for MVP")
    return mode


def normalize_asset_hub_bundle_uid(value: Any, *, label: str) -> str:
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        raise LocalJobWrapperError(f"{label} must be a string when provided")
    return value.strip()


def _normalize_optional_subtitle_provider(value: Any, *, label: str) -> str:
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        raise LocalJobWrapperError(f"{label} must be a string when provided")

    provider = value.strip().lower()
    if not provider:
        return ""
    if provider not in ALLOWED_SUBTITLE_PROVIDERS:
        raise LocalJobWrapperError(
            f"{label} must be one of: edge, whisper"
        )
    return provider


def _normalize_optional_subtitle_mode(value: Any) -> str:
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        raise LocalJobWrapperError("subtitles.mode must be a string when provided")

    mode = value.strip().lower()
    if not mode:
        return ""
    if mode not in ALLOWED_SUBTITLE_MODES:
        raise LocalJobWrapperError(
            "subtitles.mode must be one of: whisper, edge, custom_srt, none"
        )
    return mode


def _subtitle_provider_from_mode(mode: str) -> str:
    if mode in {"edge", "whisper"}:
        return mode
    return ""


def _ensure_subtitle_provider_compatible(
    *,
    mode: str,
    provider: str,
    legacy_provider: str,
) -> str:
    mode_provider = _subtitle_provider_from_mode(mode)
    effective_provider = provider or mode_provider

    if mode_provider and provider and mode_provider != provider:
        raise LocalJobWrapperError(
            f"subtitles.mode={mode!r} conflicts with subtitles.provider={provider!r}"
        )

    if effective_provider and legacy_provider and effective_provider != legacy_provider:
        raise LocalJobWrapperError(
            "top-level subtitles provider conflicts with video.subtitle_provider: "
            f"{effective_provider!r} != {legacy_provider!r}"
        )

    return effective_provider or legacy_provider


def normalize_render_quality(value: Any, *, label: str) -> str:
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        raise LocalJobWrapperError(f"{label} must be a string when provided")

    normalized = value.strip().lower()
    if not normalized:
        return ""
    try:
        return RENDER_QUALITY_ALIASES[normalized]
    except KeyError as exc:
        raise LocalJobWrapperError(
            f"{label} must be one of: 720p, 1080p, 2k"
        ) from exc


def normalize_image_motion_preset(value: Any, *, label: str) -> str:
    if value in (None, ""):
        return "none"
    if not isinstance(value, str):
        raise LocalJobWrapperError(f"{label} must be a string when provided")

    normalized = value.strip().lower()
    if not normalized:
        return "none"
    normalized = IMAGE_MOTION_ALIASES.get(normalized, normalized)
    if normalized not in IMAGE_MOTION_PRESETS:
        raise LocalJobWrapperError(
            f"Unsupported image motion preset {value!r}."
        )
    return normalized


def normalize_image_motion_intensity(value: Any, *, label: str) -> float:
    if value in (None, ""):
        return DEFAULT_IMAGE_MOTION_INTENSITY
    if isinstance(value, bool):
        raise LocalJobWrapperError(f"{label} must be a number between 0.0 and 0.20")
    try:
        intensity = float(value)
    except (TypeError, ValueError) as exc:
        raise LocalJobWrapperError(
            f"{label} must be a number between 0.0 and 0.20"
        ) from exc
    if not math.isfinite(intensity):
        raise LocalJobWrapperError(f"{label} must be a finite number")
    if intensity < 0.0 or intensity > MAX_IMAGE_MOTION_INTENSITY:
        raise LocalJobWrapperError(f"{label} must be between 0.0 and 0.20")
    return intensity


def is_image_asset(filename: str) -> bool:
    return Path(filename).suffix.lower().lstrip(".") in IMAGE_EXTENSIONS


def normalize_image_motion_config(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise LocalJobWrapperError("image_motion must be a JSON object when provided")

    enabled = _optional_bool(
        value.get("enabled"),
        default=False,
        label="image_motion.enabled",
    )
    normalized = {"enabled": enabled}
    if "preset" in value or enabled:
        normalized["preset"] = normalize_image_motion_preset(
            value.get("preset"),
            label="image_motion.preset",
        )
    if "intensity" in value or enabled:
        normalized["intensity"] = normalize_image_motion_intensity(
            value.get("intensity"),
            label="image_motion.intensity",
        )
    return normalized


def normalize_asset_motion(asset: dict[str, Any], index: int) -> dict[str, Any]:
    normalized = dict(asset)
    if "motion" in normalized:
        normalized["motion"] = normalize_image_motion_preset(
            normalized.get("motion"),
            label=f"selectedAssets[{index}].motion",
        )
    if "motion_intensity" in normalized:
        normalized["motion_intensity"] = normalize_image_motion_intensity(
            normalized.get("motion_intensity"),
            label=f"selectedAssets[{index}].motion_intensity",
        )
    return normalized


def apply_render_quality_contract(
    pending_job: dict[str, Any],
    spec: dict[str, Any],
) -> None:
    top_level_quality = normalize_render_quality(
        spec.get("render_quality"),
        label="render_quality",
    )
    video_resolution = normalize_render_quality(
        pending_job.get("video_resolution"),
        label="video.video_resolution",
    )

    if top_level_quality and video_resolution and top_level_quality != video_resolution:
        raise LocalJobWrapperError(
            "render_quality conflicts with video.video_resolution: "
            f"{top_level_quality!r} != {video_resolution!r}"
        )

    effective_quality = top_level_quality or video_resolution
    if not effective_quality:
        pending_job.pop("render_quality", None)
        return

    pending_job["video_resolution"] = effective_quality
    pending_job.pop("render_quality", None)
    if top_level_quality:
        pending_job["runner"]["render_quality"] = effective_quality


def apply_image_motion_contract(
    pending_job: dict[str, Any],
    spec: dict[str, Any],
) -> None:
    image_motion = normalize_image_motion_config(spec.get("image_motion"))
    if image_motion is None:
        return

    pending_job["runner"]["image_motion"] = image_motion
    if not image_motion.get("enabled", False):
        return

    pending_job["image_motion_enabled"] = True
    pending_job["image_motion_preset"] = image_motion.get("preset", "none")
    pending_job["image_motion_intensity"] = image_motion.get(
        "intensity",
        DEFAULT_IMAGE_MOTION_INTENSITY,
    )


def normalize_asset_hub_config(
    asset_hub: Any,
    *,
    pending_job: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if asset_hub is not None and not isinstance(asset_hub, dict):
        raise LocalJobWrapperError("asset_hub must be a JSON object when provided")

    asset_hub = asset_hub or {}
    pending_job = pending_job or {}
    top_level_path = asset_hub.get("renderer_manifest_path")
    legacy_path = pending_job.get("asset_hub_renderer_manifest_path")
    if not top_level_path and not legacy_path:
        return None

    normalized_path = normalize_asset_hub_manifest_path(
        top_level_path or legacy_path,
        label=(
            "asset_hub.renderer_manifest_path"
            if top_level_path
            else "video.asset_hub_renderer_manifest_path"
        ),
    )
    if top_level_path and legacy_path:
        normalized_legacy_path = normalize_asset_hub_manifest_path(
            legacy_path,
            label="video.asset_hub_renderer_manifest_path",
        )
        if normalized_path != normalized_legacy_path:
            raise LocalJobWrapperError(
                "asset_hub.renderer_manifest_path conflicts with "
                "video.asset_hub_renderer_manifest_path"
            )

    bundle_uid = normalize_asset_hub_bundle_uid(
        asset_hub.get("bundle_uid", pending_job.get("asset_hub_bundle_uid")),
        label="asset_hub.bundle_uid",
    )
    scene_mode = normalize_asset_hub_scene_mode(
        asset_hub.get("scene_mode", pending_job.get("asset_hub_scene_mode")),
        label="asset_hub.scene_mode",
    )
    strict = _optional_bool(
        asset_hub.get("strict", pending_job.get("asset_hub_strict")),
        default=True,
        label="asset_hub.strict",
    )
    return {
        "renderer_manifest_path": normalized_path,
        "bundle_uid": bundle_uid,
        "scene_mode": scene_mode,
        "strict": strict,
    }


def apply_asset_hub_contract(
    pending_job: dict[str, Any],
    spec: dict[str, Any],
) -> bool:
    asset_hub_config = normalize_asset_hub_config(
        spec.get("asset_hub"),
        pending_job=pending_job,
    )
    if asset_hub_config is None:
        return False

    pending_job["asset_hub_renderer_manifest_path"] = asset_hub_config[
        "renderer_manifest_path"
    ]
    pending_job["asset_hub_bundle_uid"] = asset_hub_config["bundle_uid"]
    pending_job["asset_hub_scene_mode"] = asset_hub_config["scene_mode"]
    pending_job["asset_hub_strict"] = asset_hub_config["strict"]
    pending_job["video_source"] = "local"
    pending_job.pop("video_materials", None)
    pending_job["runner"]["asset_hub"] = dict(asset_hub_config)
    return True


def apply_audio_contract(
    pending_job: dict[str, Any],
    spec: dict[str, Any],
    *,
    local_audios_dir: str | Path,
) -> None:
    audio = spec.get("audio")
    if audio is None:
        return
    if not isinstance(audio, dict):
        raise LocalJobWrapperError("audio must be a JSON object when provided")

    if "file" not in audio:
        raise LocalJobWrapperError("audio.file is required when audio is provided")

    resolved_audio = resolve_local_file(
        audio.get("file"),
        local_audios_dir,
        allowed_extensions=ALLOWED_AUDIO_EXTENSIONS,
        label="audio",
    )
    payload_path = _payload_path_for_resolved_file(resolved_audio)
    _ensure_legacy_field_matches(
        field_name="custom_audio_file",
        generated_value=payload_path,
        legacy_value=pending_job.get("custom_audio_file"),
    )
    pending_job["custom_audio_file"] = payload_path
    pending_job["runner"]["audio"] = {
        "file": resolved_audio.name,
        "custom_audio_file": payload_path,
    }


def apply_subtitle_contract(
    pending_job: dict[str, Any],
    spec: dict[str, Any],
    *,
    local_subtitles_dir: str | Path,
) -> None:
    subtitles = spec.get("subtitles")
    if subtitles is None:
        return
    if not isinstance(subtitles, dict):
        raise LocalJobWrapperError("subtitles must be a JSON object when provided")

    mode = _normalize_optional_subtitle_mode(subtitles.get("mode"))
    provider = _normalize_optional_subtitle_provider(
        subtitles.get("provider"),
        label="subtitles.provider",
    )
    legacy_provider = _normalize_optional_subtitle_provider(
        pending_job.get("subtitle_provider"),
        label="video.subtitle_provider",
    )
    if not mode and not provider:
        raise LocalJobWrapperError(
            "subtitles.mode or subtitles.provider is required when subtitles is provided"
        )

    effective_provider = _ensure_subtitle_provider_compatible(
        mode=mode,
        provider=provider,
        legacy_provider=legacy_provider,
    )

    runner_metadata = {}
    if mode:
        runner_metadata["mode"] = mode
    if provider:
        runner_metadata["provider"] = provider
    if mode == "none":
        pending_job["subtitle_enabled"] = False
        pending_job["runner"]["subtitles"] = runner_metadata
        return

    if mode == "custom_srt":
        resolved_subtitle = resolve_local_file(
            subtitles.get("file"),
            local_subtitles_dir,
            allowed_extensions=ALLOWED_SUBTITLE_EXTENSIONS,
            label="subtitle",
        )
        payload_path = _payload_path_for_resolved_file(resolved_subtitle)
        _ensure_legacy_field_matches(
            field_name="custom_subtitle_file",
            generated_value=payload_path,
            legacy_value=pending_job.get("custom_subtitle_file"),
        )

        pending_job["subtitle_enabled"] = True
        pending_job["custom_subtitle_file"] = payload_path
        pending_job["subtitle_correction_enabled"] = False
        pending_job["subtitle_optimization_enabled"] = _optional_bool(
            subtitles.get("optimize"),
            default=True,
            label="subtitles.optimize",
        )
        runner_metadata["file"] = resolved_subtitle.name
        runner_metadata["custom_subtitle_file"] = payload_path
        runner_metadata["correction_enabled"] = False
        runner_metadata["optimize"] = pending_job["subtitle_optimization_enabled"]
        pending_job["runner"]["subtitles"] = runner_metadata
        return

    if effective_provider == "edge" and pending_job.get("custom_audio_file"):
        raise LocalJobWrapperError(
            "Edge subtitles require generated TTS audio. Use "
            "subtitles.mode='whisper', subtitles.mode='custom_srt', or "
            "subtitles.mode='none' with custom audio."
        )

    if effective_provider == "whisper":
        pending_job["subtitle_enabled"] = True
        pending_job["subtitle_provider"] = "whisper"
        pending_job["subtitle_correction_enabled"] = _optional_bool(
            subtitles.get("correction_enabled"),
            default=False,
            label="subtitles.correction_enabled",
        )
        pending_job["subtitle_optimization_enabled"] = _optional_bool(
            subtitles.get("optimize"),
            default=True,
            label="subtitles.optimize",
        )
        runner_metadata["correction_enabled"] = pending_job[
            "subtitle_correction_enabled"
        ]
        runner_metadata["optimize"] = pending_job["subtitle_optimization_enabled"]
        pending_job["runner"]["subtitles"] = runner_metadata
        return

    if effective_provider == "edge":
        pending_job["subtitle_enabled"] = True
        pending_job["subtitle_provider"] = "edge"
        pending_job["subtitle_optimization_enabled"] = _optional_bool(
            subtitles.get("optimize"),
            default=True,
            label="subtitles.optimize",
        )
        runner_metadata["optimize"] = pending_job["subtitle_optimization_enabled"]
        pending_job["runner"]["subtitles"] = runner_metadata
        return

    raise LocalJobWrapperError(
        "subtitles.mode must be one of: whisper, edge, custom_srt, none"
    )


def probe_media_dimensions(path: str | Path) -> tuple[int, int]:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height",
        "-of",
        "json",
        str(path),
    ]
    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise LocalJobWrapperError(
            "ffprobe was not found; install ffmpeg or use --skip-media-probe"
        ) from exc
    except subprocess.CalledProcessError as exc:
        details = (exc.stderr or exc.stdout or "").strip()
        suffix = f": {details}" if details else ""
        raise LocalJobWrapperError(f"ffprobe failed for {path}{suffix}") from exc

    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise LocalJobWrapperError(f"ffprobe returned invalid JSON for {path}") from exc

    streams = payload.get("streams")
    if not isinstance(streams, list) or not streams:
        raise LocalJobWrapperError(f"ffprobe found no visual stream for {path}")

    stream = streams[0]
    width = stream.get("width")
    height = stream.get("height")
    if not isinstance(width, int) or not isinstance(height, int):
        raise LocalJobWrapperError(f"ffprobe did not return width/height for {path}")
    return width, height


def _order_value(asset: dict[str, Any], index: int) -> tuple[int, float, int]:
    if "order" not in asset:
        return (1, 0, index)
    value = asset["order"]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LocalJobWrapperError("asset order must be a number when provided")
    return (0, float(value), index)


def _ordered_assets(assets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not any("order" in asset for asset in assets):
        return list(assets)
    return [
        asset
        for _, asset in sorted(
            (( _order_value(asset, index), asset) for index, asset in enumerate(assets)),
            key=lambda item: item[0],
        )
    ]


def build_runner_metadata(
    *,
    ordered_assets: list[dict[str, Any]],
    source: str = "local_job_wrapper",
) -> dict[str, Any]:
    return {
        "source": source,
        "selectedAssets": ordered_assets,
    }


def build_video_materials_from_selected_assets(
    ordered_assets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    video_materials = []
    for asset in ordered_assets:
        material = {
            "provider": "local",
            "url": asset["file"],
            "duration": 0,
        }
        if is_image_asset(asset["file"]):
            if asset.get("motion"):
                material["motion"] = asset["motion"]
            if "motion_intensity" in asset:
                material["motion_intensity"] = asset["motion_intensity"]
        video_materials.append(material)
    return video_materials


def validate_job_spec(
    spec: dict[str, Any],
    local_videos_dir: str | Path = DEFAULT_LOCAL_VIDEOS_DIR,
    min_width: int = 480,
    min_height: int = 480,
    skip_media_probe: bool = False,
) -> list[dict[str, Any]]:
    if not isinstance(spec, dict):
        raise LocalJobWrapperError("job spec must be a JSON object")

    job_id = spec.get("job_id")
    if not isinstance(job_id, str) or not job_id.strip():
        raise LocalJobWrapperError("job_id is required and must be a non-empty string")

    video = spec.get("video")
    if not isinstance(video, dict):
        raise LocalJobWrapperError("video is required and must be a JSON object")

    for key in ("video_subject", "video_script"):
        value = video.get(key)
        if not isinstance(value, str) or not value.strip():
            raise LocalJobWrapperError(f"video.{key} is required")

    video_aspect = video.get("video_aspect")
    if video_aspect not in ALLOWED_VIDEO_ASPECTS:
        raise LocalJobWrapperError('video.video_aspect must be "9:16" or "16:9"')

    asset_hub = spec.get("asset_hub")
    has_asset_hub_manifest = (
        isinstance(asset_hub, dict) and bool(asset_hub.get("renderer_manifest_path"))
    ) or bool(video.get("asset_hub_renderer_manifest_path"))

    assets = spec.get("selectedAssets")
    if assets is None:
        if has_asset_hub_manifest:
            return []
        raise LocalJobWrapperError("selectedAssets must be a non-empty list")
    if not isinstance(assets, list) or (not assets and not has_asset_hub_manifest):
        raise LocalJobWrapperError("selectedAssets must be a non-empty list")

    normalized_assets: list[dict[str, Any]] = []
    seen_files: set[str] = set()
    for index, asset in enumerate(assets):
        if not isinstance(asset, dict):
            raise LocalJobWrapperError(
                f"selectedAssets[{index}] must be a JSON object"
            )
        filename = validate_asset_filename(asset.get("file"))
        if filename in seen_files:
            raise LocalJobWrapperError(f"duplicate selected asset file: {filename}")
        seen_files.add(filename)

        resolved_path = resolve_local_asset(filename, local_videos_dir)
        if not skip_media_probe:
            width, height = probe_media_dimensions(resolved_path)
            if width < min_width or height < min_height:
                raise LocalJobWrapperError(
                    f"local asset is too small: {filename} "
                    f"({width}x{height}, minimum {min_width}x{min_height})"
                )

        normalized = normalize_asset_motion(asset, index)
        normalized["file"] = filename
        normalized_assets.append(normalized)

    return _ordered_assets(normalized_assets)


def build_pending_job(
    spec: dict[str, Any],
    ordered_assets: list[dict[str, Any]],
    fonts_dir: str | Path = DEFAULT_FONTS_DIR,
    local_audios_dir: str | Path = DEFAULT_LOCAL_AUDIOS_DIR,
    local_subtitles_dir: str | Path = DEFAULT_LOCAL_SUBTITLES_DIR,
) -> dict[str, Any]:
    video = spec.get("video")
    if not isinstance(video, dict):
        raise LocalJobWrapperError("video is required and must be a JSON object")

    preset_requested = spec.get("subtitle_style_preset")
    overrides_requested = spec.get("subtitle_style_overrides")
    try:
        resolved_preset, normalized_overrides, resolved_style = (
            subtitle_style_presets.resolve_subtitle_style(
                preset_requested,
                overrides_requested,
                fonts_dir=fonts_dir,
            )
        )
    except subtitle_style_presets.SubtitleStylePresetError as exc:
        raise LocalJobWrapperError(str(exc)) from exc

    styled_video = dict(video)
    if resolved_style:
        styled_video.update(resolved_style)

    pending_job: dict[str, Any] = {}
    job_id = spec.get("job_id")
    description = spec.get("description")
    if job_id is not None:
        pending_job["job_id"] = job_id
    if description is not None:
        pending_job["description"] = description

    pending_job["runner"] = build_runner_metadata(ordered_assets=ordered_assets)
    if preset_requested is not None or overrides_requested is not None:
        pending_job["runner"]["subtitle_style_preset"] = resolved_preset
        pending_job["runner"]["subtitle_style_overrides"] = normalized_overrides
        pending_job["runner"]["resolved_subtitle_style"] = resolved_style or {}

    pending_job.update(styled_video)
    pending_job["video_source"] = "local"
    pending_job["video_materials"] = build_video_materials_from_selected_assets(
        ordered_assets
    )
    pending_job.pop("selectedAssets", None)
    pending_job.pop("asset_hub", None)
    pending_job.pop("subtitle_style_preset", None)
    pending_job.pop("subtitle_style_overrides", None)
    apply_asset_hub_contract(pending_job, spec)
    apply_render_quality_contract(pending_job, spec)
    apply_image_motion_contract(pending_job, spec)
    apply_audio_contract(
        pending_job,
        spec,
        local_audios_dir=local_audios_dir,
    )
    apply_subtitle_contract(
        pending_job,
        spec,
        local_subtitles_dir=local_subtitles_dir,
    )
    return pending_job


def build_moneyprinter_payload(
    spec: dict[str, Any],
    *,
    local_videos_dir: str | Path = DEFAULT_LOCAL_VIDEOS_DIR,
    local_audios_dir: str | Path = DEFAULT_LOCAL_AUDIOS_DIR,
    local_subtitles_dir: str | Path = DEFAULT_LOCAL_SUBTITLES_DIR,
    min_width: int = 480,
    min_height: int = 480,
    media_probe: bool = True,
    fonts_dir: str | Path = DEFAULT_FONTS_DIR,
) -> dict[str, Any]:
    ordered_assets = validate_job_spec(
        spec,
        local_videos_dir=local_videos_dir,
        min_width=min_width,
        min_height=min_height,
        skip_media_probe=not media_probe,
    )
    return build_pending_job(
        spec,
        ordered_assets,
        fonts_dir=fonts_dir,
        local_audios_dir=local_audios_dir,
        local_subtitles_dir=local_subtitles_dir,
    )


def validate_kurukin_job_spec(
    spec: dict[str, Any],
    *,
    local_videos_dir: str | Path = DEFAULT_LOCAL_VIDEOS_DIR,
    local_audios_dir: str | Path = DEFAULT_LOCAL_AUDIOS_DIR,
    local_subtitles_dir: str | Path = DEFAULT_LOCAL_SUBTITLES_DIR,
    min_width: int = 480,
    min_height: int = 480,
    media_probe: bool = True,
    fonts_dir: str | Path = DEFAULT_FONTS_DIR,
) -> None:
    build_moneyprinter_payload(
        spec,
        local_videos_dir=local_videos_dir,
        local_audios_dir=local_audios_dir,
        local_subtitles_dir=local_subtitles_dir,
        min_width=min_width,
        min_height=min_height,
        media_probe=media_probe,
        fonts_dir=fonts_dir,
    )


def summarize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    runner = payload.get("runner")
    if not isinstance(runner, dict):
        runner = {}
    materials = payload.get("video_materials")
    return {
        "job_id": payload.get("job_id"),
        "video_subject": payload.get("video_subject"),
        "video_source": payload.get("video_source"),
        "video_resolution": payload.get("video_resolution"),
        "asset_hub_bundle_uid": payload.get("asset_hub_bundle_uid"),
        "has_custom_audio": bool(payload.get("custom_audio_file")),
        "subtitle_enabled": payload.get("subtitle_enabled"),
        "subtitle_provider": payload.get("subtitle_provider"),
        "custom_subtitle_file": payload.get("custom_subtitle_file"),
        "image_motion_enabled": bool(payload.get("image_motion_enabled")),
        "material_count": len(materials) if isinstance(materials, list) else 0,
        "runner_keys": sorted(runner.keys()),
    }


normalize_subtitle_mode = _normalize_optional_subtitle_mode
normalize_subtitle_provider = _normalize_optional_subtitle_provider
normalize_image_motion = normalize_image_motion_config
resolve_safe_storage_file = resolve_local_file
