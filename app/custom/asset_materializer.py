"""Pure asset materialization helpers for Kurukin A-roll/B-roll jobs."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping

from app.custom.asset_source_policy import (
    ASSET_SOURCE_ASSET_HUB,
    ASSET_SOURCE_LOCAL_LIBRARY,
    ASSET_SOURCE_MANIFEST,
    ASSET_SOURCE_MODE_EXCLUSIVE_BRAND_ASSETS,
    ASSET_SOURCE_MODE_LOCAL_ONLY,
    ASSET_SOURCE_MODE_OPEN_SOURCES,
    ASSET_SOURCE_PEXELS,
    ASSET_SOURCE_UPLOADED,
    is_source_allowed,
    normalize_asset_source_policy,
    summarize_asset_source_policy,
    validate_asset_source_policy,
)


DEFAULT_DESIRED_COUNT = 3
MAX_MATERIALIZED_ASSETS = 8
ALLOWED_OUTPUT_ROOTS = (
    "storage/local_videos",
    "storage/local_assets",
)
ALLOWED_ASSET_ROOTS = (
    "storage/local_videos",
    "storage/local_assets",
    "storage/local_images",
)
DEFAULT_MATERIALIZED_ROOT = "storage/local_videos/_aroll_broll_materialized"


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _dedupe(values: list[str]) -> list[str]:
    normalized: list[str] = []
    for value in values:
        text = _clean_text(value)
        if text and text not in normalized:
            normalized.append(text)
    return normalized


def _clean_path_list(value: Any) -> list[str]:
    if isinstance(value, str):
        raw_items = value.replace(",", "\n").splitlines()
    elif isinstance(value, (list, tuple)):
        raw_items = list(value)
    else:
        raw_items = []

    cleaned: list[str] = []
    for item in raw_items:
        if isinstance(item, dict):
            cleaned.append(_clean_text(item.get("path")))
        else:
            cleaned.append(_clean_text(item))
    return _dedupe(cleaned)


def _has_path_traversal(value: str) -> bool:
    if "\\" in value:
        return True
    return ".." in PurePosixPath(value).parts


def _default_output_dir(raw: dict[str, Any]) -> str:
    job_id = _clean_text(raw.get("job_id")) or "default"
    safe_job_id = "".join(
        character
        for character in job_id
        if character.isalnum() or character in ("-", "_")
    ).strip("-_")
    safe_job_id = safe_job_id or "default"
    return f"{DEFAULT_MATERIALIZED_ROOT}/{safe_job_id}"


def normalize_asset_materialization_request(
    raw: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return normalized materialization request data without side effects."""

    source = deepcopy(raw) if isinstance(raw, dict) else {}
    try:
        desired_count = int(source.get("desired_count", DEFAULT_DESIRED_COUNT))
    except (TypeError, ValueError):
        desired_count = DEFAULT_DESIRED_COUNT

    asset_policy = normalize_asset_source_policy(source.get("asset_policy"))
    brand_uid = _clean_text(source.get("brand_asset_bundle_uid")) or _clean_text(
        asset_policy.get("brand_asset_bundle_uid")
    )
    manifest_path = _clean_text(source.get("manifest_path")) or None
    if not manifest_path and brand_uid:
        manifest_path = (
            f"/data/job-assets/{brand_uid}/manifests/renderer-manifest.json"
        )

    return {
        "asset_policy": asset_policy,
        "query": _clean_text(source.get("query")),
        "desired_count": desired_count,
        "output_dir": _clean_text(source.get("output_dir"))
        or _default_output_dir(source),
        "local_candidates": _clean_path_list(source.get("local_candidates")),
        "manifest_path": manifest_path,
        "brand_asset_bundle_uid": brand_uid or None,
        "metadata": deepcopy(source.get("metadata"))
        if isinstance(source.get("metadata"), dict)
        else {},
    }


def _is_local_path(value: str, allowed_roots: tuple[str, ...]) -> bool:
    text = _clean_text(value)
    if not text or _has_path_traversal(text):
        return False
    path = PurePosixPath(text)
    if path.is_absolute():
        return False
    return any(
        path == PurePosixPath(root) or path.is_relative_to(PurePosixPath(root))
        for root in allowed_roots
    )


