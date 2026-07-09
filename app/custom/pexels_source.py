"""Controlled Pexels source adapter for local B-roll materialization."""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping
from urllib import parse, request


PEXELS_VIDEO_SEARCH_ENDPOINT = "https://api.pexels.com/v1/videos/search"
PEXELS_PROVIDER = "pexels"
ALLOWED_OUTPUT_ROOTS = (
    PurePosixPath("storage/local_videos"),
    PurePosixPath("storage/local_assets"),
)


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _require_desired_count(desired_count: Any) -> int:
    try:
        count = int(desired_count)
    except (TypeError, ValueError) as exc:
        raise ValueError("desired_count must be between 1 and 8") from exc
    if count < 1 or count > 8:
        raise ValueError("desired_count must be between 1 and 8")
    return count


def _has_parent_path(path: PurePosixPath) -> bool:
    return ".." in path.parts


def _is_allowed_relative_asset_root(path: PurePosixPath) -> bool:
    if path.is_absolute() or _has_parent_path(path):
        return False
    return any(path == root or path.is_relative_to(root) for root in ALLOWED_OUTPUT_ROOTS)


def _is_allowed_output_dir(output_dir: Path) -> bool:
    text = output_dir.as_posix()
    if "\\" in text:
        return False

    if output_dir.is_absolute():
        parts = output_dir.parts
        for index in range(len(parts) - 1):
            relative = PurePosixPath(*parts[index:])
            if _is_allowed_relative_asset_root(relative):
                return True
        return False

    return _is_allowed_relative_asset_root(PurePosixPath(text))


def _open_with(opener: Any, req: request.Request):
    if opener is None:
        return request.urlopen(req, timeout=30)
    if callable(opener):
        return opener(req)
    return opener.open(req)


def _read_response_bytes(
    response: Any,
    *,
    max_bytes: int | None = None,
) -> bytes:
    def read(active: Any) -> bytes:
        if max_bytes is None:
            return active.read()
        return active.read(max_bytes)

    if hasattr(response, "__enter__"):
        with response as active:
            return read(active)
    return read(response)


