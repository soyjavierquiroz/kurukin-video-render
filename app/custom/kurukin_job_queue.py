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
DEFAULT_PENDING_DIR = Path("storage/nightly_jobs/pending")
DEFAULT_TASKS_DIR = Path("storage/tasks")
MAX_TASK_SCAN_DEPTH = 4
MAX_TASK_SCAN_FILES = 500
MAX_LOG_READ_BYTES = 64 * 1024
MAX_LOG_LINES = 80


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


def _safe_stat(path: Path) -> os.stat_result | None:
    try:
        return path.stat()
    except (FileNotFoundError, OSError):
        return None


def _safe_modified_at_iso(path: Path) -> str:
    stat = _safe_stat(path)
    if stat is None:
        return ""
    return datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()


def _safe_size(path: Path) -> int:
    stat = _safe_stat(path)
    return stat.st_size if stat is not None else 0


def _relative_path(path: Path, base_dir: Path | None = None) -> str:
    if base_dir is None:
        return path.as_posix()
    try:
        return path.relative_to(base_dir).as_posix()
    except ValueError:
        return path.as_posix()


def safe_storage_path(*parts: str | Path) -> Path:
    """Build a path under storage without allowing absolute or parent segments."""

    base_dir = Path("storage")
    safe_parts = []
    for part in parts:
        text = str(part)
        candidate = Path(text)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise KurukinJobQueueError(f"unsafe storage path segment: {text}")
        safe_parts.append(text)
    path = base_dir.joinpath(*safe_parts)
    _ensure_child_path(path, base_dir)
    return path


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


def _filename_datetime(path: Path) -> str:
    match = re.match(r"(?P<date>\d{8})-(?P<time>\d{6})-", path.name)
    if not match:
        return ""
    value = f"{match.group('date')}{match.group('time')}"
    try:
        return datetime.strptime(value, "%Y%m%d%H%M%S").replace(
            tzinfo=timezone.utc
        ).isoformat()
    except ValueError:
        return ""


def _nested_get(value: dict[str, Any], *keys: str) -> Any:
    current: Any = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _pending_asset_source(payload: dict[str, Any]) -> str:
    if payload.get("asset_hub_renderer_manifest_path") or payload.get(
        "asset_hub_bundle_uid"
    ):
        return "Asset Hub Bundle"
    if payload.get("video_materials"):
        return "Assets locales"
    return str(payload.get("video_source") or "-")


def _pending_subtitles(payload: dict[str, Any]) -> str:
    runner_mode = _nested_get(payload, "runner", "subtitles", "mode")
    if runner_mode:
        return str(runner_mode)
    if payload.get("subtitle_enabled") is False:
        return "Sin subtítulos"
    return str(payload.get("subtitle_provider") or "-")


def summarize_pending_job(path: str | Path) -> dict[str, Any]:
    """Read one pending queue JSON and return a human summary."""

    pending_path = Path(path)
    stat = _safe_stat(pending_path)
    summary: dict[str, Any] = {
        "filename": pending_path.name,
        "path": pending_path.as_posix(),
        "size_bytes": stat.st_size if stat else 0,
        "modified_at_iso": (
            datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()
            if stat
            else ""
        ),
        "created_at_iso": _filename_datetime(pending_path),
        "valid_json": False,
        "error": "",
        "raw": {},
    }

    try:
        with pending_path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except (FileNotFoundError, OSError, json.JSONDecodeError) as exc:
        summary.update(
            {
                "job_id": pending_path.stem,
                "title": "-",
                "asset_source": "-",
                "quality": "-",
                "subtitles": "-",
                "error": str(exc),
            }
        )
        return summary

    if not isinstance(payload, dict):
        summary.update(
            {
                "job_id": pending_path.stem,
                "title": "-",
                "asset_source": "-",
                "quality": "-",
                "subtitles": "-",
                "error": "Pending JSON is not an object",
            }
        )
        return summary

    runner_job_id = _nested_get(payload, "runner", "job_id")
    summary.update(
        {
            "valid_json": True,
            "job_id": str(runner_job_id or payload.get("job_id") or pending_path.stem),
            "title": str(
                payload.get("video_subject")
                or payload.get("subject")
                or payload.get("title")
                or "-"
            ),
            "asset_source": _pending_asset_source(payload),
            "quality": str(payload.get("video_resolution") or payload.get("render_quality") or "-"),
            "subtitles": _pending_subtitles(payload),
            "raw": payload,
        }
    )
    return summary


def list_pending_jobs(pending_dir: str | Path | None = None) -> list[dict[str, Any]]:
    """List pending queue JSON files with safe best-effort summaries."""

    base = Path(pending_dir) if pending_dir is not None else DEFAULT_PENDING_DIR
    if not base.exists() or not base.is_dir():
        return []

    jobs = []
    for path in sorted(base.glob("*.json"), key=lambda item: item.name):
        if not path.is_file():
            continue
        jobs.append(summarize_pending_job(path))
    return jobs


def _iter_task_files(
    task_dir: Path,
    *,
    max_depth: int = MAX_TASK_SCAN_DEPTH,
    max_files: int = MAX_TASK_SCAN_FILES,
) -> list[Path]:
    if not task_dir.exists() or not task_dir.is_dir():
        return []

    files: list[Path] = []
    base_depth = len(task_dir.parts)
    for root, dirs, filenames in os.walk(task_dir):
        root_path = Path(root)
        depth = len(root_path.parts) - base_depth
        if depth >= max_depth:
            dirs[:] = []
        dirs[:] = sorted(dirs)
        for filename in sorted(filenames):
            files.append(root_path / filename)
            if len(files) >= max_files:
                return files
    return files


