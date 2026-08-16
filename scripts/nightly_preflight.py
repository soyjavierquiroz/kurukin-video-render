#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_QUEUE_DIR = PROJECT_ROOT / "storage" / "nightly_jobs"

HUMAN_REVIEW_RENDER_MODE = "human_review_batch"

DEFAULT_MIN_FREE_GB = 5.0

HYPERFRAMES_ROOT = Path(
    os.environ.get(
        "KURUKIN_HYPERFRAMES_ROOT",
        "/opt/apps/hyperframes",
    )
)


class PreflightError(RuntimeError):
    pass


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8")
        )
    except Exception as exc:
        raise PreflightError(
            f"invalid JSON: {path}: {exc}"
        ) from exc

    if not isinstance(value, dict):
        raise PreflightError(
            f"JSON root must be an object: {path}"
        )

    return value


def write_json(
    path: Path,
    payload: dict[str, Any],
) -> None:
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temporary = path.with_suffix(
        path.suffix + ".tmp"
    )

    temporary.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    os.replace(
        temporary,
        path,
    )


def project_path(value: str | Path) -> Path:
    raw = str(value or "").strip()

    if not raw:
        return Path()

    if raw == "/MoneyPrinterTurbo":
        return PROJECT_ROOT

    prefix = "/MoneyPrinterTurbo/"

    if raw.startswith(prefix):
        return (
            PROJECT_ROOT
            / raw[len(prefix):]
        )

    path = Path(raw)

    if path.is_absolute():
        return path

    return PROJECT_ROOT / path


def require_file(
    path: Path,
    label: str,
) -> None:
    if not path.is_file():
        raise PreflightError(
            f"{label} missing: {path}"
        )

    if path.stat().st_size <= 0:
        raise PreflightError(
            f"{label} is empty: {path}"
        )


def require_binary(name: str) -> str:
    found = shutil.which(name)

    if not found:
        raise PreflightError(
            f"required binary not found: {name}"
        )

    return found


def safe_name(value: str) -> str:
    value = re.sub(
        r"[^A-Za-z0-9._-]+",
        "-",
        str(value or "").strip(),
    )

    value = value.strip("-._")

    return value[:120] or "job"


def free_space_gb(path: Path) -> float:
    usage = shutil.disk_usage(path)

    return usage.free / (
        1024 * 1024 * 1024
    )


def check_runtime_dependencies(
    *,
    min_free_gb: float,
) -> dict[str, Any]:
    ffmpeg = require_binary("ffmpeg")
    ffprobe = require_binary("ffprobe")
    node = require_binary("node")

    hyperframes_entry = (
        HYPERFRAMES_ROOT
        / "scripts"
        / "render-job.mjs"
    )

    require_file(
        hyperframes_entry,
        "HyperFrames renderer",
    )

    model_root = (
        PROJECT_ROOT
        / "models"
        / "hf-cache"
    )

    if not model_root.is_dir():
        raise PreflightError(
            f"Whisper model cache missing: "
            f"{model_root}"
        )

    try:
        has_model_files = next(
            (
                path
                for path in model_root.rglob("*")
                if path.is_file()
                and path.stat().st_size > 0
            ),
            None,
        )
    except OSError as exc:
        raise PreflightError(
            f"cannot inspect Whisper model cache: "
            f"{exc}"
        ) from exc

    if has_model_files is None:
        raise PreflightError(
            "Whisper model cache is empty"
        )

    free_gb = free_space_gb(
        PROJECT_ROOT
    )

    if free_gb < min_free_gb:
        raise PreflightError(
            f"insufficient disk space: "
            f"{free_gb:.2f} GB free, "
            f"{min_free_gb:.2f} GB required"
        )

    return {
        "ffmpeg": ffmpeg,
        "ffprobe": ffprobe,
        "node": node,
        "hyperframes": str(
            hyperframes_entry
        ),
        "whisper_cache": str(
            model_root
        ),
        "free_gb": round(
            free_gb,
            2,
        ),
    }


def _plan_script_available(
    plan: dict[str, Any],
) -> bool:
    script_text = str(
        plan.get("script_text")
        or ""
    ).strip()

    if script_text:
        return True

    script_path = project_path(
        str(
            plan.get("script_path")
            or ""
        )
    )

    return bool(
        script_path
        and script_path.is_file()
        and script_path.stat().st_size > 0
    )


