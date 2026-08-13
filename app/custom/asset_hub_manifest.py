import json
import os
from pathlib import Path
from typing import Any


DEFAULT_ASSET_HUB_JOB_ASSETS_DIR = "/data/job-assets"
ALLOWED_ASSET_TYPES = ("video", "image")
ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}


def get_asset_hub_job_assets_dir() -> Path:
    return Path(
        os.environ.get("ASSET_HUB_MATERIALIZED_ROOT")
        or os.environ.get("ASSET_HUB_JOB_ASSETS_DIR")
        or DEFAULT_ASSET_HUB_JOB_ASSETS_DIR
    )


def is_asset_hub_asset_ready(
    asset: dict[str, Any],
    manifest: dict[str, Any] | None = None,
) -> bool:
    if "materialization_status" in asset:
        return asset.get("materialization_status") == "ready"
    if "status" in asset:
        return asset.get("status") == "ready"
    if isinstance(manifest, dict):
        if "materialization_status" in manifest:
            return manifest.get("materialization_status") == "ready"
        return manifest.get("status") == "ready"
    return False


def _resolve_base_dir(base_dir: Path | None = None) -> Path:
    return Path(base_dir or get_asset_hub_job_assets_dir()).resolve()


def _resolve_path_under_base(
    value: str,
    *,
    base_dir: Path | None = None,
    require_file: bool = True,
) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("asset hub path is required")

    base = _resolve_base_dir(base_dir)
    requested = Path(value.strip())
    candidate = requested if requested.is_absolute() else base / requested
    resolved = candidate.resolve()
    try:
        resolved.relative_to(base)
    except ValueError as exc:
        raise ValueError("asset hub path is outside /data/job-assets") from exc

    if require_file and not resolved.is_file():
        raise ValueError(f"asset hub file does not exist: {resolved}")
    return resolved


def resolve_asset_hub_manifest_path(
    path: str,
    base_dir: Path | None = None,
) -> Path:
    resolved = _resolve_path_under_base(path, base_dir=base_dir, require_file=True)
    if resolved.suffix.lower() != ".json":
        raise ValueError("asset hub renderer manifest must be a .json file")
    return resolved


def resolve_asset_hub_asset_path(
    path: str,
    base_dir: Path | None = None,
    *,
    require_file: bool = True,
) -> Path:
    return _resolve_path_under_base(path, base_dir=base_dir, require_file=require_file)