def _validate_output_dir(output_dir: str) -> list[str]:
    if _is_local_path(output_dir, ALLOWED_OUTPUT_ROOTS):
        return []
    return [
        "output_dir must stay under storage/local_videos or storage/local_assets"
    ]


def _validate_asset_paths(paths: list[str]) -> list[str]:
    errors: list[str] = []
    for path in paths:
        if not _is_local_path(path, ALLOWED_ASSET_ROOTS):
            errors.append("Materialized assets must be local paths")
            break
    return errors


def _provider_label(source_provider: str) -> str:
    return {
        ASSET_SOURCE_PEXELS: "Pexels",
        ASSET_SOURCE_LOCAL_LIBRARY: "local",
        ASSET_SOURCE_ASSET_HUB: "Asset Hub",
        ASSET_SOURCE_MANIFEST: "manifest",
        "mixed": "mixed",
    }.get(source_provider, source_provider or "-")


def _error_result(
    request: dict[str, Any],
    errors: list[str],
    *,
    source_provider: str = "",
    assets: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "ok": False,
        "errors": errors,
        "source_policy": request.get("asset_policy"),
        "source_provider": source_provider,
        "b_roll_assets": list(assets or []),
        "b_roll_asset_count": len(assets or []),
        "metadata": {
            "query": request.get("query") or "",
            "materialized": False,
            "asset_policy": summarize_asset_source_policy(
                request.get("asset_policy")
            ),
        },
    }