def _json_from_response(response: Any) -> dict[str, Any]:
    payload = _read_response_bytes(response)
    data = json.loads(payload.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Pexels response must be a JSON object")
    return data


def get_pexels_api_key(env: Mapping[str, str] | None = None) -> str | None:
    """Return the configured Pexels API key without logging or side effects."""

    source = env if env is not None else os.environ
    key = _clean_text(source.get("PEXELS_API_KEY"))
    return key or None


def build_pexels_video_search_url(
    *,
    query: str,
    orientation: str = "portrait",
    per_page: int = 8,
    page: int = 1,
) -> str:
    """Build the Pexels videos search URL."""

    params = {
        "query": _clean_text(query),
        "orientation": _clean_text(orientation) or "portrait",
        "per_page": str(_bounded_int(per_page, default=8, minimum=1, maximum=80)),
        "page": str(_bounded_int(page, default=1, minimum=1, maximum=1000)),
    }
    return PEXELS_VIDEO_SEARCH_ENDPOINT + "?" + parse.urlencode(params)


def search_pexels_videos(
    *,
    query: str,
    api_key: str,
    orientation: str = "portrait",
    per_page: int = 8,
    page: int = 1,
    opener=None,
) -> dict[str, Any]:
    """Search Pexels videos through an injected opener or urllib."""

    clean_key = _clean_text(api_key)
    if not clean_key:
        raise ValueError("Pexels API key is not configured")
    url = build_pexels_video_search_url(
        query=query,
        orientation=orientation,
        per_page=per_page,
        page=page,
    )
    req = request.Request(url, headers={"Authorization": clean_key})
    return _json_from_response(_open_with(opener, req))


def _iter_video_file_candidates(video: dict[str, Any]):
    video_id = _clean_text(video.get("id"))
    photographer = _clean_text(video.get("user", {}).get("name")) if isinstance(video.get("user"), dict) else ""
    photographer_url = (
        _clean_text(video.get("user", {}).get("url"))
        if isinstance(video.get("user"), dict)
        else ""
    )
    pexels_url = _clean_text(video.get("url"))

    for file_item in video.get("video_files") or []:
        if not isinstance(file_item, dict):
            continue
        link = _clean_text(file_item.get("link"))
        file_type = _clean_text(file_item.get("file_type")).lower()
        if not link or "mp4" not in file_type:
            continue
        width = _bounded_int(file_item.get("width"), default=0, minimum=0, maximum=100000)
        height = _bounded_int(file_item.get("height"), default=0, minimum=0, maximum=100000)
        yield {
            "source_provider": PEXELS_PROVIDER,
            "pexels_video_id": video_id,
            "photographer": photographer,
            "photographer_url": photographer_url,
            "pexels_url": pexels_url,
            "width": width,
            "height": height,
            "file_type": file_type,
            "link": link,
        }


def _selection_score(item: dict[str, Any], *, prefer_portrait: bool) -> tuple[int, int, int]:
    width = int(item.get("width") or 0)
    height = int(item.get("height") or 0)
    portrait_score = 1 if width and height and width < height else 0
    if not prefer_portrait:
        portrait_score = 0
    area = width * height
    # Prefer usable vertical files, then moderate HD-ish sizes before very large files.
    reasonable = 1 if 300_000 <= area <= 2_500_000 else 0
    return (portrait_score, reasonable, area)


def select_pexels_video_files(
    response: dict,
    *,
    desired_count: int,
    prefer_portrait: bool = True,
) -> list[dict]:
    """Select downloadable Pexels MP4 files with attribution metadata."""

    count = _require_desired_count(desired_count)
    videos = response.get("videos") if isinstance(response, dict) else None
    if not videos:
        raise ValueError("No Pexels videos found")

    selected: list[dict[str, Any]] = []
    seen_video_ids: set[str] = set()
    seen_links: set[str] = set()
    for video in videos:
        if not isinstance(video, dict):
            continue
        candidates = sorted(
            _iter_video_file_candidates(video),
            key=lambda item: _selection_score(item, prefer_portrait=prefer_portrait),
            reverse=True,
        )
        for candidate in candidates:
            video_id = _clean_text(candidate.get("pexels_video_id"))
            link = _clean_text(candidate.get("link"))
            if (video_id and video_id in seen_video_ids) or link in seen_links:
                continue
            if video_id:
                seen_video_ids.add(video_id)
            seen_links.add(link)
            selected.append(candidate)
            break
        if len(selected) >= count:
            break

    if not selected:
        raise ValueError("No downloadable Pexels video files found")
    if len(selected) < count:
        raise ValueError("No downloadable Pexels video files found")
    return selected


def _filename_for_selected_file(item: dict[str, Any], index: int) -> str:
    video_id = _clean_text(item.get("pexels_video_id")) or f"video-{index}"
    link_path = PurePosixPath(parse.urlparse(_clean_text(item.get("link"))).path)
    suffix = link_path.suffix.lower()
    if suffix != ".mp4":
        suffix = ".mp4"
    safe_id = "".join(
        character for character in video_id if character.isalnum() or character in ("-", "_")
    )
    safe_id = safe_id or f"video-{index}"
    return f"pexels-{safe_id}-{index}{suffix}"


def download_pexels_video_files(
    *,
    selected_files: list[dict],
    output_dir: Path,
    opener=None,
    max_bytes_per_file: int | None = None,
) -> list[dict]:
    """Download selected Pexels files into an allowed local asset directory."""

    if not _is_allowed_output_dir(output_dir):
        raise ValueError("Pexels output_dir must be under an allowed local asset root")

    output_dir.mkdir(parents=True, exist_ok=True)
    downloaded: list[dict[str, Any]] = []
    byte_limit = None
    if max_bytes_per_file is not None:
        byte_limit = max(0, int(max_bytes_per_file))

    for index, item in enumerate(selected_files, start=1):
        link = _clean_text(item.get("link"))
        if not link:
            continue
        req = request.Request(link)
        payload = _read_response_bytes(
            _open_with(opener, req),
            max_bytes=byte_limit,
        )
        path = output_dir / _filename_for_selected_file(item, index)
        path.write_bytes(payload)
        metadata = {
            "source_provider": PEXELS_PROVIDER,
            "pexels_video_id": _clean_text(item.get("pexels_video_id")),
            "photographer": _clean_text(item.get("photographer")),
            "photographer_url": _clean_text(item.get("photographer_url")),
            "pexels_url": _clean_text(item.get("pexels_url")),
            "width": int(item.get("width") or 0),
            "height": int(item.get("height") or 0),
            "path": path.as_posix(),
        }
        downloaded.append(metadata)

    if not downloaded:
        raise ValueError("No downloadable Pexels video files found")
    return downloaded


def create_pexels_downloader(
    *,
    api_key: str | None = None,
    env: Mapping[str, str] | None = None,
    opener=None,
) -> Callable:
    """Create an Asset Materializer compatible Pexels downloader."""

    resolved_key = _clean_text(api_key) or get_pexels_api_key(env)
    if not resolved_key:
        raise ValueError("Pexels API key is not configured")

    def downloader(request_data: dict[str, Any]) -> dict[str, Any]:
        if not request_data.get("pexels_enabled", True):
            raise ValueError("Pexels source is disabled")
        needed_count = _require_desired_count(
            request_data.get("needed_count") or request_data.get("desired_count") or 1
        )
        query = _clean_text(request_data.get("query"))
        output_dir = Path(_clean_text(request_data.get("output_dir")))
        response = search_pexels_videos(
            query=query,
            api_key=resolved_key,
            orientation=_clean_text(request_data.get("orientation")) or "portrait",
            per_page=max(needed_count, 8),
            page=1,
            opener=opener,
        )
        selected = select_pexels_video_files(
            response,
            desired_count=needed_count,
            prefer_portrait=True,
        )
        assets_metadata = download_pexels_video_files(
            selected_files=selected,
            output_dir=output_dir,
            opener=opener,
            max_bytes_per_file=request_data.get("max_bytes_per_file"),
        )
        return {
            "source_provider": PEXELS_PROVIDER,
            "assets": [item["path"] for item in assets_metadata],
            "metadata": {
                "source_provider": PEXELS_PROVIDER,
                "pexels_assets": assets_metadata,
            },
        }

    return downloader