def validate_human_review_job(
    job: dict[str, Any],
    *,
    materialize: bool,
) -> dict[str, Any]:
    from app.custom import human_review

    task_id = str(
        job.get("task_id")
        or ""
    ).strip()

    if not task_id:
        raise PreflightError(
            "human review job has no task_id"
        )

    plan_value = str(
        job.get("production_plan_path")
        or ""
    ).strip()

    if not plan_value:
        raise PreflightError(
            "human review job has no "
            "production_plan_path"
        )

    plan_path = project_path(
        plan_value
    )

    require_file(
        plan_path,
        "production plan",
    )

    plan = read_json(
        plan_path
    )

    status = str(
        plan.get("review_status")
        or ""
    )

    if status != "approved":
        raise PreflightError(
            "production plan is not approved: "
            f"{status!r}"
        )

    plan_task_id = str(
        plan.get("task_id")
        or ""
    )

    if (
        plan_task_id
        and plan_task_id != task_id
    ):
        raise PreflightError(
            "job task_id does not match "
            "production plan task_id"
        )

    errors, warnings = (
        human_review.validate_plan_for_approval(
            plan
        )
    )

    if errors:
        raise PreflightError(
            "production plan validation failed: "
            + "; ".join(
                str(error)
                for error in errors
            )
        )

    timeline = (
        human_review.render_timeline_from_plan(
            plan
        )
    )

    if float(
        timeline.shortfall
    ) > 0.01:
        raise PreflightError(
            "render timeline has shortfall: "
            f"{timeline.shortfall:.3f}s"
        )

    if not timeline.pieces:
        raise PreflightError(
            "render timeline has no pieces"
        )

    audio_path = project_path(
        str(
            plan.get("audio_path")
            or ""
        )
    )

    require_file(
        audio_path,
        "audio input",
    )

    if not _plan_script_available(
        plan
    ):
        raise PreflightError(
            "canonical script unavailable"
        )

    selection = (
        human_review.selection_result_from_plan(
            plan
        )
    )

    decisions = list(
        getattr(
            selection,
            "decisions",
            (),
        )
        or ()
    )

    if not decisions:
        raise PreflightError(
            "approved plan selected no materials"
        )

    materialization = {
        "checked": False,
        "count": 0,
    }

    if materialize:
        from app.custom.material_acquisition import (
            acquire_selected_materials,
        )

        preflight_task_id = safe_name(
            f"preflight-{task_id}"
        )

        preflight_task_dir = (
            PROJECT_ROOT
            / "storage"
            / "tasks"
            / preflight_task_id
        )

        shutil.rmtree(
            preflight_task_dir,
            ignore_errors=True,
        )

        try:
            result = acquire_selected_materials(
                selection_result=selection,
                task_id=preflight_task_id,
            )

            materials = list(
                result.materials
            )

            if (
                len(materials)
                != len(decisions)
            ):
                raise PreflightError(
                    "materialization count mismatch: "
                    f"{len(materials)} materials for "
                    f"{len(decisions)} decisions"
                )

            for info in materials:
                material_path = Path(
                    str(info.url)
                )

                require_file(
                    material_path,
                    "materialized asset",
                )

            materialization = {
                "checked": True,
                "count": len(
                    materials
                ),
            }

        finally:
            # Only task-local preflight artifacts are deleted.
            # Asset Hub source media is never removed here.
            shutil.rmtree(
                preflight_task_dir,
                ignore_errors=True,
            )

    return {
        "task_id": task_id,
        "production_plan_path": str(
            plan_path
        ),
        "coverage": 1.0,
        "timeline_duration": float(
            timeline.total_output_duration
        ),
        "timeline_shortfall": float(
            timeline.shortfall
        ),
        "selected_materials": len(
            decisions
        ),
        "warnings": [
            str(warning)
            for warning in (
                warnings or []
            )
        ],
        "materialization": materialization,
    }


def preflight_job_file(
    job_file: str | Path,
    *,
    materialize: bool = False,
    min_free_gb: float = (
        DEFAULT_MIN_FREE_GB
    ),
) -> dict[str, Any]:
    path = Path(job_file)

    require_file(
        path,
        "nightly job",
    )

    job = read_json(
        path
    )

    runtime = check_runtime_dependencies(
        min_free_gb=min_free_gb,
    )

    render_mode = str(
        job.get("render_mode")
        or ""
    ).strip()

    report: dict[str, Any] = {
        "ok": True,
        "job_file": str(path),
        "render_mode": render_mode,
        "runtime": runtime,
    }

    if (
        render_mode
        == HUMAN_REVIEW_RENDER_MODE
    ):
        report["human_review"] = (
            validate_human_review_job(
                job,
                materialize=materialize,
            )
        )
    else:
        # Generic jobs still receive runtime/disk checks.
        # Existing nightly_runner.validate_job remains the
        # authoritative payload validator for those modes.
        report["generic"] = {
            "checked": True,
            "materialization": False,
        }

    return report