def _ok_result(
    request: dict[str, Any],
    *,
    source_provider: str,
    assets: list[str],
    extra_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata = {
        "query": request.get("query") or "",
        "materialized": True,
        "source_label": _provider_label(source_provider),
        "asset_policy": summarize_asset_source_policy(request.get("asset_policy")),
    }
    if extra_metadata:
        metadata.update(extra_metadata)
    return {
        "ok": True,
        "errors": [],
        "source_policy": request.get("asset_policy"),
        "source_provider": source_provider,
        "b_roll_assets": list(assets),
        "b_roll_asset_count": len(assets),
        "metadata": metadata,
    }


def _read_manifest_file(manifest_path: str) -> dict[str, Any]:
    path = Path(manifest_path)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _extract_manifest_assets(value: Any) -> list[str]:
    if isinstance(value, list):
        return _clean_path_list(value)
    if not isinstance(value, dict):
        return []

    assets: list[str] = []
    direct_assets = value.get("assets")
    if isinstance(direct_assets, list):
        for item in direct_assets:
            if isinstance(item, dict):
                assets.append(
                    _clean_text(
                        item.get("path")
                        or item.get("local_path")
                        or item.get("resolved_path")
                    )
                )
            else:
                assets.append(_clean_text(item))

    scenes = value.get("scenes")
    if isinstance(scenes, list):
        for scene in scenes:
            if not isinstance(scene, dict):
                continue
            for item in scene.get("assets") or []:
                if isinstance(item, dict):
                    assets.append(
                        _clean_text(
                            item.get("path")
                            or item.get("local_path")
                            or item.get("resolved_path")
                        )
                    )
    return _dedupe(assets)


def _manifest_payload(
    manifest_reader: Callable[..., Any] | None,
    manifest_path: str,
) -> Any:
    if manifest_reader is not None:
        return manifest_reader(manifest_path)
    return _read_manifest_file(manifest_path)


def _downloader_payload(
    downloader: Callable[..., Any],
    request: dict[str, Any],
    needed_count: int,
) -> Any:
    downloader_request = deepcopy(request)
    downloader_request["needed_count"] = needed_count
    return downloader(downloader_request)


def _extract_downloader_assets(
    value: Any,
    *,
    default_provider: str = "",
) -> tuple[list[str], str, dict[str, Any]]:
    if isinstance(value, list):
        return _clean_path_list(value), default_provider, {}
    if not isinstance(value, dict):
        return [], "", {}
    assets = _clean_path_list(value.get("assets") or value.get("b_roll_assets"))
    source_provider = _clean_text(value.get("source_provider")) or default_provider
    metadata = deepcopy(value.get("metadata")) if isinstance(value.get("metadata"), dict) else {}
    return assets, source_provider, metadata


def _select_local_candidates(request: dict[str, Any]) -> list[str]:
    policy = request["asset_policy"]
    if not (
        is_source_allowed(policy, ASSET_SOURCE_LOCAL_LIBRARY)
        or is_source_allowed(policy, ASSET_SOURCE_UPLOADED)
    ):
        return []
    return [
        path
        for path in request.get("local_candidates", [])
        if _is_local_path(path, ALLOWED_ASSET_ROOTS)
    ]


def _materialize_open_sources(
    request: dict[str, Any],
    *,
    downloader: Callable[..., Any] | None,
    source_adapters: Mapping[str, Callable[..., Any]] | None,
) -> dict[str, Any]:
    desired_count = request["desired_count"]
    assets = _select_local_candidates(request)[:desired_count]
    if len(assets) >= desired_count:
        return _ok_result(
            request,
            source_provider=ASSET_SOURCE_LOCAL_LIBRARY,
            assets=assets,
        )

    policy = request["asset_policy"]
    adapter_sources = list(policy.get("allowed_sources", []))
    external_sources = [
        source
        for source in adapter_sources
        if source not in (ASSET_SOURCE_LOCAL_LIBRARY, ASSET_SOURCE_UPLOADED)
    ]
    external_allowed = bool(external_sources)
    configured_adapters = {
        _clean_text(source): adapter
        for source, adapter in (source_adapters or {}).items()
        if callable(adapter)
    }
    allowed_adapters = [
        (source, configured_adapters[source])
        for source in adapter_sources
        if source in configured_adapters
    ]
    legacy_downloader = downloader if external_allowed else None
    if not allowed_adapters and legacy_downloader is None:
        if not external_allowed:
            return _error_result(
                request,
                ["Local-only policy requires enough local candidates"],
                assets=assets,
            )
        return _error_result(
            request,
            ["External downloader is not configured"],
            assets=assets,
        )

    providers = [ASSET_SOURCE_LOCAL_LIBRARY] if assets else []
    combined_metadata: dict[str, Any] = {}
    adapter_metadata: list[dict[str, Any]] = []

    def consume_adapter(
        adapter: Callable[..., Any],
        *,
        default_provider: str = "",
    ) -> dict[str, Any] | None:
        nonlocal assets
        downloaded, provider, metadata = _extract_downloader_assets(
            _downloader_payload(adapter, request, desired_count - len(assets)),
            default_provider=default_provider,
        )
        if downloaded and not provider:
            return _error_result(
                request,
                ["External downloader must identify source_provider"],
                assets=assets,
            )
        if provider and not is_source_allowed(policy, provider):
            return _error_result(
                request,
                [f"Source provider is not allowed: {provider}"],
                assets=assets,
            )
        assets = _dedupe([*assets, *downloaded])[:desired_count]
        asset_errors = _validate_asset_paths(assets)
        if asset_errors:
            return _error_result(request, asset_errors, assets=assets)
        if downloaded and provider and provider not in providers:
            providers.append(provider)
        if metadata:
            combined_metadata.update(metadata)
            adapter_metadata.append(
                {
                    "source_provider": provider,
                    "metadata": metadata,
                }
            )
        return None

    for source, adapter in allowed_adapters:
        error = consume_adapter(adapter, default_provider=source)
        if error:
            return error
        if len(assets) >= desired_count:
            break

    if len(assets) < desired_count and legacy_downloader is not None:
        error = consume_adapter(legacy_downloader)
        if error:
            return error

    if len(assets) < desired_count:
        return _error_result(
            request,
            ["External downloader did not return enough local assets"],
            assets=assets,
        )
    if adapter_metadata:
        combined_metadata["source_adapter_metadata"] = adapter_metadata
    provider = providers[0] if len(providers) == 1 else "mixed"
    return _ok_result(
        request,
        source_provider=provider,
        assets=assets,
        extra_metadata=combined_metadata,
    )


def _materialize_local_only(request: dict[str, Any]) -> dict[str, Any]:
    desired_count = request["desired_count"]
    assets = _select_local_candidates(request)[:desired_count]
    if len(assets) < desired_count:
        return _error_result(
            request,
            ["Local-only policy requires enough local candidates"],
            assets=assets,
        )
    return _ok_result(
        request,
        source_provider=ASSET_SOURCE_LOCAL_LIBRARY,
        assets=assets,
    )


def _materialize_exclusive_brand(
    request: dict[str, Any],
    *,
    manifest_reader: Callable[..., Any] | None,
) -> dict[str, Any]:
    desired_count = request["desired_count"]
    manifest_path = _clean_text(request.get("manifest_path"))
    if not manifest_path:
        return _error_result(
            request,
            ["Exclusive brand assets require a local manifest"],
        )
    if manifest_reader is None and not Path(manifest_path).is_file():
        return _error_result(
            request,
            ["Exclusive brand assets require a local manifest"],
        )

    assets = _extract_manifest_assets(
        _manifest_payload(manifest_reader, manifest_path)
    )[:desired_count]
    asset_errors = _validate_asset_paths(assets)
    if asset_errors:
        return _error_result(
            request,
            asset_errors,
            source_provider=ASSET_SOURCE_MANIFEST,
            assets=assets,
        )
    if len(assets) < desired_count:
        return _error_result(
            request,
            ["Exclusive brand assets require enough manifest assets"],
            source_provider=ASSET_SOURCE_MANIFEST,
            assets=assets,
        )
    provider = request["asset_policy"].get("exclusive_source") or ASSET_SOURCE_ASSET_HUB
    return _ok_result(
        request,
        source_provider=provider,
        assets=assets,
        extra_metadata={"manifest_path": manifest_path},
    )


def materialize_assets_for_aroll_broll(
    request: dict[str, Any],
    project_root: Path,
    downloader: Callable[..., Any] | None = None,
    source_adapters: Mapping[str, Callable[..., Any]] | None = None,
    manifest_reader: Callable[..., Any] | None = None,
    local_library_resolver: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Prepare local B-roll asset paths from a source policy.

    The function does not render, enqueue, shell out, or call any provider by
    itself. External sourcing is possible only through injected callables.
    """

    del project_root
    normalized = normalize_asset_materialization_request(request)
    policy = normalized["asset_policy"]
    errors = validate_asset_source_policy(policy)
    if normalized["desired_count"] < 1 or normalized["desired_count"] > MAX_MATERIALIZED_ASSETS:
        errors.append("desired_count must be between 1 and 8")
    errors.extend(_validate_output_dir(normalized["output_dir"]))
    errors.extend(_validate_asset_paths(normalized["local_candidates"]))
    if errors:
        return _error_result(normalized, errors)

    if (
        local_library_resolver is not None
        and is_source_allowed(policy, ASSET_SOURCE_LOCAL_LIBRARY)
    ):
        resolved = local_library_resolver(deepcopy(normalized))
        normalized["local_candidates"] = _dedupe(
            [
                *normalized.get("local_candidates", []),
                *_clean_path_list(resolved),
            ]
        )
        candidate_errors = _validate_asset_paths(normalized["local_candidates"])
        if candidate_errors:
            return _error_result(normalized, candidate_errors)

    mode = policy.get("mode")
    if mode == ASSET_SOURCE_MODE_OPEN_SOURCES:
        return _materialize_open_sources(
            normalized,
            downloader=downloader,
            source_adapters=source_adapters,
        )
    if mode == ASSET_SOURCE_MODE_LOCAL_ONLY:
        return _materialize_local_only(normalized)
    if mode == ASSET_SOURCE_MODE_EXCLUSIVE_BRAND_ASSETS:
        return _materialize_exclusive_brand(
            normalized,
            manifest_reader=manifest_reader,
        )
    return _error_result(normalized, ["No allowed sources available"])
