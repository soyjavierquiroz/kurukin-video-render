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

from app.custom.asset_hub_manifest import convert_asset_hub_manifest_to_materials
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


def _asset_hub_materials(decisions: list[Any], task_id: str, provider: Any) -> tuple[list[MaterialInfo], str]:
    by_term: dict[str, list[Any]] = {}
    for decision in decisions:
        candidate = decision.candidate
        by_term.setdefault(str(getattr(candidate, "search_term", "") or "asset"), []).append(candidate)
    scenes = []
    for index, (term, candidates) in enumerate(by_term.items(), 1):
        scenes.append({"scene_id": f"scene-{index:03d}", "scene_index": index, "script_scene": term,
                       "selected_asset_uids": [candidate.canonical_id for candidate in candidates]})
    intent = {"task_id": task_id, "scenes": scenes}
    selection = {scene["scene_id"]: scene["selected_asset_uids"] for scene in scenes}
    try:
        wired = wire_explicit_asset_hub_bundle(intent, provider, selection)
        bundle_uid = wired["asset_hub"]["bundle_uid"]
        manifest = provider.get_renderer_manifest(bundle_uid)
    except Exception as exc:
        if _status_code(exc) == 503:
            raise MaterialAcquisitionUnavailable("Asset Hub materialization is temporarily unavailable (503)") from exc
        raise
    materials = list(convert_asset_hub_manifest_to_materials(manifest, strict=True))
    if len(materials) != len(decisions):
        raise MaterialAcquisitionError("Asset Hub manifest did not yield the exact selected material count")
    for info, decision in zip(materials, decisions):
        candidate = decision.candidate
        info.source_info = _safe({"provider": "asset_hub", "asset_id": candidate.canonical_id,
                                  "dedupe_key": candidate.dedupe_key, "search_term": candidate.search_term,
                                  "bundle_uid": bundle_uid})
    return materials, bundle_uid


def acquire_selected_materials(*, selection_result: Any, task_id: str,
                               asset_hub_provider: Any = None) -> MaterialAcquisitionResult:
    """Download stock to a task directory and materialize Asset Hub once.

    No task lifecycle or render parameters are changed here.
    """
    task_dir, materials_dir = _task_paths(task_id)
    decisions = list(getattr(selection_result, "decisions", ()) or ())
    hub = [decision for decision in decisions if getattr(decision.candidate, "provider", "") == "asset_hub"]
    if hub and asset_hub_provider is None:
        asset_hub_provider = KurukinAssetProvider()
    hub_infos, bundle_uid = ([], None)
    if hub: hub_infos, bundle_uid = _asset_hub_materials(hub, task_id, asset_hub_provider)
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
