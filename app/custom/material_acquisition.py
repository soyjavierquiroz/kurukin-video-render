"""Materialize a deterministic selection into MPT-ready ``MaterialInfo`` values."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from app.custom.asset_hub_manifest import extract_asset_hub_local_assets
from app.custom.kurukin_asset_hub import KurukinAssetProvider
from app.custom.kurukin_asset_hub_wiring import wire_explicit_asset_hub_bundle
from app.models.schema import MaterialInfo
from app.services import material
from app.utils import utils


_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_FORBIDDEN = ("drive_file_id", "remote_path", "rclone_remote", "target_path", "credential", "api_key", "token", "secret", "password", "authorization")


class MaterialAcquisitionError(RuntimeError): pass
class MaterialAcquisitionUnavailable(MaterialAcquisitionError): pass


@dataclass(frozen=True)
class MaterialAcquisitionResult:
    materials: tuple[MaterialInfo, ...]
    manifest_path: str
    bundle_uid: str | None
    diagnostics: tuple[str, ...] = ()


def _task_paths(task_id: str) -> tuple[Path, Path]:
    if not _TASK_ID.fullmatch(str(task_id or "")):
        raise ValueError("task_id must contain only letters, digits, '_' or '-'")
    task_dir = Path(utils.storage_dir("tasks")) / task_id
    return task_dir, task_dir / "materials"


def _safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _safe(v) for k, v in value.items() if not any(word in str(k).lower() for word in _FORBIDDEN)}
    if isinstance(value, (list, tuple)): return [_safe(item) for item in value]
    return value


def _status_code(exc: Exception) -> int | None:
    for value in (getattr(exc, "status_code", None), getattr(getattr(exc, "response", None), "status_code", None)):
        try: return int(value)
        except (TypeError, ValueError): pass
    return None


def _write_manifest(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".material-acquisition-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def _approved_plan_asset_hub_uids(plan: Mapping[str, Any]) -> list[str]:
    """Return the frozen Asset Hub selection required by an approved plan."""
    from app.custom import human_review

    if plan.get("review_status") != human_review.STATUS_APPROVED:
        raise MaterialAcquisitionError("production plan is not approved")
    return [
        uid
        for scene in _approved_plan_asset_hub_scenes(plan)
        for uid in scene["selected_asset_uids"]
    ]


def _approved_plan_asset_hub_scenes(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return frozen segment-scoped Asset Hub primary and backup UID maps."""
    scenes: list[dict[str, Any]] = []
    for segment in plan.get("segments") or []:
        if not isinstance(segment, Mapping):
            continue
        scene_id = str(segment.get("segment_id") or "").strip()
        if not scene_id:
            continue
        uids = []
        for asset in [segment.get("selected_asset"), *(segment.get("backup_assets") or [])]:
            if not isinstance(asset, Mapping):
                continue
            provider = str(asset.get("provider") or asset.get("source") or "").strip()
            if provider != "asset_hub":
                continue
            uid = str(asset.get("asset_uid") or asset.get("canonical_id") or "").strip()
            if uid:
                uids.append(uid)
        if uids:
            scenes.append(
                {
                    "scene_id": scene_id,
                    "script_scene": str(segment.get("script_text") or "").strip(),
                    "selected_asset_uids": list(dict.fromkeys(uids)),
                }
            )
    return scenes


def _approved_plan_scene_ids_for_uids(plan: Mapping[str, Any], asset_uids: list[str]) -> list[str]:
    """Resolve frozen segment IDs for an already-exact approved UID sequence."""
    scenes_by_uid: dict[str, str] = {}
    for segment in plan.get("segments") or []:
        if not isinstance(segment, Mapping):
            continue
        scene_id = str(segment.get("segment_id") or "").strip()
        for asset in [segment.get("selected_asset"), *(segment.get("backup_assets") or [])]:
            if not isinstance(asset, Mapping):
                continue
            uid = str(asset.get("asset_uid") or asset.get("canonical_id") or "")
            if uid:
                scenes_by_uid[uid] = scene_id
    return [scenes_by_uid.get(uid, "") for uid in asset_uids]


