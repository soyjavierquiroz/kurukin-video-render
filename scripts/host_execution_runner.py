#!/usr/bin/env python3
"""Conservative, one-shot host dispatcher for existing NOW and NIGHT signals.

This intentionally owns no queue.  It only observes the existing content-job
schedule records and the nightly pending directory, leaving recovery of stale
producers for a later explicit policy.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts import content_delivery, nightly_runner, review_preparation


NOW_STATES = {"launching", "producing", "completed", "error"}
LOG = logging.getLogger(__name__)


def current_boot_id() -> str:
    """Return the Linux boot identity used to distinguish recycled PIDs."""
    return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()


def pid_is_alive(pid: object) -> bool:
    """Return whether *pid* currently names a process we may observe."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def producing_record_is_live(record: dict[str, Any]) -> bool:
    """A producing record is live only on this boot with a live producer PID."""
    return record.get("boot_id") == current_boot_id() and pid_is_alive(record.get("pid"))


def _read_record(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("schedule record must be a JSON object")
    return value


def _write_record(handle: Any, record: dict[str, Any]) -> None:
    handle.seek(0)
    handle.truncate()
    json.dump(record, handle, ensure_ascii=False, sort_keys=True)
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())


def _host_plan_path(value: object, project_root: Path) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("missing production_plan_path")
    path = Path(value)
    if path.is_absolute():
        try:
            return project_root / path.relative_to("/MoneyPrinterTurbo")
        except ValueError:
            pass
    return path


def now_command(plan_path: Path) -> list[str]:
    return ["python3", "scripts/produce_batch.py", "--production", "--approved-plan", plan_path.as_posix()]


def nightly_command(project_root: Path) -> list[str]:
    return ["python3", "scripts/nightly_runner.py", "--queue-dir", (project_root / "storage" / "nightly_jobs").as_posix()]


def _stale_reason(record: dict[str, Any]) -> str:
    if record.get("boot_id") != current_boot_id():
        return "boot_changed"
    return "pid_dead"


def _review_max_concurrency() -> int:
    """Bound work per timer invocation; parallelism can be raised explicitly."""
    try:
        return max(1, int(os.environ.get("KURUKIN_REVIEW_MAX_CONCURRENCY", "1")))
    except ValueError:
        return 1


def _review_record_is_live(record: dict[str, Any]) -> bool:
    return record.get("boot_id") == current_boot_id() and pid_is_alive(record.get("pid"))


def reconcile_review_preparations(*, project_root: Path = PROJECT_ROOT, dry_run: bool = False) -> list[dict[str, str]]:
    """Run bounded durable review requests in this host process, never via API."""
    root = project_root / "storage" / "content_jobs"
    if dry_run:
        decisions: list[dict[str, str]] = []
        for path in sorted(root.glob(f"*/*/{review_preparation.STATE_NAME}")) if root.is_dir() else ():
            with path.open(encoding="utf-8") as handle:
                record = json.load(handle)
            if isinstance(record, dict) and review_preparation.due(record):
                decisions.append({"kind": "review", "content_id": str(record.get("content_id", "unknown")), "action": "would_run"})
        return decisions
    lock_path = root / ".review-preparation-runner.lock"
    root.mkdir(parents=True, exist_ok=True)
    decisions: list[dict[str, str]] = []
    with lock_path.open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return [{"kind": "review", "content_id": "unknown", "action": "runner_active"}]
        try:
            records = sorted(root.glob(f"*/*/{review_preparation.STATE_NAME}"))
            for path in records:
                if len([item for item in decisions if item.get("action") not in {"ignored", "stale"}]) >= _review_max_concurrency():
                    break
                with path.open("r+", encoding="utf-8") as handle:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                    try:
                        record = review_preparation._read(handle)
                        content_id = str(record.get("content_id", "unknown"))
                        if record.get("state") == "running":
                            if _review_record_is_live(record):
                                decisions.append({"kind": "review", "content_id": content_id, "action": "ignored"})
                                continue
                            reason = _stale_reason(record)
                            record["state"] = "pending"
                            record.pop("pid", None); record.pop("boot_id", None)
                            review_preparation._write(handle, record)
                            decisions.append({"kind": "review", "content_id": content_id, "action": "stale", "reason": reason})
                        if not review_preparation.due(record):
                            decisions.append({"kind": "review", "content_id": content_id, "action": "ignored"})
                            continue
                    finally:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                if dry_run:
                    decisions.append({"kind": "review", "content_id": content_id, "action": "would_run"})
                    continue
                result = review_preparation.run_record(path, boot_id=current_boot_id(), pid=os.getpid())
                result["kind"] = "review"
                if result["action"] in {"retry_wait", "error"}:
                    LOG.error("review preparation failed content_id=%s niche_id=%s stage=ingest_or_review attempt=%s exception_class=%s elapsed_ms=%s", result["content_id"], result.get("niche_id"), result["attempt"], result["error_class"], result["elapsed_ms"])
                else:
                    LOG.info("review preparation finished content_id=%s niche_id=%s stage=review attempt=%s elapsed_ms=%s", result["content_id"], result.get("niche_id"), result["attempt"], result["elapsed_ms"])
                decisions.append(result)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return decisions


