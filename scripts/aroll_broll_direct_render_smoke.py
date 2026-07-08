#!/usr/bin/env python3
"""Dry-run smoke helper for direct A-roll/B-roll render planning."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.custom.aroll_broll_renderer import (  # noqa: E402
    ArollBrollRenderPlan,
    ArollBrollRendererError,
    build_alternating_fullscreen_timeline,
    build_aroll_broll_output_path,
    get_media_duration_seconds,
    run_aroll_broll_render,
    validate_aroll_path,
    validate_broll_path,
)
from app.custom.aroll_broll_mode import (  # noqa: E402
    MAX_BROLL_ASSETS,
    normalize_broll_asset_values,
)


DIRECT_RENDER_ENV = "KURUKIN_ENABLE_AROLL_BROLL_DIRECT_RENDER"
DEFAULT_SMOKE_DURATION_SECONDS = 6.0
DEFAULT_CLIP_SECONDS = 4
DEFAULT_FREQUENCY = "medium"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a guarded A-roll/B-roll direct render smoke plan."
    )
    parser.add_argument("--a-roll", required=True, help="A-roll video path.")
    parser.add_argument(
        "--b-roll",
        required=True,
        action="append",
        help="B-roll media path. May be passed more than once.",
    )
    parser.add_argument("--task-id", required=True, help="Task id for output path.")
    parser.add_argument(
        "--project-root",
        default=PROJECT_ROOT.as_posix(),
        help="Project root used for path validation and output planning.",
    )
    parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=True,
        help="Build and print the render plan without executing ffmpeg.",
    )
    parser.add_argument(
        "--execute",
        dest="dry_run",
        action="store_false",
        help="Execute ffmpeg only when the direct render env flag is enabled.",
    )
    parser.add_argument(
        "--a-roll-duration-seconds",
        type=float,
        default=None,
        help=(
            "Known A-roll duration for planning. Dry-run defaults to the smoke "
            "fixture duration; execute probes the A-roll when omitted."
        ),
    )
    return parser


def _is_direct_render_enabled(env: dict[str, str] | None = None) -> bool:
    value = (env or os.environ).get(DIRECT_RENDER_ENV, "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _plan_duration_seconds(
    args: argparse.Namespace,
    a_roll_path: Path,
    *,
    duration_runner=None,
) -> tuple[float, list[str]]:
    if args.a_roll_duration_seconds is not None:
        if args.a_roll_duration_seconds <= 0:
            raise ArollBrollRendererError(
                "A-roll duration must be greater than zero"
            )
        return float(args.a_roll_duration_seconds), []

    if args.dry_run:
        return DEFAULT_SMOKE_DURATION_SECONDS, [
            "dry-run smoke uses a synthetic duration and does not probe media files"
        ]

    return get_media_duration_seconds(a_roll_path, runner=duration_runner), []


def build_smoke_plan(
    args: argparse.Namespace,
    *,
    duration_runner=None,
) -> tuple[ArollBrollRenderPlan, list[str]]:
    project_root = Path(args.project_root).resolve(strict=False)
    a_roll_path = validate_aroll_path(args.a_roll, project_root=project_root)
    b_roll_paths = normalize_broll_asset_values(args.b_roll)
    if not b_roll_paths:
        raise ArollBrollRendererError("at least one B-roll asset is required")
    if len(b_roll_paths) > MAX_BROLL_ASSETS:
        raise ArollBrollRendererError(
            f"B-roll supports at most {MAX_BROLL_ASSETS} assets"
        )
    b_roll_assets = [
        validate_broll_path(path, project_root=project_root)
        for path in b_roll_paths
    ]
    output_path = build_aroll_broll_output_path(args.task_id, project_root=project_root)
    aroll_duration_seconds, warnings = _plan_duration_seconds(
        args,
        a_roll_path,
        duration_runner=duration_runner,
    )
    timeline = build_alternating_fullscreen_timeline(
        aroll_duration_seconds,
        b_roll_assets,
        DEFAULT_CLIP_SECONDS,
        DEFAULT_FREQUENCY,
    )
    plan = ArollBrollRenderPlan(
        a_roll_path=a_roll_path,
        b_roll_assets=b_roll_assets,
        output_path=output_path,
        timeline=timeline,
        aroll_duration_seconds=aroll_duration_seconds,
    )
    return plan, warnings


def run_smoke(
    args: argparse.Namespace,
    *,
    env: dict[str, str] | None = None,
    runner=None,
    duration_runner=None,
) -> dict[str, Any]:
    if not args.dry_run and not _is_direct_render_enabled(env):
        raise ArollBrollRendererError(
            "Direct A-roll/B-roll render execution is disabled"
        )

    plan, warnings = build_smoke_plan(args, duration_runner=duration_runner)
    result = run_aroll_broll_render(plan, runner=runner, dry_run=args.dry_run)
    result["warnings"] = [*warnings, *result.get("warnings", [])]
    timeline_duration = max((float(item["end"]) for item in plan.timeline), default=0.0)
    return {
        "ok": bool(result.get("ok")),
        "dry_run": bool(result.get("dry_run")),
        "a_roll_path": plan.a_roll_path.as_posix(),
        "a_roll_duration_seconds": plan.aroll_duration_seconds,
        "b_roll_count": len(plan.b_roll_assets),
        "b_roll_asset_count": len(plan.b_roll_assets),
        "b_roll_assets": [asset.path.as_posix() for asset in plan.b_roll_assets],
        "timeline": plan.timeline,
        "timeline_duration_seconds": timeline_duration,
        "output_path": result["output_path"],
        "command": result["command"],
        "returncode": result.get("returncode"),
        "stdout": result.get("stdout", ""),
        "stderr": result.get("stderr", ""),
        "warnings": result["warnings"],
    }


def main(argv: list[str] | None = None, *, runner=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = run_smoke(args, runner=runner)
    except ArollBrollRendererError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
