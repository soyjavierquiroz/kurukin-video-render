#!/usr/bin/env python3
"""Kurukin Nightly Runner.

File-based nightly queue runner for MoneyPrinterTurbo.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import sys
import time
import traceback
from typing import Any
from urllib import error, request


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.custom.kurukin_job_queue import is_aroll_broll_renderer_enabled

CONTAINER_QUEUE_DIR = Path("/MoneyPrinterTurbo/storage/nightly_jobs")
DEFAULT_API_BASE_URL = "http://127.0.0.1:18080/api/v1"
METADATA_KEYS = {"job_id", "notes", "description", "runner"}
RENDER_MODE_AROLL_BROLL = "aroll_broll"
COMPLETE_STATE = 1
FAILED_STATE = -1
PROCESSING_STATE = 4
DEFAULT_NO_PROGRESS_TIMEOUT_SECONDS = 1800


def default_queue_dir(
    *,
    project_root: str | Path | None = None,
    container_queue: str | Path = CONTAINER_QUEUE_DIR,
) -> Path:
    """Return the default queue path for container or host execution."""

    container_path = Path(container_queue)
    if container_path.exists():
        return container_path

    root = Path(project_root) if project_root is not None else PROJECT_ROOT
    return root / "storage" / "nightly_jobs"


DEFAULT_QUEUE_DIR = default_queue_dir().as_posix()


class RunnerError(Exception):
    """Expected runner failure that should move the current job to failed."""


class JsonApiError(RunnerError):
    """HTTP/API error with captured response details."""

    def __init__(self, message: str, status_code: int | None = None, body: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class NoProgressTimeoutError(RunnerError):
    """Task polling observed no state/progress changes for too long."""

    def __init__(
        self,
        task_id: str,
        last_state: int | None,
        last_progress: int | float | None,
        no_progress_timeout_seconds: int,
    ):
        super().__init__(
            "task polling made no progress "
            f"task_id={task_id} "
            f"last_state={last_state} "
            f"last_progress={last_progress} "
            f"no_progress_timeout_seconds={no_progress_timeout_seconds}"
        )
        self.task_id = task_id
        self.last_state = last_state
        self.last_progress = last_progress
        self.no_progress_timeout_seconds = no_progress_timeout_seconds


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def timestamp() -> str:
    return utc_now().strftime("%Y%m%dT%H%M%SZ")


def write_json(path: Path, data: Any) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False, sort_keys=True)
        handle.write("\n")


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def ensure_queue_dirs(queue_dir: Path) -> dict[str, Path]:
    paths = {
        "root": queue_dir,
        "pending": queue_dir / "pending",
        "processing": queue_dir / "processing",
        "completed": queue_dir / "completed",
        "failed": queue_dir / "failed",
        "logs": queue_dir / "logs",
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return paths


def parse_hhmm(value: str) -> dt.time:
    try:
        parsed = dt.datetime.strptime(value, "%H:%M")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"{value!r} must use HH:MM 24-hour format"
        ) from exc
    return parsed.time()


def is_in_window(now: dt.datetime, start: dt.time, end: dt.time) -> bool:
    current = now.time()
    if start == end:
        return True
    if start < end:
        return start <= current < end
    return current >= start or current < end


def acquire_lock(queue_dir: Path) -> Path:
    lock_path = queue_dir / "nightly_runner.lock"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(lock_path, flags, 0o644)
    except FileExistsError as exc:
        raise RunnerError(f"Runner lock already exists: {lock_path}") from exc

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"pid={os.getpid()}\nstarted_at={timestamp()}\n")
            handle.flush()
    except Exception:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass
        raise

    return lock_path


def release_lock(lock_path: Path, logger: "Logger") -> None:
    try:
        lock_path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.log(f"failed to remove runner lock {lock_path}: {exc}")


def sanitize_name(name: str) -> str:
    safe = []
    for char in name:
        if char.isalnum() or char in ("-", "_", "."):
            safe.append(char)
        else:
            safe.append("-")
    return "".join(safe).strip(".-") or "job"


def reserve_job(pending_file: Path, processing_dir: Path) -> Path:
    unique_name = (
        f"{sanitize_name(pending_file.stem)}-{timestamp()}-{os.getpid()}-"
        f"{time.time_ns()}"
    )
    run_dir = processing_dir / unique_name
    run_dir.mkdir(parents=False, exist_ok=False)
    shutil.move(str(pending_file), str(run_dir / "job.json"))
    return run_dir


def move_run_dir(run_dir: Path, destination_root: Path) -> Path:
    destination = destination_root / run_dir.name
    if destination.exists():
        destination = destination_root / f"{run_dir.name}-{time.time_ns()}"
    shutil.move(str(run_dir), str(destination))
    return destination


def validate_job(job: Any) -> dict[str, Any]:
    if not isinstance(job, dict):
        raise RunnerError("job must be a JSON object")

    render_mode = job.get("render_mode")
    if render_mode == RENDER_MODE_AROLL_BROLL:
        if not is_aroll_broll_renderer_enabled():
            raise RunnerError("A-roll/B-roll renderer execution is disabled")
        raise RunnerError(
            "A-roll/B-roll renderer execution is enabled, but no runner handler "
            "is wired for this phase"
        )
    if render_mode not in (None, ""):
        raise RunnerError(f"unsupported render_mode: {render_mode}")

    subject = job.get("video_subject")
    if not isinstance(subject, str) or not subject.strip():
        raise RunnerError("video_subject is required and must be a non-empty string")

    video_aspect = job.get("video_aspect")
    if video_aspect not in {"9:16", "16:9"}:
        raise RunnerError('video_aspect must be "9:16" or "16:9"')

    video_source = job.get("video_source")
    if video_source == "local":
        materials = job.get("video_materials")
        asset_hub_manifest_path = job.get("asset_hub_renderer_manifest_path")
        has_asset_hub_manifest = (
            isinstance(asset_hub_manifest_path, str)
            and bool(asset_hub_manifest_path.strip())
        )
        if (
            (not isinstance(materials, list) or not materials)
            and not has_asset_hub_manifest
        ):
            raise RunnerError(
                'video_materials is required and cannot be empty when video_source is "local"'
            )

    return {key: value for key, value in job.items() if key not in METADATA_KEYS}


def api_json(method: str, url: str, payload: dict[str, Any] | None = None) -> Any:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    api_request = request.Request(url, data=data, headers=headers, method=method)
    try:
        with request.urlopen(api_request, timeout=60) as response:
            body = response.read().decode("utf-8")
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise JsonApiError(
            f"{method} {url} returned HTTP {exc.code}",
            status_code=exc.code,
            body=body,
        ) from exc
    except error.URLError as exc:
        raise JsonApiError(f"{method} {url} failed: {exc}") from exc

    if not body:
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise JsonApiError(f"{method} {url} returned invalid JSON", body=body) from exc


def extract_task_id(response_body: Any) -> str:
    if not isinstance(response_body, dict):
        raise RunnerError("submit response must be a JSON object")
    data = response_body.get("data")
    if not isinstance(data, dict):
        raise RunnerError("submit response is missing data object")
    task_id = data.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        raise RunnerError("submit response is missing data.task_id")
    return task_id


def extract_task_state(task_response: Any) -> int | None:
    if not isinstance(task_response, dict):
        return None
    data = task_response.get("data")
    if isinstance(data, dict):
        state = data.get("state")
    else:
        state = task_response.get("state")
    return state if isinstance(state, int) else None


def extract_task_progress(task_response: Any) -> int | float | None:
    if not isinstance(task_response, dict):
        return None
    data = task_response.get("data")
    if isinstance(data, dict):
        progress = data.get("progress")
    else:
        progress = task_response.get("progress")
    if isinstance(progress, bool):
        return None
    return progress if isinstance(progress, (int, float)) else None


def api_error_payload(exc: BaseException) -> dict[str, Any]:
    payload = {
        "error": str(exc),
        "type": exc.__class__.__name__,
        "timestamp": timestamp(),
    }
    if isinstance(exc, JsonApiError):
        payload["status_code"] = exc.status_code
        payload["body"] = exc.body
    elif isinstance(exc, NoProgressTimeoutError):
        payload["task_id"] = exc.task_id
        payload["last_state"] = exc.last_state
        payload["last_progress"] = exc.last_progress
        payload["no_progress_timeout_seconds"] = exc.no_progress_timeout_seconds
        payload["traceback"] = traceback.format_exc()
    else:
        payload["traceback"] = traceback.format_exc()
    return payload


class Logger:
    def __init__(self, log_path: Path):
        self.log_path = log_path

    def log(self, message: str) -> None:
        line = f"{timestamp()} {message}"
        print(line, flush=True)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def run_dry_job(run_dir: Path, payload: dict[str, Any], logger: Logger) -> Path:
    write_json(
        run_dir / "submit-response.json",
        {
            "dry_run": True,
            "message": "No request sent to MoneyPrinterTurbo.",
            "data": {"task_id": f"dry-run-{run_dir.name}"},
        },
    )
    write_json(
        run_dir / "final-task.json",
        {
            "dry_run": True,
            "state": COMPLETE_STATE,
            "payload_keys": sorted(payload.keys()),
            "completed_at": timestamp(),
        },
    )
    logger.log(f"dry-run completed {run_dir.name}")
    return run_dir


def submit_and_wait(
    run_dir: Path,
    payload: dict[str, Any],
    api_base_url: str,
    poll_seconds: int,
    task_timeout_seconds: int,
    no_progress_timeout_seconds: int,
    logger: Logger,
) -> None:
    submit_url = f"{api_base_url.rstrip('/')}/videos"
    response_body = api_json("POST", submit_url, payload)
    write_json(run_dir / "submit-response.json", response_body)
    task_id = extract_task_id(response_body)
    logger.log(f"submitted {run_dir.name} task_id={task_id}")

    task_url = f"{api_base_url.rstrip('/')}/tasks/{task_id}"
    deadline = time.monotonic() + task_timeout_seconds
    last_state: int | None = None
    last_progress: int | float | None = None
    no_progress_since = time.monotonic()
    has_progress_observation = False

    while True:
        now = time.monotonic()
        if now > deadline:
            raise RunnerError(
                f"task {task_id} timed out after {task_timeout_seconds} seconds"
            )

        task_response = api_json("GET", task_url)
        state = extract_task_state(task_response)
        progress = extract_task_progress(task_response)
        logger.log(f"polled {task_id} state={state} progress={progress}")

        if (
            not has_progress_observation
            or state != last_state
            or progress != last_progress
        ):
            last_state = state
            last_progress = progress
            no_progress_since = time.monotonic()
            has_progress_observation = True
        elif time.monotonic() - no_progress_since > no_progress_timeout_seconds:
            raise NoProgressTimeoutError(
                task_id=task_id,
                last_state=last_state,
                last_progress=last_progress,
                no_progress_timeout_seconds=no_progress_timeout_seconds,
            )

        if state == COMPLETE_STATE:
            write_json(run_dir / "final-task.json", task_response)
            return
        if state == FAILED_STATE:
            write_json(run_dir / "final-task.json", task_response)
            raise RunnerError(f"task {task_id} failed")
        if state != PROCESSING_STATE:
            logger.log(f"task {task_id} has unexpected state={state}; polling again")

        time.sleep(poll_seconds)


def process_one_job(
    pending_file: Path,
    paths: dict[str, Path],
    args: argparse.Namespace,
    logger: Logger,
) -> Path:
    run_dir = reserve_job(pending_file, paths["processing"])
    logger.log(f"reserved {pending_file.name} as {run_dir.name}")

    try:
        job = read_json(run_dir / "job.json")
        payload = validate_job(job)
        write_json(run_dir / "moneyprinter-payload.json", payload)

        if args.dry_run:
            run_dry_job(run_dir, payload, logger)
        else:
            submit_and_wait(
                run_dir,
                payload,
                args.api_base_url,
                args.poll_seconds,
                args.task_timeout_seconds,
                args.no_progress_timeout_seconds,
                logger,
            )

        completed_path = move_run_dir(run_dir, paths["completed"])
        logger.log(f"moved {run_dir.name} to completed")
        return completed_path
    except Exception as exc:
        write_json(run_dir / "error.json", api_error_payload(exc))
        failed_path = move_run_dir(run_dir, paths["failed"])
        logger.log(f"moved {run_dir.name} to failed: {exc}")
        return failed_path


def pending_jobs(pending_dir: Path) -> list[Path]:
    return sorted(path for path in pending_dir.iterdir() if path.is_file())


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{value!r} must be an integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError(f"{value!r} must be >= 1")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Kurukin Nightly Runner")
    parser.add_argument("--queue-dir", default=default_queue_dir().as_posix())
    parser.add_argument("--api-base-url", default=DEFAULT_API_BASE_URL)
    parser.add_argument("--window-start", default="00:00", type=parse_hhmm)
    parser.add_argument("--window-end", default="07:00", type=parse_hhmm)
    parser.add_argument("--ignore-window", action="store_true")
    parser.add_argument("--max-jobs", default=10, type=positive_int)
    parser.add_argument("--poll-seconds", default=20, type=positive_int)
    parser.add_argument("--task-timeout-seconds", default=14400, type=positive_int)
    parser.add_argument(
        "--no-progress-timeout-seconds",
        default=DEFAULT_NO_PROGRESS_TIMEOUT_SECONDS,
        type=positive_int,
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    queue_dir = Path(args.queue_dir)
    paths = ensure_queue_dirs(queue_dir)
    logger = Logger(paths["logs"] / f"nightly-runner-{timestamp()}.log")

    try:
        lock_path = acquire_lock(queue_dir)
    except RunnerError as exc:
        logger.log(str(exc))
        return 2

    try:
        logger.log(
            "starting Kurukin Nightly Runner "
            f"dry_run={args.dry_run} max_jobs={args.max_jobs}"
        )
        if args.ignore_window:
            logger.log("manual window override enabled")
        jobs_started = 0
        while jobs_started < args.max_jobs:
            if not args.ignore_window and not is_in_window(
                dt.datetime.now().astimezone(), args.window_start, args.window_end
            ):
                logger.log(
                    "outside nightly window; no new jobs will be started "
                    f"window_start={args.window_start.strftime('%H:%M')} "
                    f"window_end={args.window_end.strftime('%H:%M')}"
                )
                break

            jobs = pending_jobs(paths["pending"])
            if not jobs:
                logger.log("no pending jobs")
                break

            process_one_job(jobs[0], paths, args, logger)
            jobs_started += 1

        logger.log(f"finished jobs_started={jobs_started}")
    except KeyboardInterrupt:
        logger.log("interrupted; exiting")
        return 130
    finally:
        release_lock(lock_path, logger)
    return 0


if __name__ == "__main__":
    sys.exit(main())
