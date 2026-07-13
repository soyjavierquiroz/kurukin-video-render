"""Minimal Kurukin job intent normalization and MPT spec compilation."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from app.custom.kurukin_local_visual_picker import (
    LOCAL_PICKER_SOURCE,
    LOW_RELEVANCE_CONFIDENCE,
    pick_local_visual_for_intent,
)
from app.custom.kurukin_topic_planner import build_topic_script_plan
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
VISUAL_RELEVANCE_REASON = "needs_relevant_local_visual_asset"
LOW_RELEVANCE_ALLOWED_WARNING = "low_relevance_visual_allowed"
AUDIO_STRONG_VISUAL_TOPIC_TOKENS = {
    "casa",
    "casas",
    "comprar",
    "compra",
    "errores",
    "error",
    "inmobiliaria",
    "inmobiliario",
    "propiedad",
    "propiedades",
    "usada",
    "usadas",
    "vivienda",
    "viviendas",
    "checklist",
}


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


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return _clean_text(value).lower() in {"1", "true", "yes", "on"}


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


def _local_visual_paths(intent: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for key in (
        "local_visual_asset",
        "local_visual_path",
        "visual_path",
        "resolved_visual_path",
    ):
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


def resolve_audio_to_video_autofill_visual_path(
    *,
    project_root: str | Path | None = None,
) -> str:
    """Return a safe existing local visual path for audio-only intents."""

    picked = pick_local_visual_for_intent({}, project_root=project_root)
    return picked["path"] if picked else ""


def _autofill_audio_to_video_visual(
    intent: dict[str, Any],
    *,
    project_root: str | Path | None = None,
) -> dict[str, Any]:
    normalized = deepcopy(intent)
    if normalized.get("mode") not in {MODE_TOPIC_TO_VIDEO, MODE_AUDIO_TO_VIDEO}:
        return normalized
    if not normalized.get("audio_path") or _local_visual_paths(normalized):
        return normalized

    picked = pick_local_visual_for_intent(
        normalized,
        project_root=project_root
    )
    visual_path = picked["path"] if picked else ""
    if not visual_path:
        return normalized

    normalized["video_path"] = visual_path
    normalized["visual_path"] = visual_path
    normalized["resolved_visual_path"] = visual_path
    normalized["visual_autofill_source"] = LOCAL_PICKER_SOURCE
    normalized["visual_relevance_score"] = picked.get("visual_relevance_score", 0)
    normalized["visual_relevance_confidence"] = _clean_text(
        picked.get("visual_relevance_confidence")
    )
    normalized["visual_relevance_reason"] = _clean_text(
        picked.get("visual_relevance_reason")
    )
    normalized["visual_relevance_matches"] = list(
        picked.get("visual_relevance_matches") or []
    )
    normalized["visual_relevance_strong_matches"] = list(
        picked.get("visual_relevance_strong_matches") or []
    )
    normalized["visual_relevance_topic_tokens"] = list(
        picked.get("visual_relevance_topic_tokens") or []
    )
    if (
        normalized["visual_relevance_confidence"] == LOW_RELEVANCE_CONFIDENCE
        and normalized.get("allow_low_relevance_visual")
    ):
        normalized["visual_relevance_warning"] = LOW_RELEVANCE_ALLOWED_WARNING
    normalized["visual_autofill"] = {
        "source": LOCAL_PICKER_SOURCE,
        "path": visual_path,
        "picker": picked,
    }
    return normalized


def _enrich_topic_to_video_plan(intent: dict[str, Any]) -> dict[str, Any]:
    normalized = deepcopy(intent)
    if normalized.get("mode") != MODE_TOPIC_TO_VIDEO:
        return normalized

    plan = build_topic_script_plan(normalized)
    normalized["topic_plan"] = plan
    if plan.get("ok"):
        normalized["script"] = normalized.get("script") or plan.get("script", "")
        normalized["scenes"] = deepcopy(plan.get("scenes") or [])
        normalized["visual_keywords"] = list(plan.get("visual_keywords") or [])
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
    normalized["resolved_visual_path"] = _clean_text(
        source.get("resolved_visual_path")
    )
    normalized["visual_autofill_source"] = _clean_text(
        source.get("visual_autofill_source")
    )
    normalized["visual_relevance_score"] = source.get("visual_relevance_score", 0)
    normalized["visual_relevance_confidence"] = _clean_text(
        source.get("visual_relevance_confidence")
    )
    normalized["visual_relevance_reason"] = _clean_text(
        source.get("visual_relevance_reason")
    )
    normalized["visual_relevance_warning"] = _clean_text(
        source.get("visual_relevance_warning")
    )
    normalized["allow_low_relevance_visual"] = _as_bool(
        source.get("allow_low_relevance_visual")
    )
    normalized["video_path"] = _clean_text(source.get("video_path")) or normalized[
        "visual_path"
    ] or normalized["resolved_visual_path"]
    normalized["task_id"] = _safe_task_id(source.get("task_id"))
    normalized = _enrich_topic_to_video_plan(normalized)
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

    for field in ("audio_path", "video_path", "visual_path", "resolved_visual_path"):
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

    status = STATUS_NEEDS_INPUT
    reasons: list[str] = []
    if not errors:
        status, reasons = _readiness_for_intent(normalized)

    return {
        "ok": not errors,
        "status": status,
        "intent": normalized,
        "reasons": reasons,
        "errors": errors,
    }


def _readiness_for_intent(intent: dict[str, Any]) -> tuple[str, list[str]]:
    mode = intent["mode"]
    reasons: list[str] = []
    visual_paths = _local_visual_paths(intent)
    low_relevance_autofill = (
        intent.get("visual_autofill_source") == LOCAL_PICKER_SOURCE
        and intent.get("visual_relevance_confidence") == LOW_RELEVANCE_CONFIDENCE
        and not intent.get("allow_low_relevance_visual")
    )

    if mode == MODE_TOPIC_TO_VIDEO:
        if not intent.get("audio_path"):
            reasons.append("needs_audio_or_tts")
        elif not visual_paths:
            reasons.append("needs_local_visual_asset")
        elif low_relevance_autofill:
            reasons.append(VISUAL_RELEVANCE_REASON)
        return (STATUS_NEEDS_INPUT if reasons else STATUS_READY_TO_SUBMIT), reasons

    if mode == MODE_AUDIO_TO_VIDEO:
        if not visual_paths:
            reasons.append(VISUAL_AUTOFILL_REASON)
        elif low_relevance_autofill and any(
            token in AUDIO_STRONG_VISUAL_TOPIC_TOKENS
            for token in _as_list(intent.get("visual_relevance_topic_tokens"))
        ):
            reasons.append(VISUAL_RELEVANCE_REASON)
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
    visual_keywords = [
        _clean_text(item)
        for item in _as_list(intent.get("visual_keywords"))
        if _clean_text(item)
    ]
    mode = intent["mode"]
    visual_paths = _local_visual_paths(intent)
    job: dict[str, Any] = {
        "job_id": intent.get("task_id") or "",
        "task_id": intent.get("task_id") or "",
        "video_subject": subject,
        "video_script": script,
        "video_terms": visual_keywords or ([intent["topic"]] if intent.get("topic") else []),
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
                "topic": intent.get("topic", ""),
                "script": script,
                "audio_path": intent.get("audio_path") or "",
                "language": intent["language"],
                "duration_seconds": intent["duration_seconds"],
                "format": intent["format"],
                "preset": intent["preset"],
                "resolved_visual_path": intent.get("resolved_visual_path", ""),
                "visual_autofill_source": intent.get("visual_autofill_source", ""),
                "visual_relevance_score": intent.get("visual_relevance_score", 0),
                "visual_relevance_confidence": intent.get(
                    "visual_relevance_confidence", ""
                ),
                "visual_relevance_reason": intent.get("visual_relevance_reason", ""),
                "visual_relevance_warning": intent.get("visual_relevance_warning", ""),
                "visual_autofill": deepcopy(intent.get("visual_autofill", {})),
                "topic_plan": deepcopy(intent.get("topic_plan", {})),
                "topic_plan_summary": {
                    "status": _as_dict(intent.get("topic_plan")).get("status", ""),
                    "reason": _as_dict(intent.get("topic_plan")).get("reason", ""),
                    "scene_count": len(_as_list(intent.get("scenes"))),
                    "visual_keywords": list(visual_keywords),
                },
                "scenes": deepcopy(intent.get("scenes", [])),
                "visual_keywords": list(visual_keywords),
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
    if normalized.get("visual_relevance_warning"):
        warnings = list(mpt_spec.get("warnings") or [])
        warning = normalized["visual_relevance_warning"]
        if warning not in warnings:
            warnings.append(warning)
        mpt_spec["warnings"] = warnings
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