def load_asset_hub_renderer_manifest(
    path: str,
    base_dir: Path | None = None,
) -> dict[str, Any]:
    manifest_path = resolve_asset_hub_manifest_path(path, base_dir=base_dir)
    with manifest_path.open("r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    if not isinstance(manifest, dict):
        raise ValueError("asset hub renderer manifest must be a JSON object")
    return manifest


def validate_asset_hub_renderer_manifest(manifest: dict[str, Any]) -> None:
    if not isinstance(manifest, dict):
        raise ValueError("asset hub renderer manifest must be a JSON object")
    if manifest.get("manifest_version") != "1.0":
        raise ValueError("asset hub renderer manifest_version must be 1.0")
    if manifest.get("generated_by") != "kurukin-asset-hub":
        raise ValueError("asset hub renderer manifest generated_by is invalid")
    if not isinstance(manifest.get("bundle_uid"), str) or not manifest["bundle_uid"]:
        raise ValueError("asset hub renderer manifest bundle_uid is required")

    scenes = manifest.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        raise ValueError("asset hub renderer manifest scenes must be a non-empty list")

    for scene_index, scene in enumerate(scenes):
        if not isinstance(scene, dict):
            raise ValueError(f"asset hub scene {scene_index} must be an object")
        if "scene_index" in scene and not isinstance(scene.get("scene_index"), (int, float)):
            raise ValueError(f"asset hub scene {scene_index} scene_index must be numeric")
        assets = scene.get("assets")
        if not isinstance(assets, list):
            raise ValueError(f"asset hub scene {scene_index} assets must be a list")
        for asset_index, asset in enumerate(assets):
            if not isinstance(asset, dict):
                raise ValueError(
                    f"asset hub scene {scene_index} asset {asset_index} must be an object"
                )
            if not isinstance(asset.get("asset_uid"), str) or not asset["asset_uid"].strip():
                raise ValueError(
                    f"asset hub scene {scene_index} asset {asset_index} asset_uid is required"
                )
            if not isinstance(asset.get("type"), str) or not asset["type"]:
                raise ValueError(
                    f"asset hub scene {scene_index} asset {asset_index} type is required"
                )
            if not asset.get("local_path") and not asset.get("relative_path"):
                raise ValueError(
                    f"asset hub scene {scene_index} asset {asset_index} path is required"
                )
            if not isinstance(asset.get("filename"), str) or not asset["filename"]:
                raise ValueError(
                    f"asset hub scene {scene_index} asset {asset_index} filename is required"
                )


def _scene_sort_key(item: tuple[int, dict[str, Any]]) -> tuple[float, int]:
    index, scene = item
    value = scene.get("scene_index", index)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        value = index
    return (float(value), index)


def _asset_order(assets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for key in ("scene_asset_rank", "rank"):
        if assets and all(
            isinstance(asset.get(key), (int, float)) and not isinstance(asset.get(key), bool)
            for asset in assets
        ):
            return [
                asset
                for _, asset in sorted(
                    ((float(asset[key]), asset) for asset in assets),
                    key=lambda item: item[0],
                )
            ]
    return list(assets)


def _asset_path_value(asset: dict[str, Any]) -> str:
    return str(asset.get("local_path") or asset.get("relative_path") or "")


def extract_asset_hub_local_assets(
    manifest: dict[str, Any],
    base_dir: Path | None = None,
    strict: bool = True,
    allowed_types: tuple[str, ...] = ALLOWED_ASSET_TYPES,
) -> list[dict[str, Any]]:
    validate_asset_hub_renderer_manifest(manifest)
    valid_assets: list[dict[str, Any]] = []
    allowed = set(allowed_types)

    scenes = [
        scene for _, scene in sorted(enumerate(manifest["scenes"]), key=_scene_sort_key)
    ]
    for scene in scenes:
        for asset in _asset_order(scene.get("assets", [])):
            try:
                if not is_asset_hub_asset_ready(asset, manifest):
                    continue

                asset_type = asset.get("type")
                if asset_type not in allowed:
                    raise ValueError(f"unsupported asset hub asset type: {asset_type}")

                resolved_path = resolve_asset_hub_asset_path(
                    _asset_path_value(asset),
                    base_dir=base_dir,
                    require_file=True,
                )
                if asset_type == "image" and resolved_path.suffix.lower() not in ALLOWED_IMAGE_EXTENSIONS:
                    raise ValueError("asset hub image must be jpg, jpeg, or png")

                normalized = dict(asset)
                normalized["local_path"] = str(resolved_path)
                normalized["scene_index"] = scene.get("scene_index")
                normalized["scene_id"] = scene.get("scene_id")
                valid_assets.append(normalized)
            except ValueError:
                if strict:
                    raise
                continue

    if not valid_assets:
        raise ValueError("asset hub renderer manifest has no valid local assets")
    return valid_assets


def convert_asset_hub_manifest_to_materials(
    manifest: dict[str, Any],
    strict: bool = True,
):
    from app.models.schema import MaterialInfo

    assets = extract_asset_hub_local_assets(manifest, strict=strict)
    materials = []
    for asset in assets:
        materials.append(
            MaterialInfo(
                provider="asset_hub",
                url=asset["local_path"],
                duration=int(float(asset.get("duration_seconds") or 0)),
                motion="",
                motion_intensity=0.0,
            )
        )
    return materials


def collect_asset_hub_render_warnings(manifest: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    scenes = manifest.get("scenes") if isinstance(manifest, dict) else []
    if not isinstance(scenes, list):
        return warnings

    for scene in scenes:
        if not isinstance(scene, dict):
            continue
        scene_label = scene.get("scene_id") or scene.get("scene_index") or "unknown"
        if scene.get("needs_human_review"):
            warnings.append(f"scene {scene_label} needs human review")
        for warning in scene.get("render_warnings") or []:
            warnings.append(f"scene {scene_label}: {warning}")
        for asset in scene.get("assets") or []:
            if not isinstance(asset, dict):
                continue
            asset_label = asset.get("asset_id") or asset.get("filename") or "unknown"
            if asset.get("needs_human_review"):
                warnings.append(f"asset {asset_label} needs human review")
            if asset.get("safe_for_subtitles") is False:
                warnings.append(f"asset {asset_label} safe_for_subtitles=false")
            if asset.get("safe_for_text_overlay") is False:
                warnings.append(f"asset {asset_label} safe_for_text_overlay=false")
            for warning in asset.get("render_warnings") or []:
                warnings.append(f"asset {asset_label}: {warning}")
    return warnings


def summarize_asset_hub_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    scenes = manifest.get("scenes") if isinstance(manifest, dict) else []
    if not isinstance(scenes, list):
        scenes = []

    total_assets = 0
    needs_human_review_count = 0
    safe_for_subtitles_false_count = 0
    safe_for_text_overlay_false_count = 0
    for scene in scenes:
        if not isinstance(scene, dict):
            continue
        if scene.get("needs_human_review"):
            needs_human_review_count += 1
        for asset in scene.get("assets") or []:
            if not isinstance(asset, dict):
                continue
            total_assets += 1
            if asset.get("needs_human_review"):
                needs_human_review_count += 1
            if asset.get("safe_for_subtitles") is False:
                safe_for_subtitles_false_count += 1
            if asset.get("safe_for_text_overlay") is False:
                safe_for_text_overlay_false_count += 1

    try:
        valid_assets = len(extract_asset_hub_local_assets(manifest, strict=False))
    except ValueError:
        valid_assets = 0

    return {
        "bundle_uid": manifest.get("bundle_uid"),
        "job_id": manifest.get("job_id"),
        "total_scenes": len(scenes),
        "total_assets": total_assets,
        "valid_assets": valid_assets,
        "warnings_count": len(collect_asset_hub_render_warnings(manifest)),
        "needs_human_review_count": needs_human_review_count,
        "safe_for_subtitles_false_count": safe_for_subtitles_false_count,
        "safe_for_text_overlay_false_count": safe_for_text_overlay_false_count,
    }
