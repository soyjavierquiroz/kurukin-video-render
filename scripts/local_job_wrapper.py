#!/usr/bin/env python3
"""CLI for building local Kurukin jobs for the MoneyPrinterTurbo queue."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.custom import kurukin_job_adapter as adapter


DEFAULT_QUEUE_DIR = "/opt/moneyprinterturbo/storage/nightly_jobs"
DEFAULT_LOCAL_VIDEOS_DIR = "/opt/moneyprinterturbo/storage/local_videos"
DEFAULT_LOCAL_AUDIOS_DIR = "/opt/moneyprinterturbo/storage/local_audios"
DEFAULT_LOCAL_SUBTITLES_DIR = "/opt/moneyprinterturbo/storage/local_subtitles"
DEFAULT_FONTS_DIR = "/opt/moneyprinterturbo/resource/fonts"
DEFAULT_ASSET_HUB_JOB_ASSETS_DIR = adapter.DEFAULT_ASSET_HUB_JOB_ASSETS_DIR
PROJECT_ROOT = adapter.PROJECT_ROOT

LocalJobWrapperError = adapter.KurukinJobAdapterError

load_job_spec = adapter.load_kurukin_job_spec
load_kurukin_job_spec = adapter.load_kurukin_job_spec
validate_asset_filename = adapter.validate_asset_filename
validate_local_filename = adapter.validate_local_filename
resolve_local_asset = adapter.resolve_local_asset
resolve_local_file = adapter.resolve_local_file
normalize_asset_hub_manifest_path = adapter.normalize_asset_hub_manifest_path
normalize_asset_hub_scene_mode = adapter.normalize_asset_hub_scene_mode
normalize_asset_hub_bundle_uid = adapter.normalize_asset_hub_bundle_uid
normalize_render_quality = adapter.normalize_render_quality
normalize_image_motion_preset = adapter.normalize_image_motion_preset
normalize_image_motion_intensity = adapter.normalize_image_motion_intensity
normalize_image_motion_config = adapter.normalize_image_motion_config
normalize_asset_motion = adapter.normalize_asset_motion
probe_media_dimensions = adapter.probe_media_dimensions
build_runner_metadata = adapter.build_runner_metadata
build_video_materials_from_selected_assets = (
    adapter.build_video_materials_from_selected_assets
)
summarize_payload = adapter.summarize_payload


def _sync_adapter_globals() -> None:
    adapter.PROJECT_ROOT = PROJECT_ROOT
    adapter.DEFAULT_ASSET_HUB_JOB_ASSETS_DIR = DEFAULT_ASSET_HUB_JOB_ASSETS_DIR


def validate_job_spec(
    spec: dict[str, Any],
    local_videos_dir: str | Path = DEFAULT_LOCAL_VIDEOS_DIR,
    min_width: int = 480,
    min_height: int = 480,
    skip_media_probe: bool = False,
) -> list[dict[str, Any]]:
    _sync_adapter_globals()
    return adapter.validate_job_spec(
        spec,
        local_videos_dir=local_videos_dir,
        min_width=min_width,
        min_height=min_height,
        skip_media_probe=skip_media_probe,
    )


def build_pending_job(
    spec: dict[str, Any],
    ordered_assets: list[dict[str, Any]],
    fonts_dir: str | Path = DEFAULT_FONTS_DIR,
    local_audios_dir: str | Path = DEFAULT_LOCAL_AUDIOS_DIR,
    local_subtitles_dir: str | Path = DEFAULT_LOCAL_SUBTITLES_DIR,
) -> dict[str, Any]:
    _sync_adapter_globals()
    return adapter.build_pending_job(
        spec,
        ordered_assets,
        fonts_dir=fonts_dir,
        local_audios_dir=local_audios_dir,
        local_subtitles_dir=local_subtitles_dir,
    )


def build_moneyprinter_payload(
    spec: dict[str, Any],
    *,
    local_videos_dir: str | Path = DEFAULT_LOCAL_VIDEOS_DIR,
    local_audios_dir: str | Path = DEFAULT_LOCAL_AUDIOS_DIR,
    local_subtitles_dir: str | Path = DEFAULT_LOCAL_SUBTITLES_DIR,
    min_width: int = 480,
    min_height: int = 480,
    media_probe: bool = True,
    fonts_dir: str | Path = DEFAULT_FONTS_DIR,
) -> dict[str, Any]:
    _sync_adapter_globals()
    return adapter.build_moneyprinter_payload(
        spec,
        local_videos_dir=local_videos_dir,
        local_audios_dir=local_audios_dir,
        local_subtitles_dir=local_subtitles_dir,
        min_width=min_width,
        min_height=min_height,
        media_probe=media_probe,
        fonts_dir=fonts_dir,
    )


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
    parser.add_argument("--local-audios-dir", default=DEFAULT_LOCAL_AUDIOS_DIR)
    parser.add_argument("--local-subtitles-dir", default=DEFAULT_LOCAL_SUBTITLES_DIR)
    parser.add_argument("--fonts-dir", default=DEFAULT_FONTS_DIR)
    parser.add_argument("--min-width", default=480, type=positive_int)
    parser.add_argument("--min-height", default=480, type=positive_int)
    parser.add_argument("--skip-media-probe", action="store_true")
    return parser


def _infer_sibling_media_dir(
    *,
    configured_dir: str,
    default_dir: str,
    local_videos_dir: str,
    sibling_name: str,
) -> str:
    if configured_dir != default_dir or local_videos_dir == DEFAULT_LOCAL_VIDEOS_DIR:
        return configured_dir
    sibling = Path(local_videos_dir).resolve().parent / sibling_name
    if sibling.exists():
        return str(sibling)
    return configured_dir


def _build_payload_from_args(args: argparse.Namespace) -> dict[str, Any]:
    local_audios_dir = _infer_sibling_media_dir(
        configured_dir=args.local_audios_dir,
        default_dir=DEFAULT_LOCAL_AUDIOS_DIR,
        local_videos_dir=args.local_videos_dir,
        sibling_name="local_audios",
    )
    local_subtitles_dir = _infer_sibling_media_dir(
        configured_dir=args.local_subtitles_dir,
        default_dir=DEFAULT_LOCAL_SUBTITLES_DIR,
        local_videos_dir=args.local_videos_dir,
        sibling_name="local_subtitles",
    )
    spec = load_kurukin_job_spec(args.job_spec)
    return build_moneyprinter_payload(
        spec,
        local_videos_dir=args.local_videos_dir,
        local_audios_dir=local_audios_dir,
        local_subtitles_dir=local_subtitles_dir,
        min_width=args.min_width,
        min_height=args.min_height,
        media_probe=not args.skip_media_probe,
        fonts_dir=args.fonts_dir,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        pending_job = _build_payload_from_args(args)

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
