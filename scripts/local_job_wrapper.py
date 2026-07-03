#!/usr/bin/env python3
"""Build local-asset MoneyPrinterTurbo jobs for the nightly queue."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any


DEFAULT_QUEUE_DIR = "/opt/moneyprinterturbo/storage/nightly_jobs"
DEFAULT_LOCAL_VIDEOS_DIR = "/opt/moneyprinterturbo/storage/local_videos"
ALLOWED_EXTENSIONS = {"mp4", "mov", "avi", "flv", "mkv", "jpg", "jpeg", "png"}
ALLOWED_VIDEO_ASPECTS = {"9:16", "16:9"}


class LocalJobWrapperError(Exception):
    """Expected validation or enqueue error."""


def load_job_spec(path: str | Path) -> dict[str, Any]:
    job_path = Path(path)
    try:
        with job_path.open("r", encoding="utf-8") as handle:
            spec = json.load(handle)
    except FileNotFoundError as exc:
        raise LocalJobWrapperError(f"job spec not found: {job_path}") from exc
    except json.JSONDecodeError as exc:
        raise LocalJobWrapperError(f"job spec is not valid JSON: {exc}") from exc

    if not isinstance(spec, dict):
        raise LocalJobWrapperError("job spec must be a JSON object")
    return spec


def validate_asset_filename(filename: Any) -> str:
    if not isinstance(filename, str) or not filename.strip():
        raise LocalJobWrapperError("asset file is required and must be non-empty")

    value = filename.strip()
    if Path(value).is_absolute() or "/" in value or "\\" in value:
        raise LocalJobWrapperError(f"asset file must be a filename only: {filename!r}")
    if value in {".", ".."} or ".." in Path(value).parts:
        raise LocalJobWrapperError(f"asset file cannot use parent paths: {filename!r}")
    if Path(value).name != value:
        raise LocalJobWrapperError(f"asset file must be a filename only: {filename!r}")

    extension = Path(value).suffix.lower().lstrip(".")
    if extension not in ALLOWED_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_EXTENSIONS))
        raise LocalJobWrapperError(
            f"asset file has unsupported extension {extension!r}; allowed: {allowed}"
        )
    return value


def resolve_local_asset(filename: str, local_videos_dir: str | Path) -> Path:
    safe_filename = validate_asset_filename(filename)
    base_dir = Path(local_videos_dir).resolve()
    candidate = base_dir / safe_filename
    if not candidate.exists():
        raise LocalJobWrapperError(f"local asset does not exist: {candidate}")

    resolved = candidate.resolve()
    try:
        resolved.relative_to(base_dir)
    except ValueError as exc:
        raise LocalJobWrapperError(
            f"local asset resolves outside local videos dir: {safe_filename}"
        ) from exc

    if not resolved.is_file():
        raise LocalJobWrapperError(f"local asset is not a file: {candidate}")
    return resolved


def probe_media_dimensions(path: str | Path) -> tuple[int, int]:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height",
        "-of",
        "json",
        str(path),
    ]
    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise LocalJobWrapperError(
            "ffprobe was not found; install ffmpeg or use --skip-media-probe"
        ) from exc
    except subprocess.CalledProcessError as exc:
        details = (exc.stderr or exc.stdout or "").strip()
        suffix = f": {details}" if details else ""
        raise LocalJobWrapperError(f"ffprobe failed for {path}{suffix}") from exc

    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise LocalJobWrapperError(f"ffprobe returned invalid JSON for {path}") from exc

    streams = payload.get("streams")
    if not isinstance(streams, list) or not streams:
        raise LocalJobWrapperError(f"ffprobe found no visual stream for {path}")

    stream = streams[0]
    width = stream.get("width")
    height = stream.get("height")
    if not isinstance(width, int) or not isinstance(height, int):
        raise LocalJobWrapperError(f"ffprobe did not return width/height for {path}")
    return width, height


def _order_value(asset: dict[str, Any], index: int) -> tuple[int, float, int]:
    if "order" not in asset:
        return (1, 0, index)
    value = asset["order"]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LocalJobWrapperError("asset order must be a number when provided")
    return (0, float(value), index)


def _ordered_assets(assets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not any("order" in asset for asset in assets):
        return list(assets)
    return [
        asset
        for _, asset in sorted(
            (( _order_value(asset, index), asset) for index, asset in enumerate(assets)),
            key=lambda item: item[0],
        )
    ]


def validate_job_spec(
    spec: dict[str, Any],
    local_videos_dir: str | Path = DEFAULT_LOCAL_VIDEOS_DIR,
    min_width: int = 480,
    min_height: int = 480,
    skip_media_probe: bool = False,
) -> list[dict[str, Any]]:
    if not isinstance(spec, dict):
        raise LocalJobWrapperError("job spec must be a JSON object")

    job_id = spec.get("job_id")
    if not isinstance(job_id, str) or not job_id.strip():
        raise LocalJobWrapperError("job_id is required and must be a non-empty string")

    video = spec.get("video")
    if not isinstance(video, dict):
        raise LocalJobWrapperError("video is required and must be a JSON object")

    for key in ("video_subject", "video_script"):
        value = video.get(key)
        if not isinstance(value, str) or not value.strip():
            raise LocalJobWrapperError(f"video.{key} is required")

    video_aspect = video.get("video_aspect")
    if video_aspect not in ALLOWED_VIDEO_ASPECTS:
        raise LocalJobWrapperError('video.video_aspect must be "9:16" or "16:9"')

    assets = spec.get("selectedAssets")
    if not isinstance(assets, list) or not assets:
        raise LocalJobWrapperError("selectedAssets must be a non-empty list")

    normalized_assets: list[dict[str, Any]] = []
    seen_files: set[str] = set()
    for index, asset in enumerate(assets):
        if not isinstance(asset, dict):
            raise LocalJobWrapperError(
                f"selectedAssets[{index}] must be a JSON object"
            )
        filename = validate_asset_filename(asset.get("file"))
        if filename in seen_files:
            raise LocalJobWrapperError(f"duplicate selected asset file: {filename}")
        seen_files.add(filename)

        resolved_path = resolve_local_asset(filename, local_videos_dir)
        if not skip_media_probe:
            width, height = probe_media_dimensions(resolved_path)
            if width < min_width or height < min_height:
                raise LocalJobWrapperError(
                    f"local asset is too small: {filename} "
                    f"({width}x{height}, minimum {min_width}x{min_height})"
                )

        normalized = dict(asset)
        normalized["file"] = filename
        normalized_assets.append(normalized)

    return _ordered_assets(normalized_assets)


def build_pending_job(
    spec: dict[str, Any],
    ordered_assets: list[dict[str, Any]],
) -> dict[str, Any]:
    video = spec.get("video")
    if not isinstance(video, dict):
        raise LocalJobWrapperError("video is required and must be a JSON object")

    pending_job: dict[str, Any] = {}
    job_id = spec.get("job_id")
    description = spec.get("description")
    if job_id is not None:
        pending_job["job_id"] = job_id
    if description is not None:
        pending_job["description"] = description

    pending_job["runner"] = {
        "source": "local_job_wrapper",
        "selectedAssets": ordered_assets,
    }
    pending_job.update(video)
    pending_job["video_source"] = "local"
    pending_job["video_materials"] = [
        {
            "provider": "local",
            "url": asset["file"],
            "duration": 0,
        }
        for asset in ordered_assets
    ]
    pending_job.pop("selectedAssets", None)
    return pending_job


def slugify(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9._-]+", "-", text)
    text = text.strip(".-")
    return text or "job"


def _utc_timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    temp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}-{time.time_ns()}")
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


def enqueue_job(pending_job: dict[str, Any], queue_dir: str | Path) -> Path:
    pending_dir = Path(queue_dir) / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)

    stem = f"{_utc_timestamp()}-{slugify(pending_job.get('job_id'))}"
    candidate = pending_dir / f"{stem}.json"
    counter = 1
    while candidate.exists():
        candidate = pending_dir / f"{stem}-{counter}.json"
        counter += 1
        if counter > 100:
            candidate = pending_dir / f"{stem}-{time.time_ns()}.json"
            break

    _write_json_atomic(candidate, pending_job)
    return candidate


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{value!r} must be an integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError(f"{value!r} must be >= 1")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build and enqueue local-asset MoneyPrinterTurbo jobs"
    )
    parser.add_argument("job_spec")
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--validate-only", action="store_true")
    actions.add_argument("--enqueue", action="store_true")
    actions.add_argument("--print-payload", action="store_true")
    parser.add_argument("--queue-dir", default=DEFAULT_QUEUE_DIR)
    parser.add_argument("--local-videos-dir", default=DEFAULT_LOCAL_VIDEOS_DIR)
    parser.add_argument("--min-width", default=480, type=positive_int)
    parser.add_argument("--min-height", default=480, type=positive_int)
    parser.add_argument("--skip-media-probe", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        spec = load_job_spec(args.job_spec)
        ordered_assets = validate_job_spec(
            spec,
            local_videos_dir=args.local_videos_dir,
            min_width=args.min_width,
            min_height=args.min_height,
            skip_media_probe=args.skip_media_probe,
        )
        pending_job = build_pending_job(spec, ordered_assets)

        if args.print_payload:
            json.dump(pending_job, sys.stdout, indent=2, ensure_ascii=False, sort_keys=True)
            sys.stdout.write("\n")
            return 0

        if args.validate_only:
            print("OK: job spec is valid")
            return 0

        written_path = enqueue_job(pending_job, args.queue_dir)
        print(f"enqueued: {written_path}")
        return 0
    except LocalJobWrapperError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
