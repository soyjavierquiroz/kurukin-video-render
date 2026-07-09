"""Pure bridge from Kurukin job intent to native MoneyPrinterTurbo task specs."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.custom.asset_source_policy import (
    ASSET_SOURCE_MODE_LOCAL_ONLY,
    ASSET_SOURCE_MODE_OPEN_SOURCES,
    normalize_asset_source_policy,
    summarize_asset_source_policy,
    validate_asset_source_policy,
)


MPT_VIDEO_SOURCE_PEXELS = "pexels"
MPT_VIDEO_SOURCE_PIXABAY = "pixabay"
MPT_VIDEO_SOURCE_COVERR = "coverr"
MPT_VIDEO_SOURCE_LOCAL = "local"

MPT_SUPPORTED_VIDEO_SOURCES = (
    MPT_VIDEO_SOURCE_PEXELS,
    MPT_VIDEO_SOURCE_PIXABAY,
    MPT_VIDEO_SOURCE_COVERR,
    MPT_VIDEO_SOURCE_LOCAL,
)

RENDER_MODE_AROLL_BROLL = "aroll_broll"

_RENDER_QUALITY_TO_MPT_RESOLUTION = {
    "draft_720p": "720p",
    "standard_1080p": "1080p",
    "premium_2k": "2k",
    "720p": "720p",
    "1080p": "1080p",
    "2k": "2k",
}


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


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


def _first_clean_text(*values: Any) -> str:
    for value in values:
        clean = _clean_text(value)
        if clean:
            return clean
    return ""


def _normalize_render_quality(value: Any) -> str:
    clean = _clean_text(value)
    return _RENDER_QUALITY_TO_MPT_RESOLUTION.get(clean, clean)


def _material_info(path_or_material: Any, *, provider: str = "local") -> dict[str, Any]:
    if isinstance(path_or_material, dict):
        material = deepcopy(path_or_material)
        if "url" not in material:
            material["url"] = _first_clean_text(
                material.get("path"),
                material.get("local_path"),
                material.get("file_path"),
                material.get("source_path"),
                material.get("resolved_path"),
            )
        material["provider"] = _clean_text(material.get("provider")) or provider
        material.setdefault("duration", 0)
        return material

    return {
        "provider": provider,
        "url": _clean_text(path_or_material),
        "duration": 0,
    }


def _local_materials_from_job(job: dict[str, Any]) -> list[dict[str, Any]]:
    materials: list[dict[str, Any]] = []
    for item in _as_list(job.get("video_materials")):
        material = _material_info(item)
        if material.get("url"):
            materials.append(material)

    for item in _as_list(job.get("selectedAssets")) + _as_list(
        job.get("selected_assets")
    ):
        path = _first_clean_text(
            item.get("path") if isinstance(item, dict) else item,
            item.get("local_path") if isinstance(item, dict) else "",
            item.get("url") if isinstance(item, dict) else "",
        )
        if path:
            materials.append(_material_info({"provider": "local", "url": path}))

    return _dedupe_materials(materials)


def _dedupe_materials(materials: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for material in materials:
        url = _clean_text(material.get("url"))
        if not url or url in seen:
            continue
        seen.add(url)
        deduped.append(material)
    return deduped


def _stock_source_from_job(job: dict[str, Any], asset_policy: dict[str, Any]) -> str:
    requested = _first_clean_text(job.get("stock_source"), job.get("video_source"))
    if requested in MPT_SUPPORTED_VIDEO_SOURCES:
        return requested

    allowed = asset_policy.get("allowed_sources") or []
    for source in (
        MPT_VIDEO_SOURCE_PEXELS,
        MPT_VIDEO_SOURCE_PIXABAY,
        MPT_VIDEO_SOURCE_COVERR,
    ):
        if source in allowed:
            return source
    return MPT_VIDEO_SOURCE_PEXELS


def _base_mpt_params(job: dict[str, Any]) -> dict[str, Any]:
    video_subject = _first_clean_text(
        job.get("video_subject"),
        job.get("subject"),
        job.get("title"),
        job.get("name"),
        "Kurukin video",
    )
    return {
        "video_subject": video_subject,
        "video_script": _first_clean_text(
            job.get("video_script"),
            job.get("script"),
            job.get("transcript"),
            job.get("caption_script"),
        ),
        "video_terms": deepcopy(job.get("video_terms", job.get("search_terms", None))),
        "video_aspect": _first_clean_text(
            job.get("video_aspect"), job.get("aspect_ratio"), "9:16"
        ),
        "video_resolution": _normalize_render_quality(
            _first_clean_text(
                job.get("video_resolution"), job.get("render_quality")
            )
        ),
        "video_concat_mode": _first_clean_text(
            job.get("video_concat_mode"), "sequential"
        ),
        "video_clip_duration": int(job.get("video_clip_duration") or 5),
        "match_materials_to_script": bool(job.get("match_materials_to_script", False)),
        "video_count": int(job.get("video_count") or 1),
        "voice_name": _clean_text(job.get("voice_name")),
        "voice_volume": float(job.get("voice_volume") or 1.0),
        "voice_rate": float(job.get("voice_rate") or 1.0),
        "bgm_type": _first_clean_text(job.get("bgm_type"), "random"),
        "bgm_file": _clean_text(job.get("bgm_file")),
        "bgm_volume": float(job.get("bgm_volume") or 0.2),
        "subtitle_enabled": bool(job.get("subtitle_enabled", True)),
        "subtitle_provider": _clean_text(job.get("subtitle_provider")),
        "subtitle_correction_enabled": bool(
            job.get("subtitle_correction_enabled", True)
        ),
        "subtitle_optimization_enabled": bool(
            job.get("subtitle_optimization_enabled", True)
        ),
        "custom_audio_file": _clean_text(job.get("custom_audio_file")),
        "custom_subtitle_file": _clean_text(job.get("custom_subtitle_file")),
        "asset_hub_renderer_manifest_path": _first_clean_text(
            job.get("asset_hub_renderer_manifest_path"),
            _as_dict(job.get("asset_hub")).get("renderer_manifest_path"),
        ),
        "asset_hub_bundle_uid": _first_clean_text(
            job.get("asset_hub_bundle_uid"),
            _as_dict(job.get("asset_hub")).get("bundle_uid"),
        ),
        "asset_hub_scene_mode": _first_clean_text(
            job.get("asset_hub_scene_mode"), "ordered"
        ),
    }


def discover_mpt_engine_capabilities() -> dict[str, Any]:
    """Return a static, network-free map of native MPT engine capabilities."""

    return {
        "engine": "MoneyPrinterTurbo",
        "network_free": True,
        "task_model": {
            "schema": "app.models.schema.VideoParams",
            "api_request": "app.models.schema.TaskVideoRequest",
            "api_endpoint": "POST /api/v1/videos",
            "service_entrypoint": "app.services.task.start(task_id, params)",
        },
        "sourcing": {
            "native_video_sources": list(MPT_SUPPORTED_VIDEO_SOURCES),
            "provider_functions": {
                "pexels": "app.services.material.search_videos_pexels",
                "pixabay": "app.services.material.search_videos_pixabay",
                "coverr": "app.services.material.search_videos_coverr",
                "local": "app.services.video.preprocess_video",
            },
            "key_config_names": {
                "pexels": "config.app.pexels_api_keys",
                "pixabay": "config.app.pixabay_api_keys",
                "coverr": "config.app.coverr_api_keys",
            },
            "downloads": (
                "app.services.material.save_video -> storage/cache_videos "
                "or config.app.material_directory"
            ),
        },
        "materials": {
            "model": "app.models.schema.MaterialInfo",
            "fields": ["provider", "url", "duration", "motion", "motion_intensity"],
            "local_source": 'VideoParams.video_source = "local"',
            "local_materials": "VideoParams.video_materials",
            "asset_hub_manifest": "VideoParams.asset_hub_renderer_manifest_path",
        },
        "audio_subtitles": {
            "custom_audio": "VideoParams.custom_audio_file skips TTS",
            "custom_subtitle": "VideoParams.custom_subtitle_file skips subtitle generation",
            "subtitle_provider": "edge or whisper",
        },
        "renderer": {
            "orchestrator": "app.services.task.generate_final_videos",
            "combine": "app.services.video.combine_videos",
            "final": "app.services.video.generate_video",
            "output": "storage/tasks/<task_id>/final-<index>.mp4",
        },
        "kurukin_boundary": {
            "preferred_role": "compile intent, policy and metadata into native MPT task params",
            "must_not_do": [
                "call stock providers",
                "download assets",
                "create pending jobs",
                "call /api/v1/videos",
                "render or shell out",
            ],
        },
    }


def build_mpt_video_task_from_kurukin_job(
    kurukin_job: dict[str, Any]
) -> dict[str, Any]:
    """Compile a generic Kurukin job into a native MPT video task spec."""

    job = _as_dict(kurukin_job)
    if job.get("render_mode") == RENDER_MODE_AROLL_BROLL or job.get("aroll_broll"):
        return build_mpt_aroll_broll_task_spec(job)

    raw_policy = _as_dict(job.get("asset_policy"))
    asset_policy = normalize_asset_source_policy(raw_policy)
    mpt_params = _base_mpt_params(job)
    materials = _local_materials_from_job(job)
    manifest_path = _clean_text(mpt_params.get("asset_hub_renderer_manifest_path"))

    if manifest_path:
        mpt_params["video_source"] = MPT_VIDEO_SOURCE_LOCAL
        mpt_params["video_materials"] = []
        mpt_params["video_terms"] = []
    elif materials or asset_policy.get("mode") == ASSET_SOURCE_MODE_LOCAL_ONLY:
        mpt_params["video_source"] = MPT_VIDEO_SOURCE_LOCAL
        mpt_params["video_materials"] = materials
    else:
        mpt_params["video_source"] = _stock_source_from_job(job, asset_policy)
        mpt_params["video_materials"] = []

    return _build_spec(
        mpt_params=mpt_params,
        asset_policy=asset_policy,
        kurukin_job=job,
        render_mode=_first_clean_text(job.get("render_mode"), "normal"),
        gaps=[],
        warnings=[],
    )


def build_mpt_aroll_broll_task_spec(kurukin_job: dict[str, Any]) -> dict[str, Any]:
    """Compile A-roll/B-roll intent to a native-MPT-first task spec."""

    job = _as_dict(kurukin_job)
    ar_config = _as_dict(job.get("aroll_broll"))
    a_roll = _as_dict(ar_config.get("a_roll")) or _as_dict(job.get("a_roll"))
    b_roll = _as_dict(ar_config.get("b_roll")) or _as_dict(job.get("b_roll"))
    subtitles = _as_dict(ar_config.get("subtitles")) or _as_dict(job.get("subtitles"))
    raw_policy = _as_dict(ar_config.get("asset_policy") or job.get("asset_policy"))
    asset_policy = normalize_asset_source_policy(raw_policy)

    a_roll_path = _first_clean_text(
        a_roll.get("path"),
        a_roll.get("local_path"),
        a_roll.get("file_path"),
        job.get("a_roll_path"),
        job.get("primary_media_path"),
    )
    a_roll_audio = _first_clean_text(
        a_roll.get("audio_path"),
        a_roll.get("custom_audio_file"),
        job.get("custom_audio_file"),
    )
    b_roll_assets = [
        _material_info(item, provider=_clean_text(item.get("provider")) or "local")
        if isinstance(item, dict)
        else _material_info(item, provider="local")
        for item in _as_list(b_roll.get("assets") or job.get("b_roll_assets"))
    ]
    b_roll_assets = _dedupe_materials(
        [material for material in b_roll_assets if material.get("url")]
    )

    mpt_params = _base_mpt_params(job)
    mpt_params["video_source"] = MPT_VIDEO_SOURCE_LOCAL
    mpt_params["video_materials"] = _dedupe_materials(
        (
            [
                _material_info(
                    {"provider": "local", "url": a_roll_path, "role": "a_roll"}
                )
            ]
            if a_roll_path
            else []
        )
        + b_roll_assets
    )
    mpt_params["video_concat_mode"] = "sequential"
    mpt_params["match_materials_to_script"] = True
    mpt_params["custom_audio_file"] = a_roll_audio
    mpt_params["subtitle_enabled"] = (
        _clean_text(subtitles.get("source")) not in {"", "none"}
        if subtitles
        else bool(mpt_params.get("subtitle_enabled", True))
    )
    mpt_params["custom_subtitle_file"] = _first_clean_text(
        subtitles.get("custom_srt_path"),
        subtitles.get("path"),
        mpt_params.get("custom_subtitle_file"),
    )
    mpt_params["subtitle_provider"] = _first_clean_text(
        subtitles.get("provider"),
        mpt_params.get("subtitle_provider"),
    )

    gaps: list[str] = []
    if a_roll_path and not a_roll_audio:
        gaps.append(
            "MPT accepts custom_audio_file but does not natively extract audio from an A-roll video path in VideoParams."
        )
    if a_roll_path and b_roll_assets:
        gaps.append(
            "Native MPT local materials concatenate support visuals; alternating A-roll/B-roll editorial timing still needs a minimal engine extension."
        )
    if (
        asset_policy.get("mode") == ASSET_SOURCE_MODE_OPEN_SOURCES
        and not b_roll_assets
    ):
        gaps.append(
            "B-roll open-source intent can map to MPT video_source providers, but this bridge intentionally does not search or download."
        )

    spec = _build_spec(
        mpt_params=mpt_params,
        asset_policy=asset_policy,
        kurukin_job=job,
        render_mode=RENDER_MODE_AROLL_BROLL,
        gaps=gaps,
        warnings=[],
    )
    spec["kurukin_metadata"]["aroll_broll"] = {
        "primary_media": {
            "role": "a_roll",
            "path": a_roll_path,
            "audio_policy": _first_clean_text(a_roll.get("audio_policy"), "original"),
        },
        "support_visuals": {
            "role": "b_roll",
            "intent": _first_clean_text(b_roll.get("intent"), "support_visuals"),
            "assets": deepcopy(b_roll_assets),
            "asset_count": len(b_roll_assets),
            "audio_policy": _first_clean_text(b_roll.get("audio_policy"), "muted"),
            "query": _clean_text(b_roll.get("query")),
        },
        "original_audio_policy": "a_roll_original",
        "subtitles_policy": {
            "source": _first_clean_text(subtitles.get("source"), "none"),
            "provider": _first_clean_text(subtitles.get("provider"), "none"),
            "custom_srt_path": _clean_text(subtitles.get("custom_srt_path")),
        },
    }
    return spec


def _build_spec(
    *,
    mpt_params: dict[str, Any],
    asset_policy: dict[str, Any],
    kurukin_job: dict[str, Any],
    render_mode: str,
    gaps: list[str],
    warnings: list[str],
) -> dict[str, Any]:
    return {
        "kind": "mpt_video_task_spec",
        "engine": "moneyprinterturbo",
        "execution": "spec_only",
        "safe_to_build_without_side_effects": True,
        "mpt_entrypoint": {
            "task_model": "app.models.schema.VideoParams",
            "api": "POST /api/v1/videos",
            "service": "app.services.task.start",
        },
        "mpt_params": deepcopy(mpt_params),
        "kurukin_metadata": {
            "job_id": _first_clean_text(
                kurukin_job.get("job_id"), kurukin_job.get("id")
            ),
            "render_mode": render_mode,
            "asset_policy": deepcopy(asset_policy),
            "asset_policy_summary": summarize_asset_source_policy(asset_policy),
            "brand_policy": deepcopy(kurukin_job.get("brand_policy", {})),
            "metadata": deepcopy(kurukin_job.get("metadata", {})),
        },
        "gaps": list(gaps),
        "warnings": list(warnings),
    }


def validate_mpt_task_spec(spec: dict[str, Any]) -> list[str]:
    """Return validation errors for a bridge-produced MPT task spec."""

    if not isinstance(spec, dict):
        return ["spec must be a JSON object"]

    errors: list[str] = []
    if spec.get("kind") != "mpt_video_task_spec":
        errors.append("spec.kind must be mpt_video_task_spec")
    if spec.get("execution") != "spec_only":
        errors.append("spec.execution must be spec_only")
    if spec.get("safe_to_build_without_side_effects") is not True:
        errors.append("spec must be marked side-effect free")

    params = spec.get("mpt_params")
    if not isinstance(params, dict):
        errors.append("mpt_params must be a JSON object")
        params = {}

    if not _first_clean_text(params.get("video_subject"), params.get("video_script")):
        errors.append("mpt_params.video_subject or video_script is required")

    video_source = _clean_text(params.get("video_source"))
    if video_source not in MPT_SUPPORTED_VIDEO_SOURCES:
        errors.append(
            "mpt_params.video_source must be pexels, pixabay, coverr, or local"
        )

    materials = params.get("video_materials") or []
    manifest_path = _clean_text(params.get("asset_hub_renderer_manifest_path"))
    if video_source == MPT_VIDEO_SOURCE_LOCAL and not manifest_path:
        if not isinstance(materials, list) or not materials:
            errors.append(
                "mpt_params.video_materials is required when video_source is local"
            )

    metadata = spec.get("kurukin_metadata")
    if not isinstance(metadata, dict):
        errors.append("kurukin_metadata must be a JSON object")
        metadata = {}

    policy = metadata.get("asset_policy")
    if isinstance(policy, dict):
        errors.extend(validate_asset_source_policy(policy))
    else:
        errors.append("kurukin_metadata.asset_policy must be a JSON object")

    if metadata.get("render_mode") == RENDER_MODE_AROLL_BROLL:
        aroll_broll = metadata.get("aroll_broll")
        if not isinstance(aroll_broll, dict):
            errors.append("kurukin_metadata.aroll_broll is required")
        else:
            primary = _as_dict(aroll_broll.get("primary_media"))
            support = _as_dict(aroll_broll.get("support_visuals"))
            if not _clean_text(primary.get("path")):
                errors.append("aroll_broll.primary_media.path is required")
            if primary.get("audio_policy") != "original":
                errors.append("aroll_broll.primary_media.audio_policy must be original")
            if not support.get("assets") and not _clean_text(support.get("intent")):
                errors.append("aroll_broll.support_visuals assets or intent is required")
            if support.get("audio_policy") != "muted":
                errors.append("aroll_broll.support_visuals.audio_policy must be muted")

    forbidden_execution_markers = [
        "pending_path",
        "created_task",
        "task_created",
        "provider_response",
        "downloaded_assets",
        "render_result",
    ]
    for marker in forbidden_execution_markers:
        if marker in spec:
            errors.append(f"spec must not include execution marker: {marker}")

    return errors


def summarize_mpt_task_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """Return a compact human summary without mutating or executing the spec."""

    params = _as_dict(spec.get("mpt_params"))
    metadata = _as_dict(spec.get("kurukin_metadata"))
    materials = params.get("video_materials") or []
    errors = validate_mpt_task_spec(spec)
    return {
        "engine": spec.get("engine", "moneyprinterturbo"),
        "execution": spec.get("execution", ""),
        "render_mode": metadata.get("render_mode", "normal"),
        "video_subject": params.get("video_subject", ""),
        "video_source": params.get("video_source", ""),
        "material_count": len(materials) if isinstance(materials, list) else 0,
        "asset_policy": _as_dict(metadata.get("asset_policy_summary")).get(
            "short_label", ""
        ),
        "custom_audio": bool(params.get("custom_audio_file")),
        "subtitles": (
            "custom"
            if params.get("custom_subtitle_file")
            else ("enabled" if params.get("subtitle_enabled") else "disabled")
        ),
        "gap_count": len(spec.get("gaps") or []),
        "valid": not errors,
        "errors": errors,
        "next_step": "Submit to native MPT only after explicit render authorization.",
    }
