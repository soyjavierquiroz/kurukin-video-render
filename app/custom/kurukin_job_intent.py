"""Minimal Kurukin job intent normalization and MPT spec compilation."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from app.custom.mpt_engine_bridge import build_mpt_video_task_from_kurukin_job


MODE_TOPIC_TO_VIDEO = "topic_to_video"
MODE_AUDIO_TO_VIDEO = "audio_to_video"
MODE_SPEAKER_VIDEO_TO_ENHANCED_VIDEO = "speaker_video_to_enhanced_video"

STATUS_READY_TO_SUBMIT = "READY_TO_SUBMIT"
STATUS_NEEDS_INPUT = "NEEDS_INPUT"
STATUS_NOT_READY = "NOT_READY"

DEFAULT_LANGUAGE = "es"
DEFAULT_FORMAT = "vertical"
DEFAULT_DURATION_SECONDS = 45
DEFAULT_PRESET = "educational"

ALLOWED_MODES = {
    MODE_TOPIC_TO_VIDEO,
    MODE_AUDIO_TO_VIDEO,
    MODE_SPEAKER_VIDEO_TO_ENHANCED_VIDEO,
}
ALLOWED_FORMATS = {"vertical", "horizontal", "square"}
FORMAT_TO_MPT_ASPECT = {
    "vertical": "9:16",
    "horizontal": "16:9",
    "square": "1:1",
}
MIN_DURATION_SECONDS = 4
MAX_DURATION_SECONDS = 300
VISUAL_AUTOFILL_REASON = "needs_local_visual_asset"
VISUAL_AUTOFILL_SOURCE = "audio_to_video_local_autofill"
VISUAL_AUTOFILL_SEARCH_DIRS = (
    Path("storage/local_videos"),
    Path("storage/local_images"),
    Path("storage/local_assets"),
)
VISUAL_AUTOFILL_EXTENSIONS = {".mp4", ".mov", ".mkv", ".jpg", ".jpeg", ".png"}
VISUAL_AUTOFILL_PREFERRED_PATHS = (
    Path("storage/local_videos/_aroll_broll_smoke_001/aroll_presenter_fixture_6s.mp4"),
    Path("storage/local_videos/_aroll_broll_smoke_001/broll_visual_fixture_2s.mp4"),
)


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _clean_mode(value: Any) -> str:
    return _clean_text(value).lower()


def _clean_format(value: Any) -> str:
    return _clean_text(value).lower() or DEFAULT_FORMAT


def _as_dict(value: Any) -> dict[str, Any]:
    return deepcopy(value) if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return deepcopy(value)
    if isinstance(value, tuple):
        return list(deepcopy(value))
    return [deepcopy(value)]


def _duration_seconds(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return DEFAULT_DURATION_SECONDS


def _error(field: str, message: str, error_type: str = "intent_validation") -> dict[str, str]:
    return {"field": field, "message": message, "type": error_type}


def _is_url(value: Any) -> bool:
    text = _clean_text(value).lower()
    return text.startswith("http://") or text.startswith("https://")


def _path_looks_local(value: Any) -> bool:
    text = _clean_text(value)
    if not text or _is_url(text):
        return False
    return "://" not in text


def _material_path(item: Any) -> str:
    if isinstance(item, dict):
        return _clean_text(
            item.get("url")
            or item.get("path")
            or item.get("local_path")
            or item.get("file_path")
            or item.get("source_path")
        )
    return _clean_text(item)


def _safe_local_visual_path(value: Any) -> str:
    text = _clean_text(value)
    if not text or not _path_looks_local(text):
        return ""
    path = Path(text)
    if path.is_absolute() or ".." in path.parts:
        return ""
    if path.suffix.lower() not in VISUAL_AUTOFILL_EXTENSIONS:
        return ""
    return path.as_posix()


def _local_visual_paths(intent: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for key in ("local_visual_asset", "local_visual_path", "visual_path"):
        value = _clean_text(intent.get(key))
        if value:
            paths.append(value)
    for key in ("local_visual_assets", "video_materials"):
        for item in _as_list(intent.get(key)):
            value = _material_path(item)
            if value:
                paths.append(value)
    if intent.get("mode") in {MODE_TOPIC_TO_VIDEO, MODE_AUDIO_TO_VIDEO}:
        video_path = _clean_text(intent.get("video_path"))
        if video_path:
            paths.append(video_path)

    deduped: list[str] = []
    for path in paths:
        if path not in deduped:
            deduped.append(path)
    return deduped


def _project_root_path(project_root: str | Path | None) -> Path:
    return Path(project_root).resolve() if project_root else Path.cwd().resolve()


def _path_exists_under_root(root: Path, relative_path: Path) -> bool:
    if relative_path.is_absolute() or ".." in relative_path.parts:
        return False
    candidate = (root / relative_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return candidate.is_file()


def resolve_audio_to_video_autofill_visual_path(
    *,
    project_root: str | Path | None = None,
) -> str:
    """Return a safe existing local visual path for audio-only intents."""

    root = _project_root_path(project_root)
    for relative_path in VISUAL_AUTOFILL_PREFERRED_PATHS:
        if _path_exists_under_root(root, relative_path):
            return relative_path.as_posix()

    candidates: list[Path] = []
    for search_dir in VISUAL_AUTOFILL_SEARCH_DIRS:
        absolute_dir = (root / search_dir).resolve()
        try:
            absolute_dir.relative_to(root)
        except ValueError:
            continue
        if not absolute_dir.is_dir():
            continue
        for candidate in absolute_dir.rglob("*"):
            if not candidate.is_file():
                continue
            if candidate.suffix.lower() not in VISUAL_AUTOFILL_EXTENSIONS:
                continue
            try:
                relative = candidate.resolve().relative_to(root)
            except ValueError:
                continue
            if _safe_local_visual_path(relative.as_posix()):
                candidates.append(relative)

    if not candidates:
        return ""

    candidates.sort(key=lambda item: (item.parts, item.name.lower()))
    return candidates[0].as_posix()


def _autofill_audio_to_video_visual(
    intent: dict[str, Any],
    *,
    project_root: str | Path | None = None,
) -> dict[str, Any]:
    normalized = deepcopy(intent)
    if normalized.get("mode") != MODE_AUDIO_TO_VIDEO:
        return normalized
    if not normalized.get("audio_path") or _local_visual_paths(normalized):
        return normalized

    visual_path = resolve_audio_to_video_autofill_visual_path(
        project_root=project_root
    )
    if not visual_path:
        return normalized

    normalized["video_path"] = visual_path
    normalized["visual_path"] = visual_path
    normalized["visual_autofill"] = {
        "source": VISUAL_AUTOFILL_SOURCE,
        "path": visual_path,
    }
    return normalized


def _safe_task_id(value: Any) -> str:
    text = _clean_text(value)
    safe = "".join(char for char in text if char.isalnum() or char in "-_")
    return safe.strip("-_")


def normalize_job_intent(intent: dict[str, Any]) -> dict[str, Any]:
    """Normalize user-facing intent fields without calling providers."""

    source = _as_dict(intent)
    normalized = deepcopy(source)
    normalized["mode"] = _clean_mode(source.get("mode"))
    normalized["topic"] = _clean_text(source.get("topic"))
    normalized["script"] = _clean_text(source.get("script"))
    normalized["language"] = _clean_text(source.get("language")) or DEFAULT_LANGUAGE
    normalized["format"] = _clean_format(source.get("format"))
    normalized["duration_seconds"] = _duration_seconds(
        source.get("duration_seconds", DEFAULT_DURATION_SECONDS)
    )
    normalized["preset"] = _clean_text(source.get("preset")) or DEFAULT_PRESET
    normalized["audio_path"] = _clean_text(source.get("audio_path"))
    normalized["visual_path"] = _clean_text(source.get("visual_path"))
    normalized["video_path"] = _clean_text(source.get("video_path")) or normalized[
        "visual_path"
    ]
    normalized["task_id"] = _safe_task_id(source.get("task_id"))
    return normalized


def validate_job_intent(intent: dict[str, Any]) -> dict[str, Any]:
    """Validate a normalized Kurukin job intent and return a repo-style result."""

    normalized = normalize_job_intent(intent)
    mode = normalized["mode"]
    errors: list[dict[str, str]] = []

    if not mode:
        errors.append(_error("mode", "mode is required"))
    elif mode not in ALLOWED_MODES:
        errors.append(_error("mode", "mode is not supported"))

    if normalized["format"] not in ALLOWED_FORMATS:
        errors.append(_error("format", "format must be vertical, horizontal, or square"))

    duration = normalized["duration_seconds"]
    if duration < MIN_DURATION_SECONDS or duration > MAX_DURATION_SECONDS:
        errors.append(
            _error(
                "duration_seconds",
                "duration_seconds must be between 4 and 300",
            )
        )

    if mode == MODE_TOPIC_TO_VIDEO and not (
        normalized["topic"] or normalized["script"]
    ):
        errors.append(_error("topic", "topic_to_video requires topic or script"))
    if mode == MODE_AUDIO_TO_VIDEO and not normalized["audio_path"]:
        errors.append(_error("audio_path", "audio_to_video requires audio_path"))
    if mode == MODE_SPEAKER_VIDEO_TO_ENHANCED_VIDEO and not normalized["video_path"]:
        errors.append(
            _error(
                "video_path",
                "speaker_video_to_enhanced_video requires video_path",
            )
        )

    for field in ("audio_path", "video_path", "visual_path"):
        value = normalized.get(field)
        if value and not _path_looks_local(value):
            errors.append(_error(field, f"{field} must be a local path, not a URL"))
    for item in _as_list(normalized.get("video_materials")) + _as_list(
        normalized.get("local_visual_assets")
    ):
        value = _material_path(item)
        if value and not _path_looks_local(value):
            errors.append(
                _error("video_materials", "visual asset paths must be local, not URLs")
            )
    for field in ("local_visual_asset", "local_visual_path"):
        value = normalized.get(field)
        if value and not _path_looks_local(value):
            errors.append(_error(field, f"{field} must be a local path, not a URL"))

    return {
        "ok": not errors,
        "status": STATUS_NEEDS_INPUT if errors else STATUS_READY_TO_SUBMIT,
        "intent": normalized,
        "errors": errors,
    }


def _readiness_for_intent(intent: dict[str, Any]) -> tuple[str, list[str]]:
    mode = intent["mode"]
    reasons: list[str] = []
    visual_paths = _local_visual_paths(intent)

    if mode == MODE_TOPIC_TO_VIDEO:
        if not intent.get("audio_path"):
            reasons.append("needs_audio_or_tts")
        if not visual_paths:
            reasons.append("needs_local_visual_asset")
        return (STATUS_NEEDS_INPUT if reasons else STATUS_READY_TO_SUBMIT), reasons

    if mode == MODE_AUDIO_TO_VIDEO:
        if not visual_paths:
            reasons.append(VISUAL_AUTOFILL_REASON)
        return (STATUS_NEEDS_INPUT if reasons else STATUS_READY_TO_SUBMIT), reasons

    if mode == MODE_SPEAKER_VIDEO_TO_ENHANCED_VIDEO:
        reasons.append("needs_audio_extract")
        reasons.append("speaker_video_direct_input_not_ready")
        return STATUS_NOT_READY, reasons

    return STATUS_NEEDS_INPUT, ["unsupported_mode"]


def _mpt_materials(paths: list[str]) -> list[dict[str, Any]]:
    return [
        {
            "provider": "local",
            "url": path,
            "duration": 0,
        }
        for path in paths
    ]


def _build_kurukin_job(intent: dict[str, Any]) -> dict[str, Any]:
    subject = _clean_text(intent.get("topic") or intent.get("script")) or "Kurukin video"
    script = _clean_text(intent.get("script"))
    mode = intent["mode"]
    visual_paths = _local_visual_paths(intent)
    job: dict[str, Any] = {
        "job_id": intent.get("task_id") or "",
        "task_id": intent.get("task_id") or "",
        "video_subject": subject,
        "video_script": script,
        "video_terms": [intent["topic"]] if intent.get("topic") else [],
        "video_aspect": FORMAT_TO_MPT_ASPECT.get(intent["format"], "9:16"),
        "video_resolution": "1080p",
        "video_clip_duration": min(10, max(1, int(intent["duration_seconds"]))),
        "video_count": 1,
        "video_language": intent["language"],
        "custom_audio_file": intent.get("audio_path") or "",
        "subtitle_enabled": False,
        "subtitle_provider": "none",
        "video_source": "local",
        "video_materials": _mpt_materials(visual_paths),
        "asset_policy": {
            "mode": "local_only",
            "allowed_sources": ["local"],
        },
        "metadata": {
            "job_intent": {
                "mode": mode,
                "language": intent["language"],
                "duration_seconds": intent["duration_seconds"],
                "format": intent["format"],
                "preset": intent["preset"],
                "visual_autofill": deepcopy(intent.get("visual_autofill", {})),
                "external_providers_allowed": False,
                "ai_generation_allowed": False,
                "asset_hub_api_allowed": False,
            }
        },
    }
    if mode == MODE_SPEAKER_VIDEO_TO_ENHANCED_VIDEO:
        job["render_mode"] = "aroll_broll"
        job["a_roll"] = {
            "path": intent.get("video_path") or "",
            "audio_policy": "original",
        }
        job["b_roll"] = {
            "assets": [],
            "audio_policy": "muted",
            "intent": "enhance_speaker_video",
        }
    return job


def compile_job_intent_to_mpt_spec(
    intent: dict[str, Any],
    *,
    project_root: str | Path | None = None,
) -> dict[str, Any]:
    """Compile a valid intent to a side-effect-free MPT-compatible spec."""

    validation = validate_job_intent(intent)
    normalized = _autofill_audio_to_video_visual(
        validation["intent"],
        project_root=project_root,
    )
    if not validation["ok"]:
        return {
            "ok": False,
            "status": STATUS_NEEDS_INPUT,
            "intent": normalized,
            "mpt_spec": {},
            "kurukin_job": {},
            "reasons": [],
            "errors": validation["errors"],
        }

    status, reasons = _readiness_for_intent(normalized)
    kurukin_job = _build_kurukin_job(normalized)
    mpt_spec = build_mpt_video_task_from_kurukin_job(kurukin_job)
    gaps = list(mpt_spec.get("gaps") or [])
    for reason in reasons:
        if reason not in gaps:
            gaps.append(reason)
    mpt_spec["gaps"] = gaps
    metadata = mpt_spec.setdefault("kurukin_metadata", {})
    metadata["job_intent_status"] = status
    metadata["job_intent_reasons"] = list(reasons)
    metadata["job_intent_mode"] = normalized["mode"]

    return {
        "ok": status == STATUS_READY_TO_SUBMIT,
        "status": status,
        "intent": normalized,
        "mpt_spec": mpt_spec,
        "kurukin_job": kurukin_job,
        "reasons": reasons,
        "errors": [],
    }
