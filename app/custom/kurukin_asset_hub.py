"""Client adapter for Kurukin Asset Hub explicit MPT bundle selection."""

from __future__ import annotations

from copy import deepcopy
import os
from pathlib import Path
import time
from typing import Any, Callable, Mapping
from urllib.parse import urljoin

import requests

from app.custom.asset_hub_manifest import (
    DEFAULT_ASSET_HUB_JOB_ASSETS_DIR,
    is_asset_hub_asset_ready,
)


DEFAULT_CREATED_BY = "money-printer-turbo"
DEFAULT_TIMEOUT_SECONDS = 15
DEFAULT_MATERIALIZE_TIMEOUT_SECONDS = 90
MAX_ATTEMPTS = 3
RETRY_STATUSES = {500, 502, 503, 504}
NO_RETRY_STATUSES = {401, 403, 404, 422}


class KurukinAssetHubError(RuntimeError):
    pass


class KurukinAssetHubAuthError(KurukinAssetHubError):
    pass


class KurukinAssetHubValidationError(KurukinAssetHubError):
    pass


class KurukinAssetHubUnavailableError(KurukinAssetHubError):
    pass


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _safe_message(message: Any) -> str:
    text = str(message or "").strip()
    lowered = text.lower()
    if any(word in lowered for word in ("api_key", "apikey", "authorization", "token", "secret")):
        return "<redacted>"
    return text


def _positive_float(value: Any, default: int | float) -> int | float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if parsed <= 0:
        return default
    return int(parsed) if parsed.is_integer() else parsed


def get_materialized_root(env: Mapping[str, str] | None = None) -> Path:
    source = env if env is not None else os.environ
    return Path(
        source.get("ASSET_HUB_MATERIALIZED_ROOT")
        or source.get("ASSET_HUB_JOB_ASSETS_DIR")
        or DEFAULT_ASSET_HUB_JOB_ASSETS_DIR
    ).resolve()


def dedupe_key(asset_uid: str) -> str:
    clean_uid = validate_asset_uid(asset_uid)
    return f"kurukin_media:{clean_uid}"


def validate_asset_uid(value: Any, *, field_name: str = "asset_uid") -> str:
    if not isinstance(value, str):
        raise KurukinAssetHubValidationError(f"{field_name} is required")
    clean_uid = value.strip()
    if not clean_uid:
        raise KurukinAssetHubValidationError(f"{field_name} is required")
    return clean_uid


def normalize_asset_identity(
    asset: Mapping[str, Any],
    *,
    allow_asset_id_fallback: bool = False,
) -> dict[str, Any]:
    normalized = dict(asset)
    if "asset_uid" in normalized:
        asset_uid = validate_asset_uid(normalized.get("asset_uid"))
    elif allow_asset_id_fallback and "asset_id" in normalized:
        asset_uid = validate_asset_uid(normalized.get("asset_id"), field_name="asset_id")
    else:
        raise KurukinAssetHubValidationError("asset_uid is required")
    normalized["asset_uid"] = asset_uid
    normalized["dedupe_key"] = dedupe_key(asset_uid)
    return normalized


def normalize_source_policy(source_policy: Mapping[str, Any] | None) -> dict[str, Any]:
    if source_policy is None:
        return {"sources": [{"scope": "generic"}]}

    raw_sources = (source_policy or {}).get("sources")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise KurukinAssetHubValidationError("source_policy.sources is required")

    sources: list[dict[str, str]] = []
    for index, source in enumerate(raw_sources):
        if not isinstance(source, Mapping):
            raise KurukinAssetHubValidationError(f"source_policy.sources[{index}] must be an object")
        scope = _clean_text(source.get("scope"))
        if scope == "generic":
            normalized = {"scope": "generic"}
        elif scope == "brand":
            brand = _clean_text(source.get("brand"))
            if not brand:
                raise KurukinAssetHubValidationError("brand source requires brand")
            normalized = {"scope": "brand", "brand": brand}
        elif scope == "title":
            title = _clean_text(source.get("title"))
            if not title:
                raise KurukinAssetHubValidationError("title source requires title")
            normalized = {"scope": "title", "title": title}
        else:
            raise KurukinAssetHubValidationError("source scope must be generic, brand, or title")
        sources.append(normalized)
    return {"sources": sources}


def validate_search_limit(limit: int) -> int:
    try:
        normalized = int(limit)
    except (TypeError, ValueError) as exc:
        raise KurukinAssetHubValidationError("limit must be between 1 and 200") from exc
    if normalized < 1 or normalized > 200:
        raise KurukinAssetHubValidationError("limit must be between 1 and 200")
    return normalized