def _approved_plan_scene_text_by_id(plan: Mapping[str, Any]) -> dict[str, str]:
    """Return the approved narration used by Asset Hub's supported scene schema."""
    scene_text: dict[str, str] = {}
    for segment in plan.get("segments") or []:
        if not isinstance(segment, Mapping):
            continue
        scene_id = str(segment.get("segment_id") or "").strip()
        text = str(segment.get("script_text") or "").strip()
        if scene_id:
            scene_text[scene_id] = text
    return scene_text


def _asset_hub_materials(
    decisions: list[Any],
    task_id: str,
    provider: Any,
    *,
    approved_plan: Mapping[str, Any] | None = None,
    scene_ids: list[str] | None = None,
) -> tuple[list[MaterialInfo], str]:
    selected_uids = [str(decision.candidate.canonical_id) for decision in decisions]
    if approved_plan is not None:
        plan_selected_uids = _approved_plan_asset_hub_uids(approved_plan)
        if any(uid not in plan_selected_uids for uid in selected_uids):
            raise MaterialAcquisitionError(
                "approved plan selected_asset_uids do not match bundle "
                "selected_asset_uids; materialization blocked"
            )

    if scene_ids is None or len(scene_ids) != len(decisions):
        if approved_plan is not None:
            raise MaterialAcquisitionError(
                "approved Asset Hub selection is missing frozen segment scene IDs"
            )
        # Non-production callers retain their existing request shape.  The
        # approved production path above never takes this fallback.
        scene_ids = [f"scene-{index:03d}" for index in range(1, len(decisions) + 1)]
    by_scene: dict[str, list[Any]] = {}
    for scene_id, decision in zip(scene_ids, decisions):
        approved_scene_id = str(scene_id or "").strip()
        if not approved_scene_id:
            raise MaterialAcquisitionError("approved Asset Hub scene_id is required")
        by_scene.setdefault(approved_scene_id, []).append(decision.candidate)
    approved_scene_text = (
        _approved_plan_scene_text_by_id(approved_plan)
        if approved_plan is not None
        else {}
    )
    if approved_plan is not None:
        scenes = [
            {
                "scene_id": scene["scene_id"],
                "scene_index": index,
                "script_scene": scene["script_scene"],
                "selected_asset_uids": scene["selected_asset_uids"],
            }
            for index, scene in enumerate(_approved_plan_asset_hub_scenes(approved_plan), 1)
        ]
    else:
        scenes = []
    for index, (scene_id, candidates) in enumerate(by_scene.items(), 1):
        if approved_plan is not None:
            continue
        # ``script_scene`` is required by the deployed Asset Hub endpoint.
        # It is API metadata only; the approved segment ID remains the local
        # deterministic mapping key used for the renderer manifest.
        script_scene = approved_scene_text.get(scene_id, "")
        if not script_scene:
            script_scene = str(getattr(candidates[0], "search_term", "") or "").strip()
        if not script_scene:
            raise MaterialAcquisitionError(
                f"Asset Hub scene {scene_id} is missing required script_scene"
            )
        scenes.append({"scene_id": scene_id, "scene_index": index,
                       "script_scene": script_scene,
                       "selected_asset_uids": [candidate.canonical_id for candidate in candidates]})
    intent = {"task_id": task_id, "scenes": scenes}
    selection = {scene["scene_id"]: scene["selected_asset_uids"] for scene in scenes}
    try:
        # Asset Hub contributes immutable source media only.  Its derived
        # renderer manifest belongs beside this task's other artifacts.
        wired = wire_explicit_asset_hub_bundle(
            intent,
            provider,
            selection,
            task_root=_task_paths(task_id)[0].parent,
        )
        bundle_uid = wired["asset_hub"]["bundle_uid"]
        manifest = provider.get_renderer_manifest(bundle_uid)
    except Exception as exc:
        if _status_code(exc) == 503:
            raise MaterialAcquisitionUnavailable("Asset Hub materialization is temporarily unavailable (503)") from exc
        raise
    assets_by_uid = {
        str(asset.get("asset_uid")): asset
        for asset in extract_asset_hub_local_assets(manifest, strict=True)
        if str(asset.get("asset_uid")) in selected_uids
    }
    missing = [asset_uid for asset_uid in selected_uids if asset_uid not in assets_by_uid]
    if missing:
        raise MaterialAcquisitionError("Asset Hub manifest is missing selected assets")
    materials = [
        MaterialInfo(
            provider="asset_hub",
            url=str(assets_by_uid[asset_uid]["local_path"]),
            duration=int(float(assets_by_uid[asset_uid].get("duration_seconds") or 0)),
            motion="",
            motion_intensity=0.0,
        )
        for asset_uid in selected_uids
    ]
    for info, decision in zip(materials, decisions):
        candidate = decision.candidate
        info.source_info = _safe({"provider": "asset_hub", "asset_id": candidate.canonical_id,
                                  "dedupe_key": candidate.dedupe_key, "search_term": candidate.search_term,
                                  "bundle_uid": bundle_uid})
    return materials, bundle_uid


