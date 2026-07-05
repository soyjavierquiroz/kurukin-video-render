"""Filesystem queue helpers for Kurukin MoneyPrinterTurbo jobs."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import time
from typing import Any


QUEUE_GROUPS = ("pending", "processing", "completed", "failed", "logs")


class KurukinJobQueueError(ValueError):
    """Expected queue validation or filesystem safety error."""


def sanitize_job_id(value: str) -> str:
    """Return a filename-safe job id using only letters, numbers, - and _."""

    text = str(value or "").strip()
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"[^A-Za-z0-9_-]+", "", text)
    text = text.strip("-_")
    return text or "kurukin-job"


def build_pending_job_filename(job_id: str, now: datetime | None = None) -> str:
    """Build a pending queue filename compatible with the nightly runner."""

    timestamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S")
    return f"{timestamp}-{sanitize_job_id(job_id)}.json"


def _modified_at_iso(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()


def _ensure_child_path(path: Path, base_dir: Path) -> None:
    try:
        path.resolve(strict=False).relative_to(base_dir.resolve(strict=False))
    except ValueError as exc:
        raise KurukinJobQueueError(f"path escapes queue directory: {path}") from exc


def _job_id_from_payload(payload: dict[str, Any]) -> str:
    runner = payload.get("runner")
    if isinstance(runner, dict):
        runner_job_id = runner.get("job_id")
        if runner_job_id:
            return str(runner_job_id)
    return str(payload.get("job_id") or "")


def enqueue_moneyprinter_payload(
    payload: dict[str, Any],
    *,
    queue_dir: str | Path = "storage/nightly_jobs/pending",
    now: datetime | None = None,
) -> Path:
    """Atomically write a MoneyPrinterTurbo payload into a pending queue dir."""

    if not isinstance(payload, dict) or not payload:
        raise KurukinJobQueueError("payload must be a non-empty JSON object")

    pending_dir = Path(queue_dir)
    pending_dir.mkdir(parents=True, exist_ok=True)
    pending_dir = pending_dir.resolve()

    filename = build_pending_job_filename(_job_id_from_payload(payload), now=now)
    candidate = pending_dir / filename
    _ensure_child_path(candidate, pending_dir)

    stem = candidate.stem
    counter = 1
    while candidate.exists():
        candidate = pending_dir / f"{stem}-{counter}.json"
        _ensure_child_path(candidate, pending_dir)
        counter += 1
        if counter > 100:
            candidate = pending_dir / f"{stem}-{time.time_ns()}.json"
            _ensure_child_path(candidate, pending_dir)
            break

    temp_path = pending_dir / f".{candidate.name}.tmp-{os.getpid()}-{time.time_ns()}"
    _ensure_child_path(temp_path, pending_dir)
    try:
        with temp_path.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
        os.replace(temp_path, candidate)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass

    return candidate


def _list_entries(directory: Path) -> list[dict[str, Any]]:
    if not directory.exists():
        return []
    entries = []
    for path in sorted(directory.iterdir(), key=lambda item: item.name):
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        entries.append(
            {
                "name": path.name,
                "path": path.as_posix(),
                "size_bytes": stat.st_size,
                "modified_at_iso": datetime.fromtimestamp(
                    stat.st_mtime,
                    timezone.utc,
                ).isoformat(),
            }
        )
    return entries


def list_nightly_queue(base_dir: str | Path = "storage/nightly_jobs") -> dict[str, list]:
    """List queue groups without reading job contents."""

    base = Path(base_dir)
    return {group: _list_entries(base / group) for group in QUEUE_GROUPS}


def list_render_tasks(tasks_dir: str | Path = "storage/tasks") -> list[dict[str, Any]]:
    """List render task directories and whether each has final-1.mp4."""

    base = Path(tasks_dir)
    if not base.exists():
        return []

    tasks = []
    for path in sorted(base.iterdir(), key=lambda item: item.name):
        if not path.is_dir():
            continue
        final_video = path / "final-1.mp4"
        has_final_video = final_video.is_file()
        tasks.append(
            {
                "task_id": path.name,
                "path": path.as_posix(),
                "has_final_video": has_final_video,
                "final_video_path": final_video.as_posix() if has_final_video else "",
                "final_video_size_bytes": (
                    final_video.stat().st_size if has_final_video else 0
                ),
                "modified_at_iso": _modified_at_iso(path),
            }
        )
    return tasks


def _directory_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return total
    if path.is_file():
        return path.stat().st_size
    for root, _, files in os.walk(path):
        for filename in files:
            file_path = Path(root) / filename
            try:
                total += file_path.stat().st_size
            except FileNotFoundError:
                continue
    return total


def get_storage_summary(storage_dir: str | Path = "storage") -> dict[str, Any]:
    """Return approximate storage size and first-level subdirectory sizes."""

    base = Path(storage_dir)
    subdirs = []
    if base.exists():
        for path in sorted(base.iterdir(), key=lambda item: item.name):
            if path.is_dir():
                subdirs.append(
                    {
                        "name": path.name,
                        "path": path.as_posix(),
                        "size_bytes": _directory_size(path),
                        "modified_at_iso": _modified_at_iso(path),
                    }
                )
    total_size_bytes = _directory_size(base)
    return {
        "path": base.as_posix(),
        "size_bytes": total_size_bytes,
        "total_size_bytes": total_size_bytes,
        "subdirs": subdirs,
    }