def detect_task_outputs(task_dir: str | Path) -> list[dict[str, Any]]:
    """Return MP4 files found under a task directory with a bounded scan."""

    base = Path(task_dir)
    outputs = []
    for path in _iter_task_files(base):
        if path.suffix.lower() != ".mp4":
            continue
        outputs.append(
            {
                "name": path.name,
                "path": path.as_posix(),
                "relative_path": _relative_path(path, base),
                "size_bytes": _safe_size(path),
                "modified_at_iso": _safe_modified_at_iso(path),
                "task_id": base.name,
            }
        )
    return outputs


def _read_small_text_tail(path: Path) -> str:
    stat = _safe_stat(path)
    if stat is None or stat.st_size > MAX_LOG_READ_BYTES:
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except (FileNotFoundError, OSError):
        return ""
    return "\n".join(lines[-MAX_LOG_LINES:])


def _detect_error_signals(task_dir: Path) -> list[dict[str, Any]]:
    signals = []
    error_name_re = re.compile(r"(error|failed|exception|traceback)", re.IGNORECASE)
    error_text_re = re.compile(r"(traceback|exception|error|failed)", re.IGNORECASE)

    for path in _iter_task_files(task_dir):
        relative_path = _relative_path(path, task_dir)
        name_signal = bool(error_name_re.search(path.name))
        text_tail = ""
        text_signal = False
        if path.suffix.lower() in {".log", ".txt", ".json"}:
            text_tail = _read_small_text_tail(path)
            text_signal = bool(text_tail and error_text_re.search(text_tail))
        if name_signal or text_signal:
            signals.append(
                {
                    "name": path.name,
                    "path": path.as_posix(),
                    "relative_path": relative_path,
                    "size_bytes": _safe_size(path),
                    "modified_at_iso": _safe_modified_at_iso(path),
                    "preview": text_tail,
                }
            )
    return signals


def _task_has_recent_files(task_dir: Path, *, recent_seconds: int = 60 * 60) -> bool:
    now = time.time()
    for path in _iter_task_files(task_dir):
        stat = _safe_stat(path)
        if stat and now - stat.st_mtime <= recent_seconds:
            return True
    return False


def infer_task_status(task_dir: str | Path) -> str:
    """Infer task state from files only, without mutating or executing anything."""

    base = Path(task_dir)
    if detect_task_outputs(base):
        return "completed"
    if _detect_error_signals(base):
        return "failed"
    if _task_has_recent_files(base):
        return "processing"
    return "unknown"


def _task_logs(task_dir: Path) -> list[dict[str, Any]]:
    logs = []
    for path in _iter_task_files(task_dir):
        if path.suffix.lower() not in {".log", ".txt"}:
            continue
        logs.append(
            {
                "name": path.name,
                "path": path.as_posix(),
                "relative_path": _relative_path(path, task_dir),
                "size_bytes": _safe_size(path),
                "modified_at_iso": _safe_modified_at_iso(path),
                "preview": _read_small_text_tail(path),
            }
        )
    return logs


def _task_size(task_dir: Path) -> int:
    total = 0
    for path in _iter_task_files(task_dir):
        total += _safe_size(path)
    return total


def list_task_summaries(tasks_dir: str | Path | None = None) -> list[dict[str, Any]]:
    """Summarize immediate task directories with bounded read-only inspection."""

    base = Path(tasks_dir) if tasks_dir is not None else DEFAULT_TASKS_DIR
    if not base.exists() or not base.is_dir():
        return []

    tasks = []
    for task_dir in sorted(base.iterdir(), key=lambda item: item.name):
        if not task_dir.is_dir():
            continue
        outputs = detect_task_outputs(task_dir)
        errors = _detect_error_signals(task_dir)
        status = "completed" if outputs else "failed" if errors else infer_task_status(task_dir)
        tasks.append(
            {
                "task_id": task_dir.name,
                "job_id": task_dir.name,
                "path": task_dir.as_posix(),
                "status": status,
                "outputs": outputs,
                "output_count": len(outputs),
                "logs": _task_logs(task_dir),
                "log_count": len(_task_logs(task_dir)),
                "errors": errors,
                "error_count": len(errors),
                "size_bytes": _task_size(task_dir),
                "modified_at_iso": _safe_modified_at_iso(task_dir),
            }
        )
    return tasks


def get_job_lifecycle_summary(
    *,
    pending_dir: str | Path | None = None,
    tasks_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Return a read-only dashboard summary for queue and render artifacts."""

    pending_jobs = list_pending_jobs(pending_dir)
    tasks = list_task_summaries(tasks_dir)
    outputs = [output for task in tasks for output in task.get("outputs", [])]
    completed = [task for task in tasks if task.get("status") == "completed"]
    failed = [task for task in tasks if task.get("status") == "failed"]
    processing = [task for task in tasks if task.get("status") == "processing"]
    return {
        "pending_jobs": pending_jobs,
        "tasks": tasks,
        "outputs": outputs,
        "counts": {
            "pending": len(pending_jobs),
            "processing": len(processing),
            "completed": len(completed),
            "failed": len(failed),
            "videos": len(outputs),
        },
    }


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