def validate_materialized_path(
    local_path: str,
    *,
    materialized_root: str | Path | None = None,
) -> str:
    clean_path = _clean_text(local_path)
    if not clean_path:
        raise KurukinAssetHubValidationError("local_path is required")
    requested = Path(clean_path)
    if not requested.is_absolute():
        raise KurukinAssetHubValidationError("local_path must be absolute")
    root = Path(materialized_root).resolve() if materialized_root else get_materialized_root()
    resolved = requested.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise KurukinAssetHubValidationError(f"local_path must stay under {root}") from exc
    return str(resolved)


def _manifest_scenes(manifest: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    scenes = manifest.get("scenes")
    return scenes if isinstance(scenes, list) else []


def resolve_ready_asset_paths(
    manifest: Mapping[str, Any],
    *,
    materialized_root: str | Path | None = None,
) -> list[dict[str, Any]]:
    ready_assets: list[dict[str, Any]] = []
    for scene in _manifest_scenes(manifest):
        if not isinstance(scene, Mapping):
            continue
        for asset in scene.get("assets") or []:
            if not isinstance(asset, Mapping) or not is_asset_hub_asset_ready(dict(asset)):
                continue
            normalized = normalize_asset_identity(asset)
            local_path = validate_materialized_path(
                _clean_text(asset.get("local_path")),
                materialized_root=materialized_root,
            )
            ready_assets.append(
                {
                    "asset_uid": normalized["asset_uid"],
                    "local_path": local_path,
                    "relative_path": _clean_text(asset.get("relative_path")),
                    "size_bytes": asset.get("size_bytes"),
                    "sha256": _clean_text(asset.get("sha256")),
                }
            )
    return ready_assets


def _scene_payload(scene: Mapping[str, Any], index: int) -> dict[str, Any]:
    selected_asset_uids = [
        validate_asset_uid(asset_uid, field_name=f"scenes[{index}].selected_asset_uids")
        for asset_uid in scene.get("selected_asset_uids") or []
    ]
    if not selected_asset_uids:
        raise KurukinAssetHubValidationError(f"scenes[{index}].selected_asset_uids is required")
    payload = {
        "scene_id": _clean_text(scene.get("scene_id")) or f"scene-{index + 1:03d}",
        "scene_index": int(scene.get("scene_index") or index + 1),
        "script_scene": _clean_text(scene.get("script_scene")),
        "selected_asset_uids": selected_asset_uids,
    }
    if "count" in scene:
        payload["count"] = len(selected_asset_uids)
    return payload


def build_explicit_bundle_payload(
    *,
    job_id: str,
    scenes: list[Mapping[str, Any]],
    created_by: str = DEFAULT_CREATED_BY,
    brand_slug: str = "",
) -> dict[str, Any]:
    clean_job_id = _clean_text(job_id)
    if not clean_job_id:
        raise KurukinAssetHubValidationError("job_id is required")
    if not isinstance(scenes, list) or not scenes:
        raise KurukinAssetHubValidationError("scenes is required")
    payload = {
        "job_id": clean_job_id,
        "created_by": _clean_text(created_by) or DEFAULT_CREATED_BY,
        "scenes": [_scene_payload(scene, index) for index, scene in enumerate(scenes)],
    }
    clean_brand_slug = _clean_text(brand_slug)
    if clean_brand_slug:
        payload["brand_slug"] = clean_brand_slug
    return payload


class KurukinAssetProvider:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        session: requests.Session | None = None,
        timeout: int | float = DEFAULT_TIMEOUT_SECONDS,
        materialize_timeout: int | float | None = None,
        sleeper: Callable[[float], None] | None = None,
        backoff_seconds: tuple[float, ...] = (0.1, 0.2),
        env: Mapping[str, str] | None = None,
    ) -> None:
        source = env if env is not None else os.environ
        self.base_url = (_clean_text(base_url) or _clean_text(source.get("ASSET_HUB_BASE_URL"))).rstrip("/")
        self.api_key = _clean_text(api_key) or _clean_text(source.get("ASSET_HUB_API_KEY"))
        if not self.base_url:
            raise KurukinAssetHubValidationError("ASSET_HUB_BASE_URL is required")
        if not self.api_key:
            raise KurukinAssetHubAuthError("ASSET_HUB_API_KEY is required")
        self.session = session or requests.Session()
        self.timeout = _positive_float(
            source.get("ASSET_HUB_TIMEOUT_SECONDS"),
            timeout,
        )
        self.materialize_timeout = _positive_float(
            source.get("ASSET_HUB_MATERIALIZE_TIMEOUT_SECONDS"),
            materialize_timeout
            if materialize_timeout is not None
            else DEFAULT_MATERIALIZE_TIMEOUT_SECONDS,
        )
        self.sleeper = sleeper or time.sleep
        self.backoff_seconds = backoff_seconds

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Asset-Hub-Api-Key": self.api_key,
        }

    def _url(self, path: str) -> str:
        return urljoin(f"{self.base_url}/", path.lstrip("/"))

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        max_attempts: int = MAX_ATTEMPTS,
        timeout: int | float | None = None,
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        attempts = max(1, int(max_attempts))
        for attempt in range(attempts):
            try:
                response = self.session.request(
                    method,
                    self._url(path),
                    headers=self._headers(),
                    json=deepcopy(json_body) if json_body is not None else None,
                    timeout=self.timeout if timeout is None else timeout,
                )
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_error = exc
                if attempt == attempts - 1:
                    raise KurukinAssetHubUnavailableError("Asset Hub is unavailable after retries") from exc
                self._sleep(attempt)
                continue

            status = int(getattr(response, "status_code", 0) or 0)
            if 200 <= status < 300:
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise KurukinAssetHubError("Asset Hub response must be JSON") from exc
                return payload if isinstance(payload, dict) else {"data": payload}

            if status in NO_RETRY_STATUSES:
                raise self._status_error(status, response)
            if status in RETRY_STATUSES and attempt < attempts - 1:
                self._sleep(attempt)
                continue
            raise self._status_error(status, response)

        raise KurukinAssetHubUnavailableError(_safe_message(last_error) or "Asset Hub request failed")

    def _sleep(self, attempt: int) -> None:
        if not self.backoff_seconds:
            return
        index = min(attempt, len(self.backoff_seconds) - 1)
        delay = self.backoff_seconds[index]
        if delay > 0:
            self.sleeper(delay)

    def _status_error(self, status: int, response: Any) -> KurukinAssetHubError:
        if status in (401, 403):
            return KurukinAssetHubAuthError(f"Asset Hub authorization failed: HTTP {status}")
        if status == 422:
            return KurukinAssetHubValidationError("Asset Hub rejected the request: HTTP 422")
        if status == 404:
            return KurukinAssetHubValidationError("Asset Hub resource was not found: HTTP 404")
        if status in RETRY_STATUSES:
            return KurukinAssetHubUnavailableError(f"Asset Hub is unavailable: HTTP {status}")
        return KurukinAssetHubError(f"Asset Hub request failed: HTTP {status}")

    def search(
        self,
        *,
        query: str,
        limit: int = 20,
        source_policy: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        payload = {
            "query": _clean_text(query),
            "limit": validate_search_limit(limit),
            "source_policy": normalize_source_policy(source_policy),
        }
        response = self._request("POST", "/api/assets/search", json_body=payload)
        assets = response.get("assets") or []
        if not isinstance(assets, list):
            raise KurukinAssetHubError("Asset Hub search assets must be a list")
        return [
            normalize_asset_identity(asset, allow_asset_id_fallback=True)
            for asset in assets
            if isinstance(asset, Mapping)
        ]

    def create_bundle(
        self,
        *,
        job_id: str,
        scenes: list[Mapping[str, Any]],
        created_by: str = DEFAULT_CREATED_BY,
        brand_slug: str = "",
    ) -> dict[str, Any]:
        payload = build_explicit_bundle_payload(
            job_id=job_id,
            scenes=scenes,
            created_by=created_by,
            brand_slug=brand_slug,
        )
        return self._request("POST", "/api/jobs/asset-bundles", json_body=payload, max_attempts=1)

    def materialize_bundle(self, bundle_uid: str, *, force: bool = False) -> dict[str, Any]:
        clean_uid = _clean_text(bundle_uid)
        if not clean_uid:
            raise KurukinAssetHubValidationError("bundle_uid is required")
        return self._request(
            "POST",
            f"/api/jobs/asset-bundles/{clean_uid}/materialize",
            json_body={"force": bool(force)},
            max_attempts=MAX_ATTEMPTS if not force else 1,
            timeout=self.materialize_timeout,
        )

    def get_renderer_manifest(self, bundle_uid: str) -> dict[str, Any]:
        clean_uid = _clean_text(bundle_uid)
        if not clean_uid:
            raise KurukinAssetHubValidationError("bundle_uid is required")
        return self._request(
            "GET",
            f"/api/jobs/asset-bundles/{clean_uid}/renderer-manifest",
        )


KurukinAssetHub = KurukinAssetProvider
