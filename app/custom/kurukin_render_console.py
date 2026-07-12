"""Pure helpers for the Kurukin Render Console Streamlit page."""

from __future__ import annotations

import os
import re
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
