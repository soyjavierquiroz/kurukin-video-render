"""Pure helpers for the Kurukin Render Console Streamlit page."""

from __future__ import annotations

import os
import re
import json
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from app.custom.aroll_broll_mode import build_aroll_broll_queue_payload
from app.custom.asset_materializer import (
    MAX_MATERIALIZED_ASSETS,
    materialize_assets_for_aroll_broll,
)
from app.custom.asset_hub_manifest import (
    load_asset_hub_renderer_manifest,
    summarize_asset_hub_manifest,
    validate_asset_hub_renderer_manifest,
)
from app.custom.asset_source_policy import (
    ASSET_SOURCE_PEXELS as POLICY_SOURCE_PEXELS,
    normalize_asset_source_policy,
    summarize_asset_source_policy,
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
from app.custom.kurukin_asset_hub import (
    KurukinAssetHubAuthError,
    KurukinAssetHubError,
    KurukinAssetHubUnavailableError,
    KurukinAssetHubValidationError,
    validate_asset_uid,
)
from app.custom.kurukin_asset_hub_wiring import (
    KurukinAssetHubMaterializationNotReady,
    KurukinAssetHubSelectionRequired,
    KurukinAssetHubWiringError,
    resolve_renderer_manifest_path,
)
from app.custom.kurukin_job_queue import (
    enqueue_job_intent,
    enqueue_moneyprinter_payload,
    is_aroll_broll_queue_enabled,
    sanitize_job_id,
)


DEFAULT_VOICE_NAME = "es-MX-DaliaNeural-Female"
MPT_ENGINE_SUBMIT_FLAG = "KURUKIN_ENABLE_MPT_ENGINE_SUBMIT"
PEXELS_SOURCE_FLAG = "KURUKIN_ENABLE_PEXELS_SOURCE"
SOURCE_MODE_ASSET_HUB = "asset_hub_bundle"
SOURCE_MODE_LOCAL = "local_assets"
SOURCE_MODE_STOCK = "stock_external"
ASSET_SOURCE_ASSET_HUB = SOURCE_MODE_ASSET_HUB
ASSET_SOURCE_LOCAL = SOURCE_MODE_LOCAL
ASSET_SOURCE_STOCK = SOURCE_MODE_STOCK
STOCK_SOURCES = {"pexels", "pixabay", "coverr"}
AROLL_BROLL_LOCAL_BROLL_ROOTS = (
    ("storage", "local_videos"),
    ("storage", "local_assets"),
    ("storage", "local_images"),
)
ASSET_HUB_OPERATOR_SOURCE_GENERIC = "Generic"
ASSET_HUB_OPERATOR_SOURCE_TITLE = "Title"
ASSET_HUB_OPERATOR_SOURCE_BRAND = "Brand"
ASSET_HUB_OPERATOR_SOURCE_TITLE_GENERIC = "Title + Generic"
ASSET_HUB_OPERATOR_SOURCE_BRAND_GENERIC = "Brand + Generic"
ASSET_HUB_OPERATOR_SOURCE_OPTIONS = (
    ASSET_HUB_OPERATOR_SOURCE_GENERIC,
    ASSET_HUB_OPERATOR_SOURCE_TITLE,
    ASSET_HUB_OPERATOR_SOURCE_BRAND,
    ASSET_HUB_OPERATOR_SOURCE_TITLE_GENERIC,
    ASSET_HUB_OPERATOR_SOURCE_BRAND_GENERIC,
)


def _clean_text(value: str) -> str:
    return str(value or "").strip()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def asset_hub_safe_operator_error_message(exc: Exception) -> str:
    """Return a safe Asset Hub UI message without response bodies or secrets."""

    if isinstance(exc, KurukinAssetHubAuthError):
        return "Kurukin Asset Hub authorization failed"
    if isinstance(exc, KurukinAssetHubValidationError):
        return str(exc) or "Kurukin Asset Hub validation failed"
    if isinstance(exc, KurukinAssetHubUnavailableError):
        return "Kurukin Asset Hub is temporarily unavailable"
    if isinstance(exc, KurukinAssetHubSelectionRequired):
        return str(exc) or "Kurukin Asset Hub selection is required"
    if isinstance(exc, KurukinAssetHubMaterializationNotReady):
        return "Kurukin Asset Hub bundle is not ready yet"
    if isinstance(exc, KurukinAssetHubWiringError):
        return str(exc) or "Kurukin Asset Hub wiring contract failed"
    if isinstance(exc, KurukinAssetHubError):
        return "Kurukin Asset Hub operation failed"
    return "Kurukin Asset Hub operation failed"


def build_asset_hub_operator_source_policy(
    option: str,
    *,
    title_slug: str = "",
    brand_slug: str = "",
) -> dict[str, Any]:
    """Build the only source_policy variants exposed to the operator UI."""

    label = _clean_text(option) or ASSET_HUB_OPERATOR_SOURCE_GENERIC
    if label not in ASSET_HUB_OPERATOR_SOURCE_OPTIONS:
        raise KurukinAssetHubValidationError("Asset Hub source policy is not supported")

    title = _clean_text(title_slug)
    brand = _clean_text(brand_slug)
    if label in {ASSET_HUB_OPERATOR_SOURCE_TITLE, ASSET_HUB_OPERATOR_SOURCE_TITLE_GENERIC}:
        if not title:
            raise KurukinAssetHubValidationError("Title source requires slug")
        sources = [{"scope": "title", "title": title}]
        if label == ASSET_HUB_OPERATOR_SOURCE_TITLE_GENERIC:
            sources.append({"scope": "generic"})
        return {"sources": sources}

    if label in {ASSET_HUB_OPERATOR_SOURCE_BRAND, ASSET_HUB_OPERATOR_SOURCE_BRAND_GENERIC}:
        if not brand:
            raise KurukinAssetHubValidationError("Brand source requires slug")
        sources = [{"scope": "brand", "brand": brand}]
        if label == ASSET_HUB_OPERATOR_SOURCE_BRAND_GENERIC:
            sources.append({"scope": "generic"})
        return {"sources": sources}

    return {"sources": [{"scope": "generic"}]}


def build_asset_hub_search_context(
    intent: dict[str, Any],
    source_policy: dict[str, Any],
    query_overrides_by_scene_id: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a deterministic, secret-free context for Asset Hub searches."""

    from app.custom.kurukin_asset_hub_wiring import build_asset_hub_search_requests

    overrides = {
        _clean_text(scene_id): _clean_text(query)
        for scene_id, query in (query_overrides_by_scene_id or {}).items()
        if _clean_text(scene_id)
    }
    request_by_scene_id = {}
    for request in build_asset_hub_search_requests(intent, source_policy):
        clean_scene_id = _clean_text(request.get("scene_id"))
        if clean_scene_id in overrides:
            request = dict(request)
            request["query"] = overrides[clean_scene_id]
        request_by_scene_id[clean_scene_id] = request

    scenes = []
    raw_scenes = intent.get("scenes") if isinstance(intent, dict) else []
    if not isinstance(raw_scenes, list):
        raw_scenes = []
    for fallback, scene in enumerate(raw_scenes, start=1):
        if not isinstance(scene, dict):
            continue
        raw_index = scene.get("scene_index", scene.get("index", fallback))
        try:
            scene_index = int(raw_index)
        except (TypeError, ValueError):
            scene_index = fallback
        scene_id = _clean_text(scene.get("scene_id")) or f"scene-{scene_index:03d}"
        request = request_by_scene_id.get(scene_id, {})
        scenes.append(
            {
                "scene_id": scene_id,
                "scene_index": scene_index,
                "script_scene": _clean_text(
                    scene.get("script_scene")
                    or scene.get("text")
                    or scene.get("caption")
                    or scene.get("description")
                    or request.get("script_scene")
                ),
                "query": _clean_text(request.get("query")),
            }
        )
    return {
        "source_policy": json.loads(_canonical_json(source_policy or {})),
        "scenes": scenes,
        "fingerprint": _canonical_json(
            {
                "source_policy": source_policy or {},
                "scenes": scenes,
            }
        ),
    }


def merge_asset_hub_search_result_with_context(
    search_result: dict[str, Any],
    search_context: dict[str, Any],
) -> dict[str, Any]:
    """Ensure every context scene is represented in a safe search result."""

    result = json.loads(_canonical_json(search_result or {}))
    selection = result.setdefault("asset_hub_selection", {})
    scenes = selection.setdefault("scenes", [])
    if not isinstance(scenes, list):
        scenes = []
        selection["scenes"] = scenes
    seen = {
        _clean_text(scene.get("scene_id"))
        for scene in scenes
        if isinstance(scene, dict)
    }
    for scene in (search_context or {}).get("scenes") or []:
        if not isinstance(scene, dict):
            continue
        scene_id = _clean_text(scene.get("scene_id"))
        if not scene_id or scene_id in seen:
            continue
        scenes.append(
            {
                "scene_id": scene_id,
                "scene_index": scene.get("scene_index"),
                "script_scene": _clean_text(scene.get("script_scene")),
                "query": _clean_text(scene.get("query")),
                "candidates": [],
            }
        )
        seen.add(scene_id)
    return result


def _valid_asset_selection_scene(scene: Any) -> bool:
    if not isinstance(scene, dict):
        return False
    return bool(
        _clean_text(scene.get("scene_id"))
        or _clean_text(scene.get("script_scene"))
        or _clean_text(scene.get("text"))
        or _clean_text(scene.get("caption"))
        or _clean_text(scene.get("description"))
        or _clean_text(scene.get("query"))
    )


def content_is_valid_for_asset_selection(result: dict[str, Any]) -> bool:
    """Return whether normalized content is enough to search/prepare assets."""

    if not isinstance(result, dict) or result.get("errors"):
        return False
    intent = result.get("intent")
    if not isinstance(intent, dict):
        return False
    topic_plan = intent.get("topic_plan")
    if not isinstance(topic_plan, dict):
        return False
    scenes = topic_plan.get("scenes") or intent.get("scenes") or []
    return any(_valid_asset_selection_scene(scene) for scene in scenes)


def rank_asset_hub_candidates_for_format(
    candidates: list[dict[str, Any]],
    target_format: str,
) -> list[dict[str, Any]]:
    """Return candidates ordered for UI presentation without changing provider data."""

    target = _clean_text(target_format).lower()
    if target == "vertical":
        priority_by_orientation = {
            "vertical-9x16": 0,
            "vertical-4x5": 1,
            "horizontal-16x9": 2,
        }
    elif target == "horizontal":
        priority_by_orientation = {"horizontal-16x9": 0}
    else:
        priority_by_orientation = {}

    def sort_key(indexed_candidate: tuple[int, dict[str, Any]]) -> tuple[int, int]:
        index, candidate = indexed_candidate
        orientation = _clean_text(candidate.get("orientation")).lower()
        priority = priority_by_orientation.get(orientation, 1 if target == "horizontal" else 3)
        return priority, index

    indexed = [
        (index, candidate)
        for index, candidate in enumerate(candidates or [])
        if isinstance(candidate, dict)
    ]
    return [candidate for _, candidate in sorted(indexed, key=sort_key)]


def clear_asset_hub_selection_widget_state(
    session_state: dict[str, Any],
    *,
    previous_search_result: dict[str, Any] | None = None,
    new_search_result: dict[str, Any] | None = None,
) -> list[str]:
    """Remove only Asset Hub scene multiselect widget keys from session state."""

    cleared: list[str] = []
    scene_ids = set()
    for result in (previous_search_result or {}, new_search_result or {}):
        scenes = (
            (result or {})
            .get("asset_hub_selection", {})
            .get("scenes", [])
        )
        for scene in scenes if isinstance(scenes, list) else []:
            if isinstance(scene, dict) and _clean_text(scene.get("scene_id")):
                scene_ids.add(_clean_text(scene.get("scene_id")))
    for scene_id in sorted(scene_ids):
        key = f"asset_hub_select_{scene_id}"
        if key in session_state:
            session_state.pop(key, None)
            cleared.append(key)
    return cleared


def build_asset_hub_prepare_context(
    search_context: dict[str, Any],
    selected_asset_uids_by_scene: dict[str, Any],
    *,
    job_context_fingerprint: str = "",
    mpt_spec: dict[str, Any] | None = None,
    mpt_spec_fingerprint: str = "",
) -> dict[str, Any]:
    """Return a deterministic context for a prepared explicit selection."""

    selected: dict[str, list[str]] = {}
    for scene_id in sorted((selected_asset_uids_by_scene or {}).keys()):
        clean_scene_id = _clean_text(scene_id)
        values = selected_asset_uids_by_scene.get(scene_id)
        if not isinstance(values, list):
            raise KurukinAssetHubValidationError(
                "selected_asset_uids must be a list per scene"
            )
        selected[clean_scene_id] = [
            validate_asset_uid(
                asset_uid,
                field_name=f"{clean_scene_id}.selected_asset_uids",
            )
            for asset_uid in values
        ]
    context = {
        "search_context_fingerprint": _clean_text(
            (search_context or {}).get("fingerprint")
        ),
        "selected_asset_uids_by_scene": selected,
        "job_context_fingerprint": _clean_text(job_context_fingerprint)
        or _clean_text(mpt_spec_fingerprint)
        or build_asset_hub_job_spec_fingerprint(mpt_spec or {}),
    }
    context["fingerprint"] = _canonical_json(context)
    return context


def build_asset_hub_job_spec_fingerprint(mpt_spec: dict[str, Any]) -> str:
    """Return a deterministic, secret-free fingerprint for the MPT spec context."""

    if not isinstance(mpt_spec, dict):
        raise KurukinAssetHubValidationError("mpt_spec must be a JSON object")
    normalized = json.loads(_canonical_json(mpt_spec))
    normalized.pop("asset_hub", None)
    return _canonical_json(normalized)


def asset_hub_search_context_matches(
    stored_context: dict[str, Any] | None,
    current_context: dict[str, Any] | None,
) -> bool:
    return bool(
        stored_context
        and current_context
        and stored_context.get("fingerprint") == current_context.get("fingerprint")
    )


def asset_hub_prepare_context_matches(
    stored_context: dict[str, Any] | None,
    current_context: dict[str, Any] | None,
) -> bool:
    return asset_hub_search_context_matches(stored_context, current_context)


def validate_asset_hub_scene_selections(
    search_result: dict[str, Any],
    selected_asset_uids_by_scene: dict[str, Any],
) -> dict[str, Any]:
    """Validate explicit string asset_uid selections against search scenes."""

    selection = (
        (search_result or {})
        .get("asset_hub_selection", {})
        .get("scenes", [])
    )
    missing: list[str] = []
    empty_candidate_scenes: list[str] = []
    unknown: dict[str, list[str]] = {}
    normalized: dict[str, list[str]] = {}
    for scene in selection if isinstance(selection, list) else []:
        if not isinstance(scene, dict):
            continue
        scene_id = _clean_text(scene.get("scene_id"))
        if not scene_id:
            continue
        candidates = scene.get("candidates") if isinstance(scene.get("candidates"), list) else []
        candidate_asset_uids = {
            _clean_text(candidate.get("asset_uid"))
            for candidate in candidates
            if isinstance(candidate, dict) and _clean_text(candidate.get("asset_uid"))
        }
        if not candidates:
            empty_candidate_scenes.append(scene_id)
        raw_values = (selected_asset_uids_by_scene or {}).get(scene_id) or []
        if not isinstance(raw_values, list):
            raise KurukinAssetHubValidationError(
                f"{scene_id}.selected_asset_uids must be a list"
            )
        clean_values = [
            validate_asset_uid(asset_uid, field_name=f"{scene_id}.selected_asset_uids")
            for asset_uid in raw_values
        ]
        if not clean_values:
            missing.append(scene_id)
        unknown_values = [
            asset_uid for asset_uid in clean_values if asset_uid not in candidate_asset_uids
        ]
        if unknown_values:
            unknown[scene_id] = unknown_values
        normalized[scene_id] = clean_values
    return {
        "ok": not missing and not empty_candidate_scenes and not unknown and bool(normalized),
        "missing_scene_ids": missing,
        "empty_candidate_scene_ids": empty_candidate_scenes,
        "unknown_asset_uids_by_scene": unknown,
        "selected_asset_uids_by_scene": normalized,
    }


def apply_prepared_asset_hub_contract_to_spec(
    spec: dict[str, Any],
    prepare_result: dict[str, Any],
) -> dict[str, Any]:
    """Copy the prepared Asset Hub renderer contract into an MPT spec."""

    asset_hub = (prepare_result or {}).get("asset_hub")
    if not isinstance(asset_hub, dict) or not asset_hub.get("renderer_manifest_path"):
        raise KurukinAssetHubValidationError("Prepared Asset Hub contract is required")
    updated = json.loads(_canonical_json(spec or {}))
    updated["asset_hub"] = dict(asset_hub)
    return updated


def build_moneyprinter_payload_with_prepared_asset_hub(
    spec: dict[str, Any],
    prepare_result: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Insert the prepared Asset Hub contract before building the MPT payload."""

    prepared_spec = apply_prepared_asset_hub_contract_to_spec(spec, prepare_result)
    payload, summary = validate_and_build_payload_from_console_spec(prepared_spec)
    return prepared_spec, payload, summary


def require_prepared_asset_hub_payload_for_queue(
    *,
    stored_prepare_context: dict[str, Any] | None,
    current_prepare_context: dict[str, Any] | None,
    prepared_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return the exact prepared payload Queue may enqueue, or a NEEDS_INPUT result."""

    if not asset_hub_prepare_context_matches(
        stored_prepare_context,
        current_prepare_context,
    ):
        return {
            "ok": False,
            "status": "NEEDS_INPUT",
            "reason": "asset_hub_prepare_required",
            "payload": {},
        }
    if not isinstance(prepared_payload, dict) or not prepared_payload:
        return {
            "ok": False,
            "status": "NEEDS_INPUT",
            "reason": "asset_hub_prepared_payload_missing",
            "payload": {},
        }
    return {
        "ok": True,
        "status": "READY_TO_SUBMIT",
        "reason": "",
        "payload": prepared_payload,
    }


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
    return resolve_renderer_manifest_path(safe_bundle_uid)


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


def _validate_aroll_broll_local_asset_path(value: str) -> str:
    text = _clean_text(value)
    if not text:
        raise ValueError("B-roll local asset path cannot be empty")

    path = PurePosixPath(text)
    if path.is_absolute() or _has_parent_path(path) or "\\" in text:
        raise ValueError("B-roll local asset path must stay under storage")
    if len(path.parts) < 3 or path.parts[:2] not in AROLL_BROLL_LOCAL_BROLL_ROOTS:
        raise ValueError(
            "B-roll local asset path must stay under storage/local_videos, "
            "storage/local_assets, or storage/local_images"
        )
    extension = path.suffix.lower().lstrip(".")
    if extension not in ALLOWED_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_EXTENSIONS))
        raise ValueError(f"B-roll local asset must use one of: {allowed}")
    return path.as_posix()


def _has_parent_path(path: PurePosixPath) -> bool:
    return ".." in path.parts


def is_mpt_engine_submit_enabled(environ: dict[str, str] | None = None) -> bool:
    """Return whether native MPT submit is explicitly enabled."""

    source = environ if environ is not None else os.environ
    return source.get(MPT_ENGINE_SUBMIT_FLAG) == "1"


def _redact_mpt_submit_message(value: Any) -> str:
    message = str(value or "")
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
    for word in sensitive_words:
        message = re.sub(
            rf"(?i)({re.escape(word)})(\s*[=:]\s*)([^\s,;]+)",
            r"\1\2<redacted>",
            message,
        )
    if any(word in message.lower() for word in sensitive_words):
        return "<redacted>"
    return message


def _safe_mpt_submit_error(exc: Exception) -> str:
    return _redact_mpt_submit_message(_safe_error_message(exc))


def _safe_mpt_local_material_path(value: str) -> str:
    text = _clean_text(value)
    if not text:
        raise ValueError("video local path is required")
    path = PurePosixPath(text)
    if path.is_absolute() or "\\" in text or _has_parent_path(path):
        raise ValueError("video local path must stay under storage/local_videos")
    parts = path.parts
    if len(parts) >= 3 and parts[:2] == ("storage", "local_videos"):
        path = PurePosixPath(*parts[2:])
    elif parts and parts[0] == "local_videos":
        path = PurePosixPath(*parts[1:])
    if not path.parts:
        raise ValueError("video local path is required")
    extension = path.suffix.lower().lstrip(".")
    if extension not in ALLOWED_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_EXTENSIONS))
        raise ValueError(f"video local path must use one of: {allowed}")
    return path.as_posix()


def _safe_mpt_audio_path(value: str) -> str:
    text = _clean_text(value)
    if not text:
        raise ValueError("audio local path is required")
    path = PurePosixPath(text)
    if path.is_absolute() or "\\" in text or _has_parent_path(path):
        raise ValueError("audio local path must stay under storage/local_audios")
    parts = path.parts
    if len(parts) >= 3 and parts[:2] == ("storage", "local_audios"):
        normalized = path
    elif parts and parts[0] == "local_audios":
        normalized = PurePosixPath("storage", *parts)
    else:
        normalized = PurePosixPath("storage", "local_audios", *parts)
    extension = normalized.suffix.lower().lstrip(".")
    if extension not in ALLOWED_AUDIO_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_AUDIO_EXTENSIONS))
        raise ValueError(f"audio local path must use one of: {allowed}")
    return normalized.as_posix()


def submit_mpt_native_local_job_from_console(
    *,
    task_id: str,
    video_local_path: str,
    audio_local_path: str,
    video_subject: str,
    video_script: str,
    environ: dict[str, str] | None = None,
    task_start=None,
    bridge_builder=None,
    video_params_model=None,
) -> dict[str, Any]:
    """Submit a local-only job directly to native MPT when explicitly enabled."""

    clean_task_id = _clean_text(task_id)
    expected_output = (
        f"storage/tasks/{clean_task_id}/final-1.mp4" if clean_task_id else ""
    )
    if not is_mpt_engine_submit_enabled(environ):
        return {
            "ok": False,
            "task_id": clean_task_id,
            "expected_output": expected_output,
            "errors": [f"{MPT_ENGINE_SUBMIT_FLAG}=1 is required"],
        }

    try:
        if not clean_task_id:
            raise ValueError("task_id is required")
        if sanitize_job_id(clean_task_id) != clean_task_id:
            raise ValueError("task_id must contain only letters, numbers, - or _")
        material_path = _safe_mpt_local_material_path(video_local_path)
        audio_path = _safe_mpt_audio_path(audio_local_path)
        subject = _clean_text(video_subject) or "Kurukin local MPT render"
        script = _clean_text(video_script) or subject

        job = {
            "task_id": clean_task_id,
            "render_mode": "mpt_native_local_console",
            "video_subject": subject,
            "video_script": script,
            "video_terms": "",
            "video_source": "local",
            "video_materials": [
                {
                    "provider": "local",
                    "url": material_path,
                    "duration": 0,
                }
            ],
            "custom_audio_file": audio_path,
            "subtitle_enabled": False,
            "subtitle_provider": "none",
            "asset_policy": {
                "mode": "local_only",
                "allowed_sources": ["local"],
            },
            "kurukin_metadata": {
                "source": "render_console_mpt_native_local_submit",
                "external_providers_allowed": False,
            },
        }

        if bridge_builder is None:
            from app.custom.mpt_engine_bridge import (
                build_validated_mpt_video_task_from_kurukin_job,
            )

            bridge_builder = build_validated_mpt_video_task_from_kurukin_job
        validated = bridge_builder(job)
        if not validated.get("ok"):
            errors = [
                _redact_mpt_submit_message(
                    item.get("message") if isinstance(item, dict) else item
                )
                for item in validated.get("errors") or []
            ]
            return {
                "ok": False,
                "task_id": clean_task_id,
                "expected_output": expected_output,
                "errors": errors or ["VideoParams validation failed"],
            }

        params_payload = validated["spec"]
        if video_params_model is None:
            from app.models.schema import VideoParams

            video_params_model = VideoParams
        if hasattr(video_params_model, "model_validate"):
            params = video_params_model.model_validate(params_payload)
        elif hasattr(video_params_model, "parse_obj"):
            params = video_params_model.parse_obj(params_payload)
        else:
            params = video_params_model(**params_payload)

        if task_start is None:
            from app.services import task as task_service

            task_start = task_service.start
        task_start(task_id=clean_task_id, params=params)

        return {
            "ok": True,
            "task_id": clean_task_id,
            "expected_output": expected_output,
            "errors": [],
            "validated_model": validated.get("validated_model", "VideoParams"),
        }
    except Exception as exc:
        return {
            "ok": False,
            "task_id": clean_task_id,
            "expected_output": expected_output,
            "errors": [_safe_mpt_submit_error(exc)],
        }


INTENT_QUEUE_PROCESSABLE_STATUSES = {"QUEUED", "PENDING"}
INTENT_QUEUE_DONE_STATUS = "DONE"
INTENT_QUEUE_FAILED_STATUS = "FAILED"
INTENT_QUEUE_PROCESSING_STATUS = "PROCESSING"


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    temp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temp_path.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def _looks_like_url(value: Any) -> bool:
    return bool(re.search(r"(?i)\b(?:https?|ftp|s3)://", str(value or "")))


def _find_url_value(value: Any, *, path: str = "") -> str:
    if isinstance(value, dict):
        for key, item in value.items():
            found = _find_url_value(item, path=f"{path}.{key}" if path else str(key))
            if found:
                return found
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found = _find_url_value(item, path=f"{path}[{index}]")
            if found:
                return found
    elif isinstance(value, str) and _looks_like_url(value):
        return path or "value"
    return ""


def _first_material_path(mpt_params: dict[str, Any]) -> str:
    materials = mpt_params.get("video_materials")
    if not isinstance(materials, list) or not materials:
        return ""
    first = materials[0]
    if isinstance(first, dict):
        return _clean_text(first.get("url") or first.get("path"))
    return _clean_text(first)


def _validate_intent_queue_payload_for_manual_process(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if payload.get("source") != "job_intent_v1":
        errors.append("queue item source must be job_intent_v1")

    status = _clean_text(payload.get("status")).upper()
    if status not in INTENT_QUEUE_PROCESSABLE_STATUSES:
        errors.append("queue item status must be QUEUED or PENDING")

    mpt_spec = payload.get("compiled_mpt_spec")
    if not isinstance(mpt_spec, dict) or not mpt_spec:
        errors.append("compiled_mpt_spec is required")
        mpt_spec = {}
    mpt_params = mpt_spec.get("mpt_params")
    if not isinstance(mpt_params, dict) or not mpt_params:
        errors.append("compiled_mpt_spec.mpt_params is required")
        mpt_params = {}

    guardrails = payload.get("guardrails")
    if not isinstance(guardrails, dict):
        errors.append("guardrails are required")
        guardrails = {}
    for key in (
        "external_providers_allowed",
        "ai_generation_allowed",
        "asset_hub_api_allowed",
    ):
        if guardrails.get(key) is not False:
            errors.append(f"guardrails.{key} must be false")

    if _clean_text(mpt_params.get("video_source")) != "local":
        errors.append("compiled_mpt_spec.mpt_params.video_source must be local")
    if _clean_text(mpt_params.get("asset_hub_renderer_manifest_path")):
        errors.append("asset_hub_renderer_manifest_path is not allowed")

    materials = mpt_params.get("video_materials")
    if not isinstance(materials, list) or not materials:
        errors.append("compiled_mpt_spec.mpt_params.video_materials is required")
    else:
        for index, material in enumerate(materials):
            if not isinstance(material, dict):
                errors.append(f"video_materials[{index}] must be an object")
                continue
            if _clean_text(material.get("provider")) != "local":
                errors.append(f"video_materials[{index}].provider must be local")
            if not _clean_text(material.get("url") or material.get("path")):
                errors.append(f"video_materials[{index}].url is required")

    audio_path = _clean_text(mpt_params.get("custom_audio_file"))
    if not audio_path:
        errors.append("compiled_mpt_spec.mpt_params.custom_audio_file is required")

    url_field = _find_url_value(
        {
            "original_intent": payload.get("original_intent"),
            "normalized_intent": payload.get("normalized_intent"),
            "resolved_visual_path": payload.get("resolved_visual_path"),
            "compiled_mpt_spec": mpt_spec,
        }
    )
    if url_field:
        errors.append(f"URLs are not allowed in queue item: {url_field}")

    return errors


def _queue_process_result(
    *,
    ok: bool,
    queue_item_path: Path,
    payload: dict[str, Any] | None = None,
    status: str = "",
    output_path: str = "",
    errors: list[str] | None = None,
) -> dict[str, Any]:
    source = payload or {}
    return {
        "ok": ok,
        "pending_path": queue_item_path.as_posix(),
        "task_id": _clean_text(source.get("task_id")),
        "status": status or _clean_text(source.get("status")),
        "source": _clean_text(source.get("source")),
        "mode": _clean_text(source.get("mode")),
        "resolved_visual_path": _clean_text(source.get("resolved_visual_path")),
        "visual_autofill_source": _clean_text(source.get("visual_autofill_source")),
        "output_path": output_path or _clean_text(source.get("output_path")),
        "errors": errors or [],
    }


def process_queued_intent_with_mpt_native(
    queue_item_path: str | Path,
    *,
    environ: dict[str, str] | None = None,
    submitter=None,
) -> dict[str, Any]:
    """Manually process one job_intent_v1 pending item through native MPT."""

    path = Path(queue_item_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError) as exc:
        return _queue_process_result(
            ok=False,
            queue_item_path=path,
            errors=[_safe_mpt_submit_error(exc)],
        )
    if not isinstance(payload, dict):
        return _queue_process_result(
            ok=False,
            queue_item_path=path,
            errors=["queue item must be a JSON object"],
        )

    validation_errors = _validate_intent_queue_payload_for_manual_process(payload)
    if validation_errors:
        return _queue_process_result(
            ok=False,
            queue_item_path=path,
            payload=payload,
            errors=validation_errors,
        )
    if not is_mpt_engine_submit_enabled(environ):
        return _queue_process_result(
            ok=False,
            queue_item_path=path,
            payload=payload,
            errors=[f"{MPT_ENGINE_SUBMIT_FLAG}=1 is required"],
        )

    mpt_params = payload["compiled_mpt_spec"]["mpt_params"]
    task_id = _clean_text(payload.get("task_id"))
    output_path = f"storage/tasks/{task_id}/final-1.mp4" if task_id else ""
    processing_payload = dict(payload)
    processing_payload.update(
        {
            "status": INTENT_QUEUE_PROCESSING_STATUS,
            "processing_started_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    _write_json_atomic(path, processing_payload)

    if submitter is None:
        submitter = submit_mpt_native_local_job_from_console

    result = submitter(
        task_id=task_id,
        video_local_path=_first_material_path(mpt_params),
        audio_local_path=_clean_text(mpt_params.get("custom_audio_file")),
        video_subject=_clean_text(mpt_params.get("video_subject")),
        video_script=_clean_text(mpt_params.get("video_script")),
        environ=environ,
    )
    final_payload = dict(processing_payload)
    final_payload["processed_at"] = datetime.now(timezone.utc).isoformat()
    final_payload["manual_process_result"] = result
    if result.get("ok"):
        output_path = _clean_text(result.get("output_path") or result.get("expected_output")) or output_path
        final_payload["status"] = INTENT_QUEUE_DONE_STATUS
        final_payload["output_path"] = output_path
        final_payload["guardrails"] = dict(final_payload.get("guardrails") or {})
        final_payload["guardrails"]["real_render_started"] = True
        errors: list[str] = []
    else:
        final_payload["status"] = INTENT_QUEUE_FAILED_STATUS
        final_payload["error"] = "; ".join(str(item) for item in result.get("errors") or ["MPT native submit failed"])
        errors = [final_payload["error"]]
    _write_json_atomic(path, final_payload)

    return _queue_process_result(
        ok=bool(result.get("ok")),
        queue_item_path=path,
        payload=final_payload,
        status=final_payload["status"],
        output_path=output_path,
        errors=errors,
    )


def _mark_intent_queue_item_failed(path: Path, errors: list[str]) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("source") != "job_intent_v1":
        return None
    failed_payload = dict(payload)
    failed_payload["status"] = INTENT_QUEUE_FAILED_STATUS
    failed_payload["error"] = "; ".join(errors or ["batch manual process failed"])
    failed_payload["processed_at"] = datetime.now(timezone.utc).isoformat()
    _write_json_atomic(path, failed_payload)
    return failed_payload


def process_queued_intent_batch_with_mpt_native(
    queue_item_paths: list[str | Path],
    *,
    max_items: int = 5,
    continue_on_error: bool = True,
    environ: dict[str, str] | None = None,
    submitter=None,
    processor=None,
) -> dict[str, Any]:
    """Manually process multiple job_intent_v1 queue items through native MPT."""

    try:
        limit = int(max_items)
    except (TypeError, ValueError):
        limit = 5
    limit = max(1, min(limit, 5))
    selected_paths = [Path(path) for path in (queue_item_paths or [])[:limit]]
    if not selected_paths:
        return {
            "ok": False,
            "processed": 0,
            "done": 0,
            "failed": 0,
            "items": [],
            "errors": ["queue_item_paths is required"],
        }
    if not is_mpt_engine_submit_enabled(environ):
        return {
            "ok": False,
            "processed": 0,
            "done": 0,
            "failed": 0,
            "items": [],
            "errors": [f"{MPT_ENGINE_SUBMIT_FLAG}=1 is required"],
        }

    if processor is None:
        processor = process_queued_intent_with_mpt_native

    items: list[dict[str, Any]] = []
    errors: list[str] = []
    done = 0
    failed = 0
    for path in selected_paths:
        result = processor(path, environ=environ, submitter=submitter)
        item_errors = [str(error) for error in result.get("errors") or []]
        if result.get("ok"):
            done += 1
            status = result.get("status") or INTENT_QUEUE_DONE_STATUS
        else:
            failed += 1
            status = result.get("status") or INTENT_QUEUE_FAILED_STATUS
            errors.extend(item_errors)
            if status != INTENT_QUEUE_FAILED_STATUS:
                failed_payload = _mark_intent_queue_item_failed(path, item_errors)
                if failed_payload is not None:
                    status = INTENT_QUEUE_FAILED_STATUS
                    result = dict(result)
                    result["status"] = status
                    result["task_id"] = _clean_text(failed_payload.get("task_id"))
            if not continue_on_error:
                items.append(
                    {
                        "queue_item_path": result.get("pending_path", path.as_posix()),
                        "task_id": result.get("task_id", ""),
                        "status": status,
                        "output_path": result.get("output_path", ""),
                        "error": "; ".join(item_errors),
                    }
                )
                break
        items.append(
            {
                "queue_item_path": result.get("pending_path", path.as_posix()),
                "task_id": result.get("task_id", ""),
                "status": status,
                "output_path": result.get("output_path", ""),
                "error": "; ".join(item_errors),
            }
        )

    processed = len(items)
    return {
        "ok": processed > 0 and failed == 0,
        "processed": processed,
        "done": done,
        "failed": failed,
        "items": items,
        "errors": errors,
    }


def normalize_aroll_broll_local_asset_paths(value: str | list[Any]) -> list[str]:
    """Return safe storage-relative B-roll asset paths from UI text/list input."""

    if isinstance(value, str):
        raw_items = [
            line.strip()
            for line in value.replace(",", "\n").splitlines()
            if line.strip()
        ]
    elif isinstance(value, list):
        raw_items = []
        for item in value:
            if isinstance(item, dict):
                raw_items.append(str(item.get("path") or "").strip())
            else:
                raw_items.append(str(item or "").strip())
        raw_items = [item for item in raw_items if item]
    else:
        raw_items = []

    normalized: list[str] = []
    for item in raw_items:
        safe = _validate_aroll_broll_local_asset_path(item)
        if safe not in normalized:
            normalized.append(safe)
    return normalized


def _clamp_prepare_broll_count(value: Any) -> int:
    try:
        count = int(value)
    except (TypeError, ValueError):
        count = 1
    return max(1, min(MAX_MATERIALIZED_ASSETS, count))


def is_pexels_source_enabled(environ: dict[str, str] | None = None) -> bool:
    """Return whether controlled Pexels sourcing is explicitly enabled."""

    source = environ if environ is not None else os.environ
    return source.get(PEXELS_SOURCE_FLAG) == "1"


def _prepare_broll_error_message(
    errors: list[str],
    *,
    pexels_enabled: bool = False,
) -> str:
    if "External downloader is not configured" in errors:
        if not pexels_enabled:
            return "No hay suficientes assets locales. Pexels no está activo en esta consola."
        return "No hay suficientes assets locales y no hay downloader configurado."
    if errors:
        return errors[0]
    return "No se pudieron preparar assets B-roll."


def prepare_broll_assets_from_console(
    *,
    project_root: Path,
    asset_policy: dict | None,
    query: str | None,
    desired_count: int,
    local_candidates: str | list[str] | None,
    output_dir: str | None = None,
    downloader=None,
    source_adapters=None,
    pexels_downloader=None,
    environ: dict[str, str] | None = None,
    manifest_reader=None,
    local_library_resolver=None,
) -> dict[str, Any]:
    """Prepare local B-roll assets for UI state without enqueueing or rendering."""

    normalized_policy = normalize_asset_source_policy(asset_policy)
    pexels_enabled = is_pexels_source_enabled(environ)
    effective_adapters = dict(source_adapters or {})
    if pexels_enabled and pexels_downloader is not None:
        effective_adapters.setdefault(POLICY_SOURCE_PEXELS, pexels_downloader)
    policy_summary = summarize_asset_source_policy(normalized_policy)
    try:
        candidates = normalize_aroll_broll_local_asset_paths(local_candidates or "")
    except ValueError as exc:
        return {
            "ok": False,
            "error": _safe_error_message(exc),
            "asset_policy": normalized_policy,
            "asset_policy_label": policy_summary["console_label"],
        }

    materializer_request: dict[str, Any] = {
        "asset_policy": normalized_policy,
        "query": _clean_text(query),
        "desired_count": _clamp_prepare_broll_count(desired_count),
        "local_candidates": candidates,
    }
    if output_dir:
        materializer_request["output_dir"] = _clean_text(output_dir)
    if isinstance(asset_policy, dict):
        manifest_path = _clean_text(asset_policy.get("manifest_path"))
        if manifest_path:
            materializer_request["manifest_path"] = manifest_path
        brand_uid = _clean_text(asset_policy.get("brand_asset_bundle_uid"))
        if brand_uid:
            materializer_request["brand_asset_bundle_uid"] = brand_uid

    try:
        result = materialize_assets_for_aroll_broll(
            materializer_request,
            project_root=project_root,
            downloader=downloader,
            source_adapters=effective_adapters,
            manifest_reader=manifest_reader,
            local_library_resolver=local_library_resolver,
        )
    except Exception as exc:
        return {
            "ok": False,
            "error": _safe_error_message(exc),
            "asset_policy": normalized_policy,
            "asset_policy_label": policy_summary["console_label"],
            "query": materializer_request["query"],
            "b_roll_assets": [],
            "b_roll_asset_count": 0,
            "source_provider": "",
        }
    if not result.get("ok"):
        errors = [str(error) for error in result.get("errors") or []]
        return {
            "ok": False,
            "error": _prepare_broll_error_message(
                errors,
                pexels_enabled=pexels_enabled,
            ),
            "asset_policy": result.get("source_policy") or normalized_policy,
            "asset_policy_label": policy_summary["console_label"],
            "query": materializer_request["query"],
            "b_roll_assets": list(result.get("b_roll_assets") or []),
            "b_roll_asset_count": int(result.get("b_roll_asset_count") or 0),
            "source_provider": result.get("source_provider") or "",
        }

    assets = list(result.get("b_roll_assets") or [])
    response_policy = result.get("source_policy") or normalized_policy
    response_summary = summarize_asset_source_policy(response_policy)
    return {
        "ok": True,
        "b_roll_assets": assets,
        "b_roll_asset_count": len(assets),
        "source_provider": result.get("source_provider") or "",
        "asset_policy": response_policy,
        "asset_policy_label": response_summary["console_label"],
        "query": materializer_request["query"],
        "message": "B-roll assets preparados",
        "metadata": result.get("metadata") or {},
    }


def build_aroll_broll_payload_from_console(
    config: dict[str, Any],
    *,
    job_id: str,
    project_root: str | Path | None = None,
    render_quality: str = "draft_720p",
    title: str = "A-roll/B-roll",
    task_id: str = "",
    created_by: str = "render_console_ui",
) -> dict[str, Any]:
    """Build the guarded A-roll/B-roll payload used by Render Console."""

    clean_job_id = _clean_text(job_id)
    clean_task_id = _clean_text(task_id) or clean_job_id
    if sanitize_job_id(clean_job_id) != clean_job_id:
        raise ValueError("job_id must contain only letters, numbers, - or _")
    if sanitize_job_id(clean_task_id) != clean_task_id:
        raise ValueError("task_id must contain only letters, numbers, - or _")

    payload = build_aroll_broll_queue_payload(
        config,
        job_id=clean_job_id,
        project_root=project_root,
        render_quality=render_quality,
        title=title,
        strict=True,
    )
    payload["task_id"] = clean_task_id
    if created_by:
        payload["created_by"] = _clean_text(created_by)
    runner = payload.setdefault("runner", {})
    if isinstance(runner, dict):
        runner["task_id"] = clean_task_id
        runner["created_by"] = payload.get("created_by", "")
    return payload


def enqueue_aroll_broll_from_console(
    config: dict[str, Any],
    *,
    job_id: str,
    project_root: str | Path | None = None,
    queue_dir: str | Path = "storage/nightly_jobs/pending",
    render_quality: str = "draft_720p",
    title: str = "A-roll/B-roll",
    task_id: str = "",
    created_by: str = "render_console_ui",
    environ: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Create a pending A-roll/B-roll job through the Render Console guard."""

    if not is_aroll_broll_queue_enabled(environ):
        raise ValueError("A-roll/B-roll queue flag is disabled")

    payload = build_aroll_broll_payload_from_console(
        config,
        job_id=job_id,
        project_root=project_root,
        render_quality=render_quality,
        title=title,
        task_id=task_id,
        created_by=created_by,
    )
    path = enqueue_moneyprinter_payload(payload, queue_dir=queue_dir)
    return {
        "pending_path": path.as_posix(),
        "job_id": payload.get("job_id"),
        "task_id": payload.get("task_id"),
        "render_mode": payload.get("render_mode"),
        "payload": payload,
    }


def enqueue_job_intent_from_console(
    intent: dict[str, Any],
    *,
    queue_dir: str | Path = "storage/nightly_jobs/pending",
    project_root: str | Path | None = None,
    now=None,
) -> dict[str, Any]:
    """Queue a compiled job intent without rendering or calling providers."""

    return enqueue_job_intent(
        intent,
        queue_dir=queue_dir,
        project_root=project_root,
        now=now,
    )


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
