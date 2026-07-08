"""Pure A-roll/B-roll mode helpers for the Kurukin Render Console."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path, PurePosixPath
from typing import Any


RENDER_MODE_AROLL_BROLL = "aroll_broll"
AROLL_BROLL_QUEUE_GUARD = "renderer_not_enabled"

LAYOUT_ALTERNATING_FULLSCREEN = "alternating_fullscreen"
LAYOUT_VERTICAL_SPLIT_A_TOP = "vertical_split_a_top"
LAYOUT_VERTICAL_SPLIT_B_TOP = "vertical_split_b_top"
LAYOUT_BROLL_FULLSCREEN_SPEAKER_BUBBLE = "broll_fullscreen_speaker_bubble"
LAYOUT_AROLL_MAIN_BROLL_LOWER_PANEL = "aroll_main_broll_lower_panel"

AROLL_AUDIO_ORIGINAL = "original"
BROLL_AUDIO_MUTED = "muted"

SUBTITLES_SOURCE_AROLL_AUDIO = "aroll_audio"
SUBTITLES_SOURCE_CUSTOM_SRT = "custom_srt"
SUBTITLES_SOURCE_NONE = "none"

SPEAKER_CROP_CENTER = "center"
SPEAKER_CROP_TOP = "top"
SPEAKER_CROP_BOTTOM = "bottom"

BROLL_SOURCE_ASSET_HUB_MANIFEST = "asset_hub_manifest"
BROLL_SOURCE_LOCAL_ASSETS = "local_assets"

DEFAULT_ASSET_HUB_JOB_ASSETS_DIR = Path("/data/job-assets")
LOCAL_VIDEO_ROOTS = ("storage/local_videos", "storage/local_assets")
LOCAL_SUBTITLE_ROOT = "storage/local_subtitles"

ALLOWED_LAYOUT_PRESETS = {
    LAYOUT_ALTERNATING_FULLSCREEN,
    LAYOUT_VERTICAL_SPLIT_A_TOP,
    LAYOUT_VERTICAL_SPLIT_B_TOP,
    LAYOUT_BROLL_FULLSCREEN_SPEAKER_BUBBLE,
    LAYOUT_AROLL_MAIN_BROLL_LOWER_PANEL,
}
ALLOWED_SUBTITLE_SOURCES = {
    SUBTITLES_SOURCE_AROLL_AUDIO,
    SUBTITLES_SOURCE_CUSTOM_SRT,
    SUBTITLES_SOURCE_NONE,
}
ALLOWED_CROPS = {
    SPEAKER_CROP_CENTER,
    SPEAKER_CROP_TOP,
    SPEAKER_CROP_BOTTOM,
}
ALLOWED_BROLL_SOURCES = {
    BROLL_SOURCE_ASSET_HUB_MANIFEST,
    BROLL_SOURCE_LOCAL_ASSETS,
}
ALLOWED_FREQUENCIES = {"low", "medium", "high"}
FREQUENCY_INTERVAL_SECONDS = {
    "low": 16.0,
    "medium": 10.0,
    "high": 6.0,
}


def build_default_aroll_broll_config() -> dict[str, Any]:
    """Return safe A-roll/B-roll defaults without touching the filesystem."""

    return {
        "render_mode": RENDER_MODE_AROLL_BROLL,
        "a_roll": {
            "source": "local_video",
            "path": "",
            "audio_policy": AROLL_AUDIO_ORIGINAL,
            "crop": SPEAKER_CROP_CENTER,
        },
        "b_roll": {
            "source": BROLL_SOURCE_ASSET_HUB_MANIFEST,
            "bundle_uid": "",
            "manifest_path": "",
            "clip_seconds": 4,
            "frequency": "medium",
            "audio_policy": BROLL_AUDIO_MUTED,
            "image_motion": "slow_zoom_in",
        },
        "layout": {
            "preset": LAYOUT_ALTERNATING_FULLSCREEN,
            "aspect_ratio": "9:16",
            "speaker_position": SPEAKER_CROP_CENTER,
            "subtitle_safe_area": True,
        },
        "subtitles": {
            "source": SUBTITLES_SOURCE_NONE,
            "provider": "none",
            "custom_srt_path": "",
        },
    }


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _merge_default_config(config: Any) -> dict[str, Any]:
    normalized = build_default_aroll_broll_config()
    if not isinstance(config, dict):
        return normalized

    for key, value in config.items():
        if isinstance(value, dict) and isinstance(normalized.get(key), dict):
            normalized[key].update(value)
        else:
            normalized[key] = value
    return normalized


def _has_path_traversal(value: str) -> bool:
    if "\\" in value:
        return True
    path = PurePosixPath(value)
    return ".." in path.parts


def _safe_bundle_uid(value: Any) -> tuple[str, str | None]:
    bundle_uid = _clean_text(value)
    if not bundle_uid:
        return "", None
    if PurePosixPath(bundle_uid).is_absolute() or _has_path_traversal(bundle_uid):
        return bundle_uid, "bundle_uid cannot contain path separators or parent paths"
    if "/" in bundle_uid:
        return bundle_uid, "bundle_uid cannot contain path separators or parent paths"
    return bundle_uid, None


def _expected_manifest_path(bundle_uid: str) -> str:
    return (
        DEFAULT_ASSET_HUB_JOB_ASSETS_DIR
        / bundle_uid
        / "manifests"
        / "renderer-manifest.json"
    ).as_posix()


def _is_under(candidate: Path, roots: list[Path]) -> bool:
    for root in roots:
        try:
            candidate.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _normalize_local_path(
    value: Any,
    *,
    project_root: Path,
    roots: tuple[str, ...],
    label: str,
    strict: bool,
    errors: list[str],
    warnings: list[str],
) -> str:
    text = _clean_text(value)
    if not text:
        return ""
    if _has_path_traversal(text):
        errors.append(f"{label} cannot use path traversal")
        return text

    requested = Path(text)
    candidate = requested if requested.is_absolute() else project_root / requested
    resolved = candidate.resolve(strict=False)
    allowed_roots = [(project_root / root).resolve(strict=False) for root in roots]
    if not _is_under(resolved, allowed_roots):
        allowed = ", ".join(roots)
        errors.append(f"{label} must stay under {allowed}")
        return resolved.as_posix()

    if not resolved.exists():
        message = f"{label} does not exist: {resolved.as_posix()}"
        if strict:
            errors.append(message)
        else:
            warnings.append(message)
    elif not resolved.is_file():
        errors.append(f"{label} is not a file: {resolved.as_posix()}")
    return resolved.as_posix()


def _normalize_asset_hub_manifest_path(
    value: Any,
    *,
    strict: bool,
    errors: list[str],
    warnings: list[str],
) -> str:
    text = _clean_text(value)
    if not text:
        return ""
    if _has_path_traversal(text):
        errors.append("b_roll.manifest_path cannot use path traversal")
        return text

    requested = Path(text)
    candidate = requested if requested.is_absolute() else DEFAULT_ASSET_HUB_JOB_ASSETS_DIR / requested
    resolved = candidate.resolve(strict=False)
    base = DEFAULT_ASSET_HUB_JOB_ASSETS_DIR.resolve(strict=False)
    try:
        relative = resolved.relative_to(base)
    except ValueError:
        errors.append("b_roll.manifest_path must stay under /data/job-assets")
        return resolved.as_posix()

    if len(relative.parts) != 3 or relative.parts[1:] != (
        "manifests",
        "renderer-manifest.json",
    ):
        errors.append(
            "b_roll.manifest_path must match "
            "/data/job-assets/<bundle_uid>/manifests/renderer-manifest.json"
        )
    if resolved.suffix.lower() != ".json":
        errors.append("b_roll.manifest_path must point to a .json file")
    if not resolved.exists():
        message = f"b_roll.manifest_path does not exist: {resolved.as_posix()}"
        if strict:
            errors.append(message)
        else:
            warnings.append(message)
    elif not resolved.is_file():
        errors.append(f"b_roll.manifest_path is not a file: {resolved.as_posix()}")
    return resolved.as_posix()


def validate_aroll_broll_config(
    config: dict[str, Any],
    project_root: str | Path | None = None,
    *,
    strict: bool = True,
) -> dict[str, Any]:
    """Validate and normalize an A-roll/B-roll config without rendering or enqueueing."""

    errors: list[str] = []
    warnings: list[str] = []
    normalized = _merge_default_config(config)
    root = Path(project_root or Path.cwd()).resolve(strict=False)

    if normalized.get("render_mode") != RENDER_MODE_AROLL_BROLL:
        errors.append("render_mode must be aroll_broll")
        normalized["render_mode"] = RENDER_MODE_AROLL_BROLL

    a_roll = normalized.setdefault("a_roll", {})
    b_roll = normalized.setdefault("b_roll", {})
    layout = normalized.setdefault("layout", {})
    subtitles = normalized.setdefault("subtitles", {})

    if a_roll.get("audio_policy") != AROLL_AUDIO_ORIGINAL:
        errors.append("a_roll.audio_policy must be original")
    if b_roll.get("audio_policy") != BROLL_AUDIO_MUTED:
        errors.append("b_roll.audio_policy must be muted")

    crop = _clean_text(a_roll.get("crop")) or SPEAKER_CROP_CENTER
    if crop not in ALLOWED_CROPS:
        errors.append("a_roll.crop must be center, top, or bottom")
    a_roll["crop"] = crop

    preset = _clean_text(layout.get("preset")) or LAYOUT_ALTERNATING_FULLSCREEN
    if preset not in ALLOWED_LAYOUT_PRESETS:
        errors.append("layout.preset is not supported")
    layout["preset"] = preset

    subtitles_source = _clean_text(subtitles.get("source")) or SUBTITLES_SOURCE_NONE
    if subtitles_source not in ALLOWED_SUBTITLE_SOURCES:
        errors.append("subtitles.source is not supported")
    subtitles["source"] = subtitles_source

    try:
        clip_seconds = int(b_roll.get("clip_seconds", 4))
    except (TypeError, ValueError):
        clip_seconds = 4
        errors.append("b_roll.clip_seconds must be a number")
    if clip_seconds < 2 or clip_seconds > 12:
        errors.append("b_roll.clip_seconds must be between 2 and 12")
    b_roll["clip_seconds"] = clip_seconds

    frequency = _clean_text(b_roll.get("frequency")) or "medium"
    if frequency not in ALLOWED_FREQUENCIES:
        errors.append("b_roll.frequency must be low, medium, or high")
    b_roll["frequency"] = frequency

    b_roll_source = _clean_text(b_roll.get("source")) or BROLL_SOURCE_ASSET_HUB_MANIFEST
    if b_roll_source not in ALLOWED_BROLL_SOURCES:
        errors.append("b_roll.source is not supported")
    b_roll["source"] = b_roll_source

    a_roll_path = _clean_text(a_roll.get("path"))
    if not a_roll_path:
        message = "a_roll.path is required for complete validation"
        if strict:
            errors.append(message)
        else:
            warnings.append(message)
    else:
        a_roll["path"] = _normalize_local_path(
            a_roll_path,
            project_root=root,
            roots=LOCAL_VIDEO_ROOTS,
            label="a_roll.path",
            strict=strict,
            errors=errors,
            warnings=warnings,
        )

    if subtitles_source == SUBTITLES_SOURCE_CUSTOM_SRT:
        subtitles["custom_srt_path"] = _normalize_local_path(
            subtitles.get("custom_srt_path"),
            project_root=root,
            roots=(LOCAL_SUBTITLE_ROOT,),
            label="subtitles.custom_srt_path",
            strict=strict,
            errors=errors,
            warnings=warnings,
        )

    if b_roll_source == BROLL_SOURCE_ASSET_HUB_MANIFEST:
        bundle_uid, bundle_error = _safe_bundle_uid(b_roll.get("bundle_uid"))
        if bundle_error:
            errors.append(bundle_error)
        b_roll["bundle_uid"] = bundle_uid
        manifest_path = _clean_text(b_roll.get("manifest_path"))
        if bundle_uid and not manifest_path:
            manifest_path = _expected_manifest_path(bundle_uid)
            b_roll["manifest_path"] = manifest_path
        if manifest_path:
            b_roll["manifest_path"] = _normalize_asset_hub_manifest_path(
                manifest_path,
                strict=strict,
                errors=errors,
                warnings=warnings,
            )
        elif strict:
            errors.append("b_roll.manifest_path is required for Asset Hub B-roll")

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "normalized": deepcopy(normalized),
    }


def build_aroll_broll_preview_timeline(
    aroll_duration_seconds: float,
    broll_count: int,
    clip_seconds: int,
    frequency: str,
    layout_preset: str,
) -> list[dict[str, Any]]:
    """Build a conceptual visual-only timeline for UI preview."""

    try:
        total_duration = max(0.0, float(aroll_duration_seconds))
    except (TypeError, ValueError):
        total_duration = 0.0
    try:
        total_broll = max(0, int(broll_count))
    except (TypeError, ValueError):
        total_broll = 0
    try:
        clip_duration = max(0.0, float(clip_seconds))
    except (TypeError, ValueError):
        clip_duration = 0.0

    layout = layout_preset if layout_preset in ALLOWED_LAYOUT_PRESETS else LAYOUT_ALTERNATING_FULLSCREEN
    if total_duration <= 0:
        return []
    if total_broll <= 0 or clip_duration <= 0:
        return [
            {
                "start": 0.0,
                "end": round(total_duration, 2),
                "visual": "a_roll",
                "layout": layout,
            }
        ]

    interval = FREQUENCY_INTERVAL_SECONDS.get(frequency, FREQUENCY_INTERVAL_SECONDS["medium"])
    timeline: list[dict[str, Any]] = []
    cursor = 0.0
    broll_index = 0
    next_broll_start = min(interval / 2.0, total_duration)

    while cursor < total_duration:
        if broll_index >= total_broll or next_broll_start >= total_duration:
            if cursor < total_duration:
                timeline.append(
                    {
                        "start": round(cursor, 2),
                        "end": round(total_duration, 2),
                        "visual": "a_roll",
                        "layout": layout,
                    }
                )
            break

        broll_start = max(cursor, min(next_broll_start, total_duration))
        if cursor < broll_start:
            timeline.append(
                {
                    "start": round(cursor, 2),
                    "end": round(broll_start, 2),
                    "visual": "a_roll",
                    "layout": layout,
                }
            )

        broll_end = min(broll_start + clip_duration, total_duration)
        if broll_end <= broll_start:
            break
        timeline.append(
            {
                "start": round(broll_start, 2),
                "end": round(broll_end, 2),
                "visual": "b_roll",
                "broll_index": broll_index,
                "layout": layout,
            }
        )
        broll_index += 1
        cursor = broll_end
        next_broll_start = cursor + interval

    return [item for item in timeline if item["end"] > item["start"]]


def summarize_aroll_broll_config(config: dict[str, Any]) -> dict[str, str]:
    """Return short human-readable labels for the UI."""

    normalized = _merge_default_config(config)
    subtitles_source = normalized.get("subtitles", {}).get("source")
    subtitles_label = {
        SUBTITLES_SOURCE_AROLL_AUDIO: "Subtítulos desde audio del presentador",
        SUBTITLES_SOURCE_CUSTOM_SRT: "Subtítulos desde SRT propio",
        SUBTITLES_SOURCE_NONE: "Sin subtítulos",
    }.get(subtitles_source, "Subtítulos sin definir")

    return {
        "audio": "Audio original del presentador",
        "b-roll": "B-roll muted como apoyo visual",
        "subtitles": subtitles_label,
        "layout": str(
            normalized.get("layout", {}).get("preset")
            or LAYOUT_ALTERNATING_FULLSCREEN
        ),
        "crop": str(normalized.get("a_roll", {}).get("crop") or SPEAKER_CROP_CENTER),
        "renderer": "Renderer preparado: alternating_fullscreen",
    }


def build_aroll_broll_queue_payload(
    config: dict[str, Any],
    *,
    job_id: str,
    project_root: str | Path | None = None,
    render_quality: str = "draft_720p",
    title: str = "",
    strict: bool = True,
) -> dict[str, Any]:
    """Build a queued-only A-roll/B-roll pending payload.

    The resulting job is intentionally not a MoneyPrinterTurbo API payload. It
    carries a render_mode guard so the runner can reject it before submitting
    anything to the backend until the A-roll/B-roll renderer is wired end to end.
    """

    clean_job_id = _clean_text(job_id)
    if not clean_job_id:
        raise ValueError("job_id is required for A-roll/B-roll queue payload")

    validation = validate_aroll_broll_config(
        config,
        project_root=project_root,
        strict=strict,
    )
    if not validation["ok"]:
        details = "; ".join(validation["errors"])
        raise ValueError(f"A-roll/B-roll config is not valid: {details}")

    normalized = validation["normalized"]
    subtitles_source = normalized.get("subtitles", {}).get("source")
    video_title = _clean_text(title) or "A-roll/B-roll"
    quality = _clean_text(render_quality) or "draft_720p"

    return {
        "job_id": clean_job_id,
        "description": "A-roll/B-roll queued job; renderer execution disabled",
        "render_mode": RENDER_MODE_AROLL_BROLL,
        "aroll_broll": deepcopy(normalized),
        "video_subject": video_title,
        "video_aspect": "9:16",
        "video_resolution": quality,
        "video_source": RENDER_MODE_AROLL_BROLL,
        "subtitle_enabled": subtitles_source != SUBTITLES_SOURCE_NONE,
        "runner": {
            "job_id": clean_job_id,
            "render_mode": RENDER_MODE_AROLL_BROLL,
            "execution_guard": AROLL_BROLL_QUEUE_GUARD,
            "renderer_enabled": False,
            "message": (
                "A-roll/B-roll queue payload is prepared, but runner execution "
                "is disabled until the renderer integration phase."
            ),
        },
    }