def quarantine_job(
    pending_file: str | Path,
    *,
    report: dict[str, Any],
    queue_dir: str | Path | None = None,
) -> Path:
    pending = Path(
        pending_file
    )

    root = (
        Path(queue_dir)
        if queue_dir is not None
        else pending.parent.parent
    )

    blocked_root = (
        root
        / "blocked"
    )

    blocked_root.mkdir(
        parents=True,
        exist_ok=True,
    )

    timestamp = (
        datetime.now(timezone.utc)
        .strftime("%Y%m%dT%H%M%SZ")
    )

    destination = (
        blocked_root
        / (
            safe_name(
                pending.stem
            )
            + "-"
            + timestamp
        )
    )

    counter = 1
    original = destination

    while destination.exists():
        destination = Path(
            str(original)
            + f"-{counter}"
        )
        counter += 1

    destination.mkdir(
        parents=True,
        exist_ok=False,
    )

    if pending.is_file():
        shutil.move(
            str(pending),
            str(
                destination
                / "job.json"
            ),
        )

    write_json(
        destination
        / "preflight.json",
        report,
    )

    return destination


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate Kurukin nightly jobs before "
            "the unattended render window."
        )
    )

    parser.add_argument(
        "--queue-dir",
        default=str(
            DEFAULT_QUEUE_DIR
        ),
    )

    parser.add_argument(
        "--materialize",
        action="store_true",
        help=(
            "Materialize approved assets to verify "
            "they are actually retrievable."
        ),
    )

    parser.add_argument(
        "--quarantine",
        action="store_true",
        help=(
            "Move failed pending jobs to blocked/."
        ),
    )

    parser.add_argument(
        "--min-free-gb",
        type=float,
        default=DEFAULT_MIN_FREE_GB,
    )

    return parser


def main() -> int:
    args = build_parser().parse_args()

    queue_dir = Path(
        args.queue_dir
    )

    pending_dir = (
        queue_dir
        / "pending"
    )

    pending_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    jobs = sorted(
        path
        for path in pending_dir.iterdir()
        if path.is_file()
        and path.suffix.lower() == ".json"
    )

    print(
        "Kurukin Nightly Preflight"
    )
    print(
        f"jobs: {len(jobs)}"
    )
    print(
        "materialize:",
        bool(args.materialize),
    )
    print()

    passed = 0
    blocked = 0

    for index, job_file in enumerate(
        jobs,
        1,
    ):
        print(
            f"[{index}/{len(jobs)}] "
            f"{job_file.name}"
        )

        try:
            report = preflight_job_file(
                job_file,
                materialize=bool(
                    args.materialize
                ),
                min_free_gb=float(
                    args.min_free_gb
                ),
            )
        except Exception as exc:
            blocked += 1

            failure_report = {
                "ok": False,
                "job_file": str(
                    job_file
                ),
                "error_type": (
                    type(exc).__name__
                ),
                "error": str(exc),
                "materialize": bool(
                    args.materialize
                ),
                "checked_at": (
                    datetime.now(
                        timezone.utc
                    ).isoformat()
                ),
            }

            print(
                "  BLOCK",
                str(exc),
            )

            if args.quarantine:
                destination = (
                    quarantine_job(
                        job_file,
                        report=failure_report,
                        queue_dir=queue_dir,
                    )
                )

                print(
                    "  ->",
                    destination,
                )

            continue

        passed += 1

        human = report.get(
            "human_review"
        )

        if isinstance(
            human,
            dict,
        ):
            materialization = (
                human.get(
                    "materialization"
                )
                or {}
            )

            print(
                "  PASS",
                "materials=",
                human.get(
                    "selected_materials"
                ),
                "materialized=",
                materialization.get(
                    "count",
                    0,
                ),
                "timeline=",
                f"{human.get('timeline_duration', 0):.3f}s",
            )
        else:
            print(
                "  PASS"
            )

    print()
    print(
        "PREFLIGHT SUMMARY"
    )
    print(
        f"PASS:    {passed}"
    )
    print(
        f"BLOCKED: {blocked}"
    )

    if blocked:
        print()
        print(
            "NIGHT QUEUE HAS BLOCKED JOBS"
        )
        return 2

    print()
    print(
        "READY FOR NIGHT RUNNER"
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