def reconcile_now(*, project_root: Path = PROJECT_ROOT, dry_run: bool = False) -> list[dict[str, str]]:
    """Reconcile NOW records, launching only records still in ``launching``."""
    decisions: list[dict[str, str]] = []
    records = sorted((project_root / "storage" / "content_jobs").glob("*/*/production-schedule.json"))
    for record_path in records:
        with record_path.open("r+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                record = _read_record(record_path)
                state = record.get("production_state")
                content_id = str(record.get("content_id", "unknown"))
                if state in {"completed", "error"}:
                    decisions.append({"kind": "now", "content_id": content_id, "action": "ignored"})
                    continue
                if state == "producing":
                    action = "ignored" if producing_record_is_live(record) else "stale"
                    decision = {"kind": "now", "content_id": content_id, "action": action}
                    if action == "stale":
                        decision["reason"] = _stale_reason(record)
                    decisions.append(decision)
                    continue
                if state != "launching":
                    decisions.append({"kind": "now", "content_id": content_id, "action": "ignored"})
                    continue
                plan_path = _host_plan_path(record.get("production_plan_path"), project_root)
                command = now_command(plan_path)
                if dry_run:
                    decisions.append({"kind": "now", "content_id": content_id, "action": "would_launch", "command": " ".join(command)})
                    continue
                producer = subprocess.Popen(command, cwd=project_root.as_posix())
                record["production_state"] = "producing"
                record["pid"] = producer.pid
                record["boot_id"] = current_boot_id()
                _write_record(handle, record)
            except Exception as exc:
                # A claimed record must never remain launching after a launch error.
                try:
                    record = _read_record(record_path)
                    record["production_state"] = "error"
                    _write_record(handle, record)
                except Exception:
                    pass
                decisions.append({"kind": "now", "content_id": "unknown", "action": "error", "reason": type(exc).__name__})
                continue
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

        try:
            returncode = producer.wait()
            if returncode != 0:
                raise RuntimeError("producer failed")
            content_delivery.finalize_production_plan(plan_path)
            final_state = "completed"
        except Exception as exc:
            final_state = "error"
            failure = type(exc).__name__
        with record_path.open("r+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                record = _read_record(record_path)
                record["production_state"] = final_state
                _write_record(handle, record)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        decision = {"kind": "now", "content_id": content_id, "action": final_state, "command": " ".join(command)}
        if final_state == "error":
            decision["reason"] = failure
        decisions.append(decision)
    return decisions


def nightly_window_is_open() -> bool:
    """Use the canonical nightly runner's default window semantics."""
    args = nightly_runner.build_parser().parse_args([])
    return nightly_runner.is_in_window(dt.datetime.now().astimezone(), args.window_start, args.window_end)


def reconcile_night(*, project_root: Path = PROJECT_ROOT, dry_run: bool = False) -> dict[str, str]:
    queue_dir = project_root / "storage" / "nightly_jobs"
    if not any((queue_dir / "pending").glob("*.json")):
        return {"kind": "night", "action": "no_pending"}
    if not nightly_window_is_open():
        return {"kind": "night", "action": "outside_window"}

    command = nightly_command(project_root)
    if dry_run:
        # Dry runs only observe lock presence.  Canonical lock acquisition may
        # reclaim stale locks, so it must remain exclusive to real execution.
        if (queue_dir / "nightly_runner.lock").exists():
            return {"kind": "night", "action": "lock_present"}
        return {"kind": "night", "action": "would_launch", "command": " ".join(command)}

    # Reuse the canonical runner's lock semantics: it rejects a live lock and
    # repairs a lock whose recorded PID is dead.  The canonical child acquires
    # the lock again before it can process work, so it remains authoritative
    # if another launcher races us after this preflight check.
    try:
        lock_path = nightly_runner.acquire_lock(queue_dir)
    except nightly_runner.RunnerError:
        return {"kind": "night", "action": "runner_active"}
    nightly_runner.release_lock(lock_path, logger=None)

    process = subprocess.Popen(command, cwd=project_root.as_posix())
    if process.wait() == 0:
        return {"kind": "night", "action": "completed", "command": " ".join(command)}
    return {"kind": "night", "action": "error", "command": " ".join(command)}


def reconcile_once(*, project_root: Path = PROJECT_ROOT, dry_run: bool = False) -> list[dict[str, str]]:
    return [*reconcile_review_preparations(project_root=project_root, dry_run=dry_run), *reconcile_now(project_root=project_root, dry_run=dry_run), reconcile_night(project_root=project_root, dry_run=dry_run)]


def _schedule_for_plan(plan_path: Path, project_root: Path) -> Path:
    """Find the one existing NOW schedule record for an approved plan."""
    matches: list[Path] = []
    for record_path in (project_root / "storage" / "content_jobs").glob("*/*/production-schedule.json"):
        try:
            record = _read_record(record_path)
            if _host_plan_path(record.get("production_plan_path"), project_root).resolve() == plan_path.resolve():
                matches.append(record_path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    if len(matches) != 1:
        raise ValueError("expected exactly one production schedule for approved plan")
    return matches[0]


def finalize_only(plan_path: str | Path, *, project_root: Path = PROJECT_ROOT) -> dict[str, str]:
    """Deliver one already-rendered approved plan without invoking a renderer."""
    host_plan = _host_plan_path(str(plan_path), project_root)
    record_path = _schedule_for_plan(host_plan, project_root)
    try:
        delivery = content_delivery.finalize_production_plan(host_plan)
    except Exception:
        with record_path.open("r+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                record = _read_record(record_path)
                record["production_state"] = "error"
                _write_record(handle, record)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        raise
    with record_path.open("r+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            record = _read_record(record_path)
            record["production_state"] = "completed"
            _write_record(handle, record)
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return delivery


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="One-shot host execution dispatcher")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--finalize-only", action="store_true")
    parser.add_argument("--approved-plan", metavar="PATH")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.finalize_only:
        if not args.approved_plan:
            build_parser().error("--finalize-only requires --approved-plan PATH")
        try:
            delivery = finalize_only(args.approved_plan)
        except Exception as exc:
            print(json.dumps({"kind": "finalize_only", "action": "error", "reason": type(exc).__name__}, sort_keys=True))
            return 1
        print(json.dumps({"kind": "finalize_only", "action": "completed", **delivery}, ensure_ascii=False, sort_keys=True))
        return 0
    if args.approved_plan:
        build_parser().error("--approved-plan requires --finalize-only")
    for decision in reconcile_once(dry_run=args.dry_run):
        print(json.dumps(decision, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
