"""Dry-run submit planning for native MoneyPrinterTurbo execution."""

from __future__ import annotations

from copy import deepcopy
import os
import re
from typing import Any, Callable

from app.custom.mpt_engine_bridge import (
    build_validated_mpt_video_task_from_kurukin_job,
)


MPT_ENGINE_SUBMIT_FLAG = "KURUKIN_ENABLE_MPT_ENGINE_SUBMIT"

_SUBMIT_TARGET = {
    "mode": "mpt_engine",
    "api_path": "/api/v1/videos",
    "service_path": "app.services.task.start",
}

_GUARDRAILS = {
    "dry_run_required_by_default": True,
    "real_submit_requires_explicit_authorization": True,
}

_FORBIDDEN_PLAN_MARKERS = (
    "pending_path",
    "created_task",
    "task_id",
    "task_created",
    "provider_response",
    "downloaded_assets",
    "render_result",
)


def _as_dict(value: Any) -> dict[str, Any]:
    return deepcopy(value) if isinstance(value, dict) else {}


def _redact_sensitive_text(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    sensitive_words = (
        "api_key",
        "apikey",
        "authorization",
        "bearer",
        "credential",
        "password",
        "secret",
        "token",
    )
    redacted = text
    for word in sensitive_words:
        redacted = re.sub(
            rf"(?i)({re.escape(word)})(\\s*[=:]\\s*)([^\\s,;]+)",
            r"\1\2<redacted>",
            redacted,
        )
    if any(word in redacted.lower() for word in sensitive_words):
        return "<redacted>"
    return redacted


def _safe_error(message: Any, *, field: str = "", error_type: str = "submit_plan") -> dict[str, str]:
    return {
        "field": _redact_sensitive_text(field),
        "message": _redact_sensitive_text(message),
        "type": _redact_sensitive_text(error_type),
    }


def _safe_errors(errors: Any) -> list[dict[str, str]]:
    if not isinstance(errors, list):
        return []

    safe: list[dict[str, str]] = []
    for item in errors:
        if isinstance(item, dict):
            safe.append(
                _safe_error(
                    item.get("message", "invalid value"),
                    field=str(item.get("field", "")),
                    error_type=str(item.get("type", "validation_error")),
                )
            )
        else:
            safe.append(_safe_error(item))
    return safe


def build_mpt_engine_submit_plan(kurukin_job: dict[str, Any]) -> dict[str, Any]:
    """Build a side-effect-free MPT submit plan from a Kurukin job."""

    try:
        validation = build_validated_mpt_video_task_from_kurukin_job(kurukin_job)
    except Exception as exc:
        return {
            "kind": "mpt_engine_submit_plan",
            "ok": False,
            "execution": "dry_run",
            "submitted": False,
            "dry_run": True,
            "validated_model": "VideoParams",
            "mpt_params": {},
            "kurukin_metadata": {},
            "submit_target": deepcopy(_SUBMIT_TARGET),
            "guardrails": deepcopy(_GUARDRAILS),
            "errors": [_safe_error(exc, error_type=exc.__class__.__name__)],
        }

    task_spec = _as_dict(validation.get("task_spec"))
    plan = {
        "kind": "mpt_engine_submit_plan",
        "ok": bool(validation.get("ok")),
        "execution": "dry_run",
        "submitted": False,
        "dry_run": True,
        "validated_model": validation.get("validated_model") or "VideoParams",
        "mpt_params": _as_dict(validation.get("spec")),
        "kurukin_metadata": _as_dict(task_spec.get("kurukin_metadata")),
        "submit_target": deepcopy(_SUBMIT_TARGET),
        "guardrails": deepcopy(_GUARDRAILS),
        "gaps": list(task_spec.get("gaps") or []),
        "warnings": list(task_spec.get("warnings") or []),
        "errors": _safe_errors(validation.get("errors")),
    }
    return plan


def validate_mpt_engine_submit_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """Validate a submit plan without executing the target."""

    if not isinstance(plan, dict):
        return {"ok": False, "errors": [_safe_error("plan must be a JSON object")]}

    errors: list[dict[str, str]] = []
    if plan.get("kind") != "mpt_engine_submit_plan":
        errors.append(_safe_error("plan.kind must be mpt_engine_submit_plan"))
    if plan.get("execution") != "dry_run":
        errors.append(_safe_error("plan.execution must be dry_run"))

    params = plan.get("mpt_params")
    if not isinstance(params, dict):
        errors.append(_safe_error("mpt_params must be a JSON object"))
        params = {}
    if not str(params.get("video_subject") or params.get("video_script") or "").strip():
        errors.append(_safe_error("mpt_params.video_subject or video_script is required"))

    metadata = plan.get("kurukin_metadata")
    if not isinstance(metadata, dict):
        errors.append(_safe_error("kurukin_metadata must be a JSON object"))

    if plan.get("submit_target") != _SUBMIT_TARGET:
        errors.append(_safe_error("submit_target must describe the native MPT target"))

    guardrails = plan.get("guardrails")
    if not isinstance(guardrails, dict):
        errors.append(_safe_error("guardrails must be a JSON object"))
    else:
        if guardrails.get("dry_run_required_by_default") is not True:
            errors.append(_safe_error("dry_run_required_by_default must be true"))
        if guardrails.get("real_submit_requires_explicit_authorization") is not True:
            errors.append(
                _safe_error("real_submit_requires_explicit_authorization must be true")
            )

    for marker in _FORBIDDEN_PLAN_MARKERS:
        if marker in plan:
            errors.append(_safe_error(f"plan must not include execution marker: {marker}"))

    if plan.get("ok") is not True:
        errors.extend(_safe_errors(plan.get("errors")))

    return {"ok": not errors, "errors": errors}


def summarize_mpt_engine_submit_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """Return a compact operator summary of a dry-run submit plan."""

    params = _as_dict(plan.get("mpt_params"))
    metadata = _as_dict(plan.get("kurukin_metadata"))
    materials = params.get("video_materials") or []
    validation = validate_mpt_engine_submit_plan(plan)
    return {
        "ok": bool(plan.get("ok")) and validation["ok"],
        "execution": plan.get("execution", ""),
        "submitted": bool(plan.get("submitted")),
        "dry_run": bool(plan.get("dry_run")),
        "validated_model": plan.get("validated_model", ""),
        "render_mode": metadata.get("render_mode", "normal"),
        "video_subject": params.get("video_subject", ""),
        "video_source": params.get("video_source", ""),
        "material_count": len(materials) if isinstance(materials, list) else 0,
        "submit_target": _as_dict(plan.get("submit_target")),
        "gap_count": len(plan.get("gaps") or []),
        "errors": validation["errors"],
    }


def submit_mpt_engine_plan(
    plan: dict[str, Any],
    *,
    executor: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Submit a plan only through an explicitly injected executor.

    The default path is a dry run and never touches the executor.
    """

    validation = validate_mpt_engine_submit_plan(plan)
    if not validation["ok"]:
        return {
            "ok": False,
            "submitted": False,
            "dry_run": bool(dry_run),
            "errors": validation["errors"],
        }

    if dry_run:
        return {
            "ok": True,
            "submitted": False,
            "dry_run": True,
            "execution": "dry_run",
            "validated_model": plan.get("validated_model", "VideoParams"),
            "submit_target": _as_dict(plan.get("submit_target")),
        }

    if os.environ.get(MPT_ENGINE_SUBMIT_FLAG) != "1":
        return {
            "ok": False,
            "submitted": False,
            "dry_run": False,
            "errors": [
                _safe_error(
                    f"real MPT engine submit is disabled; set {MPT_ENGINE_SUBMIT_FLAG}=1 and inject an executor"
                )
            ],
        }

    if executor is None:
        return {
            "ok": False,
            "submitted": False,
            "dry_run": False,
            "errors": [_safe_error("executor is required for non-dry-run submit")],
        }

    try:
        result = executor(deepcopy(plan))
    except Exception as exc:
        return {
            "ok": False,
            "submitted": False,
            "dry_run": False,
            "errors": [_safe_error(exc, error_type=exc.__class__.__name__)],
        }

    return {
        "ok": True,
        "submitted": True,
        "dry_run": False,
        "executor_result": _as_dict(result),
    }
