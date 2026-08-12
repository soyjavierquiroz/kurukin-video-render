"""Kurukin Asset Hub selection and explicit bundle wiring helpers."""

from __future__ import annotations

from copy import deepcopy
import math
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from app.custom.asset_hub_manifest import (
    get_asset_hub_job_assets_dir,
    validate_asset_hub_renderer_manifest,
)
from app.custom.kurukin_asset_hub import (
    dedupe_key,
    resolve_ready_asset_paths,
    validate_asset_uid,
)


STATUS_NEEDS_INPUT = "NEEDS_INPUT"
STATUS_READY = "READY"
REASON_EXPLICIT_ASSET_SELECTION_REQUIRED = "explicit_asset_selection_required"
REASON_ASSET_HUB_MATERIALIZATION_NOT_READY = "asset_hub_materialization_not_ready"


class KurukinAssetHubWiringError(RuntimeError):
    """Expected wiring error for explicit Asset Hub selection."""


class KurukinAssetHubSelectionRequired(KurukinAssetHubWiringError):
    """Raised when a required scene is missing explicit asset_uid selection."""


class KurukinAssetHubMaterializationNotReady(KurukinAssetHubWiringError):
    """Raised when Asset Hub materialization has not reached a ready state."""


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


def _scene_index(scene: Mapping[str, Any], fallback: int) -> int:
    value = scene.get("scene_index", scene.get("index", fallback))
    if isinstance(value, bool):
        return fallback
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _scene_id(scene: Mapping[str, Any], index: int) -> str:
    return _clean_text(scene.get("scene_id")) or f"scene-{index:03d}"


def _scene_text(scene: Mapping[str, Any]) -> str:
    return _clean_text(
        scene.get("script_scene")
        or scene.get("text")
        or scene.get("caption")
        or scene.get("description")
    )


def _visual_keyword_query(scene: Mapping[str, Any]) -> str:
    for item in _as_list(scene.get("visual_keywords")):
        if isinstance(item, Mapping):
            text = _clean_text(
                item.get("query")
                or item.get("keyword")
                or item.get("text")
                or item.get("label")
            )
        else:
            text = _clean_text(item)
        if text:
            return text
    return ""


def _iter_intent_scenes(intent: Mapping[str, Any]) -> list[dict[str, Any]]:
    scenes = _as_list(intent.get("scenes"))
    return [dict(scene) for scene in scenes if isinstance(scene, Mapping)]


def _query_for_scene(scene: Mapping[str, Any]) -> str:
    return _visual_keyword_query(scene) or _scene_text(scene)