def acquire_selected_materials(*, selection_result: Any, task_id: str,
                               asset_hub_provider: Any = None,
                               approved_plan: Mapping[str, Any] | None = None) -> MaterialAcquisitionResult:
    """Download stock to a task directory and materialize Asset Hub once.

    No task lifecycle or render parameters are changed here.
    """
    task_dir, materials_dir = _task_paths(task_id)
    decisions = list(getattr(selection_result, "decisions", ()) or ())
    hub = [decision for decision in decisions if getattr(decision.candidate, "provider", "") == "asset_hub"]
    if hub and asset_hub_provider is None:
        asset_hub_provider = KurukinAssetProvider()
    hub_infos, bundle_uid = ([], None)
    if hub:
        decision_segment_ids = list(getattr(selection_result, "decision_segment_ids", ()) or ())
        decision_scene_ids = (
            [
                decision_segment_ids[index]
                for index, decision in enumerate(decisions)
                if getattr(decision.candidate, "provider", "") == "asset_hub"
            ]
            if len(decision_segment_ids) == len(decisions)
            else []
        )
        if approved_plan is not None and len(decision_scene_ids) != len(hub):
            decision_scene_ids = _approved_plan_scene_ids_for_uids(
                approved_plan,
                [str(decision.candidate.canonical_id) for decision in hub],
            )
        hub_infos, bundle_uid = _asset_hub_materials(
            hub,
            task_id,
            asset_hub_provider,
            approved_plan=approved_plan,
            scene_ids=decision_scene_ids,
        )
    hub_by_key = {decision.candidate.dedupe_key: info for decision, info in zip(hub, hub_infos)}
    result, manifest_items = [], []
    materials_dir.mkdir(parents=True, exist_ok=True)
    for decision in decisions:
        candidate = decision.candidate
        provider = str(getattr(candidate, "provider", "") or "")
        if provider == "asset_hub":
            info = hub_by_key[candidate.dedupe_key]
        elif provider == "local":
            info = MaterialInfo(provider="local", url=str(getattr(candidate, "url", "") or ""),
                                duration=int(float(getattr(candidate, "duration", 0) or 0)),
                                source_info=_safe({"dedupe_key": candidate.dedupe_key, "search_term": candidate.search_term}))
        else:
            local_path = material.download_material_candidate(
                candidate, str(materials_dir), task_id=task_id
            )
            if not local_path:
                raise MaterialAcquisitionError(f"download failed for {provider}:{getattr(candidate, 'canonical_id', '')}")
            info = MaterialInfo(provider=provider, url=local_path, duration=int(float(getattr(candidate, "duration", 0) or 0)),
                                source_info=_safe({"provider": provider, "asset_id": candidate.canonical_id,
                                                   "dedupe_key": candidate.dedupe_key, "search_term": candidate.search_term}))
        result.append(info)
        item = {"provider": provider, "canonical_id": str(getattr(candidate, "canonical_id", "")),
                "dedupe_key": str(getattr(candidate, "dedupe_key", "")), "search_term": str(getattr(candidate, "search_term", ""))}
        if provider == "asset_hub": item["bundle_uid"] = bundle_uid
        else:
            try: item["local_path"] = str(Path(info.url).resolve().relative_to(task_dir.resolve()))
            except ValueError: item["local_path"] = ""
        manifest_items.append(item)
    manifest_path = task_dir / "material-acquisition.json"
    _write_manifest(manifest_path, {"task_id": task_id, "created_at": datetime.now(timezone.utc).isoformat(),
                                    "selected": manifest_items, "diagnostics": []})
    return MaterialAcquisitionResult(tuple(result), str(manifest_path), bundle_uid)