def build_asset_hub_search_requests(
    intent: Mapping[str, Any],
    source_policy: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Build one Asset Hub search request per Kurukin scene without network."""

    requests: list[dict[str, Any]] = []
    for fallback, scene in enumerate(_iter_intent_scenes(intent), start=1):
        scene_index = _scene_index(scene, fallback)
        query = _query_for_scene(scene)
        if not query:
            continue
        requests.append(
            {
                "scene_id": _scene_id(scene, scene_index),
                "scene_index": scene_index,
                "script_scene": _scene_text(scene),
                "query": query,
                "source_policy": deepcopy(dict(source_policy)),
            }
        )
    return requests


def _candidate_value(asset: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = _clean_text(asset.get(key))
        if value:
            return value
    return ""


def _candidate_scope(asset: Mapping[str, Any]) -> str:
    source = asset.get("source")
    if isinstance(source, Mapping):
        return _clean_text(source.get("scope"))
    return _candidate_value(asset, "scope", "source_scope")


def _candidate_brand(asset: Mapping[str, Any]) -> str:
    source = asset.get("source")
    if isinstance(source, Mapping):
        return _clean_text(source.get("brand"))
    return _candidate_value(asset, "brand", "brand_slug")


def _candidate_title(asset: Mapping[str, Any]) -> str:
    source = asset.get("source")
    if isinstance(source, Mapping):
        return _clean_text(source.get("title"))
    return _candidate_value(asset, "title", "title_slug")


def _candidate_tags(asset: Mapping[str, Any]) -> list[str]:
    tags = asset.get("tags")
    if not isinstance(tags, list):
        return []
    return [_clean_text(tag) for tag in tags if _clean_text(tag)]


def _normalize_candidate(asset: Mapping[str, Any]) -> dict[str, Any]:
    asset_uid = validate_asset_uid(asset.get("asset_uid"))
    return {
        "asset_uid": asset_uid,
        "dedupe_key": dedupe_key(asset_uid),
        "filename": _candidate_value(asset, "filename", "name"),
        "media_type": _candidate_value(asset, "media_type", "type", "asset_type"),
        "scope": _candidate_scope(asset),
        "brand": _candidate_brand(asset),
        "title": _candidate_title(asset),
        "orientation": _candidate_value(asset, "orientation", "aspect"),
        "tags": _candidate_tags(asset),
    }


def search_asset_hub_candidates(
    provider: Any,
    search_requests: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """Search Asset Hub candidates per scene without selecting assets."""

    scenes = []
    for request in search_requests:
        assets = provider.search(
            query=_clean_text(request.get("query")),
            source_policy=_as_dict(request.get("source_policy")),
        )
        candidates = [
            _normalize_candidate(asset)
            for asset in assets
            if isinstance(asset, Mapping)
        ]
        scenes.append(
            {
                "scene_id": _clean_text(request.get("scene_id")),
                "scene_index": request.get("scene_index"),
                "script_scene": _clean_text(request.get("script_scene")),
                "query": _clean_text(request.get("query")),
                "source_policy": _as_dict(request.get("source_policy")),
                "candidates": candidates,
            }
        )

    candidates_available = any(scene["candidates"] for scene in scenes)
    return {
        "ok": False,
        "status": STATUS_NEEDS_INPUT,
        "reason": REASON_EXPLICIT_ASSET_SELECTION_REQUIRED,
        "search_complete": True,
        "candidates_available": candidates_available,
        "asset_hub_selection": {
            "source_policy": (
                _as_dict(search_requests[0].get("source_policy"))
                if search_requests
                else {}
            ),
            "scenes": scenes,
            "selected_asset_uids": {},
        },
    }


def build_missing_selection_result(
    search_result: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the project-style NEEDS_INPUT shape for candidate search results."""

    return {
        "ok": False,
        "status": STATUS_NEEDS_INPUT,
        "reason": REASON_EXPLICIT_ASSET_SELECTION_REQUIRED,
        "search_complete": bool(search_result.get("search_complete")),
        "candidates_available": bool(search_result.get("candidates_available")),
        "asset_hub_selection": _as_dict(search_result.get("asset_hub_selection")),
    }


def _selection_for_scene(
    selected_asset_uids_by_scene: Mapping[str, Any],
    scene_id: str,
    scene_index: int,
) -> list[str]:
    raw = None
    for key in (scene_id, str(scene_index), scene_index):
        if key in selected_asset_uids_by_scene:
            raw = selected_asset_uids_by_scene[key]
            break

    selected = _as_list(raw)
    if not selected:
        raise KurukinAssetHubSelectionRequired(
            f"scene {scene_id} requires explicit selected_asset_uids"
        )
    return [
        validate_asset_uid(
            asset_uid,
            field_name=f"{scene_id}.selected_asset_uids",
        )
        for asset_uid in selected
    ]


def build_asset_hub_bundle_scenes(
    intent: Mapping[str, Any],
    selected_asset_uids_by_scene: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Build explicit Asset Hub bundle scenes from selected asset_uids."""

    scenes = _iter_intent_scenes(intent)
    if not scenes:
        raise KurukinAssetHubSelectionRequired("intent.scenes is required")

    bundle_scenes = []
    for fallback, scene in enumerate(scenes, start=1):
        scene_index = _scene_index(scene, fallback)
        scene_id = _scene_id(scene, scene_index)
        selected = _selection_for_scene(
            selected_asset_uids_by_scene,
            scene_id,
            scene_index,
        )
        bundle_scenes.append(
            {
                "scene_id": scene_id,
                "scene_index": scene_index,
                "script_scene": _scene_text(scene),
                "selected_asset_uids": selected,
            }
        )
    return bundle_scenes


def resolve_renderer_manifest_path(
    bundle_uid: str,
    root: str | Path | None = None,
) -> str:
    """Return the physical renderer manifest path for a materialized bundle."""

    clean_uid = _clean_text(bundle_uid)
    if not clean_uid:
        raise ValueError("bundle_uid is required")
    uid_path = PurePosixPath(clean_uid)
    if uid_path.is_absolute() or "/" in clean_uid or "\\" in clean_uid or ".." in uid_path.parts:
        raise ValueError("bundle_uid cannot contain path separators or parent paths")

    base = Path(root).resolve() if root is not None else get_asset_hub_job_assets_dir().resolve()
    manifest_path = (base / clean_uid / "manifests" / "renderer-manifest.json").resolve(
        strict=False
    )
    try:
        manifest_path.relative_to(base)
    except ValueError as exc:
        raise ValueError("renderer manifest path must stay under asset hub root") from exc
    return manifest_path.as_posix()


def _extract_bundle_uid(response: Mapping[str, Any]) -> str:
    return _clean_text(
        response.get("bundle_uid")
        or response.get("uid")
        or _as_dict(response.get("bundle")).get("bundle_uid")
    )


def _is_ready_response(response: Mapping[str, Any]) -> bool:
    if "materialization_status" in response:
        return response.get("materialization_status") == "ready"
    return response.get("status") == "ready"


def _is_manifest_asset_ready(asset: Mapping[str, Any]) -> bool:
    if "materialization_status" in asset:
        return asset.get("materialization_status") == "ready"
    return asset.get("status") == "ready"


def _ordered_manifest_assets(assets: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    ranked = [asset for asset in assets if "rank" in asset]
    if not ranked:
        return list(assets)
    if len(ranked) != len(assets):
        raise KurukinAssetHubWiringError(
            "renderer manifest asset ranks must be all-or-none per scene"
        )

    ordered = []
    seen_ranks: set[float] = set()
    for asset in assets:
        rank = asset.get("rank")
        if (
            isinstance(rank, bool)
            or not isinstance(rank, (int, float))
            or not math.isfinite(float(rank))
        ):
            raise KurukinAssetHubWiringError(
                "renderer manifest asset rank must be numeric"
            )
        numeric_rank = float(rank)
        if numeric_rank in seen_ranks:
            raise KurukinAssetHubWiringError(
                "renderer manifest asset ranks must be unique per scene"
            )
        seen_ranks.add(numeric_rank)
        ordered.append((numeric_rank, asset))
    return [asset for _, asset in sorted(ordered, key=lambda item: item[0])]


def _manifest_scenes_by_id(manifest: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    scenes = manifest.get("scenes")
    if not isinstance(scenes, list):
        return {}

    indexed: dict[str, Mapping[str, Any]] = {}
    for scene in scenes:
        if not isinstance(scene, Mapping):
            continue
        scene_id = _clean_text(scene.get("scene_id"))
        if not scene_id:
            raise KurukinAssetHubWiringError("renderer manifest scene_id is required")
        if scene_id in indexed:
            raise KurukinAssetHubWiringError(
                f"renderer manifest has duplicate scene_id: {scene_id}"
            )
        indexed[scene_id] = scene
    return indexed


def validate_explicit_manifest_selection(
    manifest: Mapping[str, Any],
    expected_bundle_scenes: list[Mapping[str, Any]],
) -> None:
    """Validate that a renderer manifest exactly matches explicit selection."""

    manifest_scenes = _manifest_scenes_by_id(manifest)
    expected_scene_ids: list[str] = []
    seen_expected_scene_ids: set[str] = set()
    for expected_scene in expected_bundle_scenes:
        scene_id = _clean_text(expected_scene.get("scene_id"))
        if scene_id in seen_expected_scene_ids:
            raise KurukinAssetHubWiringError(
                f"explicit bundle has duplicate scene_id: {scene_id}"
            )
        seen_expected_scene_ids.add(scene_id)
        expected_scene_ids.append(scene_id)
        if not scene_id or scene_id not in manifest_scenes:
            raise KurukinAssetHubWiringError(
                f"renderer manifest is missing selected scene: {scene_id or '<unknown>'}"
            )

        manifest_scene = manifest_scenes[scene_id]
        raw_assets = manifest_scene.get("assets")
        if not isinstance(raw_assets, list):
            raise KurukinAssetHubWiringError(
                f"renderer manifest scene {scene_id} assets must be a list"
            )
        manifest_assets = [
            asset for asset in raw_assets if isinstance(asset, Mapping)
        ]
        ready_asset_uids = [
            validate_asset_uid(asset.get("asset_uid"))
            for asset in _ordered_manifest_assets(manifest_assets)
            if _is_manifest_asset_ready(asset)
        ]
        expected_asset_uids = [
            validate_asset_uid(asset_uid)
            for asset_uid in _as_list(expected_scene.get("selected_asset_uids"))
        ]
        if ready_asset_uids != expected_asset_uids:
            raise KurukinAssetHubWiringError(
                f"renderer manifest scene {scene_id} does not match explicit selected_asset_uids"
            )
    if set(manifest_scenes) != set(expected_scene_ids):
        raise KurukinAssetHubWiringError(
            "renderer manifest scenes do not exactly match explicit bundle scenes"
        )


def wire_explicit_asset_hub_bundle(
    intent: Mapping[str, Any],
    provider: Any,
    selected_asset_uids_by_scene: Mapping[str, Any],
    *,
    created_by: str = "money-printer-turbo",
    root: str | Path | None = None,
) -> dict[str, Any]:
    """Create and materialize an explicitly selected Asset Hub bundle."""

    bundle_scenes = build_asset_hub_bundle_scenes(
        intent,
        selected_asset_uids_by_scene,
    )
    job_id = _clean_text(intent.get("task_id") or intent.get("job_id"))
    if not job_id:
        raise KurukinAssetHubWiringError("job_id is required")

    create_response = provider.create_bundle(
        job_id=job_id,
        scenes=bundle_scenes,
        created_by=created_by,
    )
    bundle_uid = _extract_bundle_uid(create_response)
    if not bundle_uid:
        raise KurukinAssetHubWiringError("Asset Hub create_bundle did not return bundle_uid")

    materialize_response = provider.materialize_bundle(bundle_uid, force=False)
    if not isinstance(materialize_response, Mapping) or not _is_ready_response(
        materialize_response
    ):
        raise KurukinAssetHubMaterializationNotReady(
            REASON_ASSET_HUB_MATERIALIZATION_NOT_READY
        )

    manifest = provider.get_renderer_manifest(bundle_uid)
    if not isinstance(manifest, dict):
        raise KurukinAssetHubWiringError("Asset Hub renderer manifest must be a JSON object")
    validate_asset_hub_renderer_manifest(manifest)
    manifest_bundle_uid = _clean_text(manifest.get("bundle_uid"))
    if manifest_bundle_uid and manifest_bundle_uid != bundle_uid:
        raise KurukinAssetHubWiringError(
            "renderer manifest bundle_uid does not match created bundle_uid"
        )
    validate_explicit_manifest_selection(manifest, bundle_scenes)
    ready_assets = resolve_ready_asset_paths(manifest, materialized_root=root)
    if not ready_assets:
        raise KurukinAssetHubMaterializationNotReady(
            REASON_ASSET_HUB_MATERIALIZATION_NOT_READY
        )

    return {
        "asset_hub": {
            "renderer_manifest_path": resolve_renderer_manifest_path(
                bundle_uid,
                root=root,
            ),
            "bundle_uid": bundle_uid,
            "scene_mode": "ordered",
            "strict": True,
        }
    }
