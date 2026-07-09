"""Filesystem queue helpers for Kurukin MoneyPrinterTurbo jobs."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any

from app.custom.asset_source_policy import summarize_asset_source_policy


QUEUE_GROUPS = ("pending", "processing", "completed", "failed", "logs")
DEFAULT_PENDING_DIR = Path("storage/nightly_jobs/pending")
DEFAULT_COMPLETED_DIR = Path("storage/nightly_jobs/completed")
DEFAULT_TASKS_DIR = Path("storage/tasks")
MAX_TASK_SCAN_DEPTH = 4
MAX_TASK_SCAN_FILES = 500
MAX_LOG_READ_BYTES = 64 * 1024
MAX_LOG_LINES = 80
MAX_STORAGE_SCAN_DEPTH = 5
MAX_STORAGE_SCAN_FILES = 2000
VIDEO_PREVIEW_MAX_BYTES = 200 * 1024 * 1024
VIDEO_DOWNLOAD_MEMORY_MAX_BYTES = 500 * 1024 * 1024
RENDER_MODE_NORMAL = "normal"
RENDER_MODE_AROLL_BROLL = "aroll_broll"
AROLL_BROLL_DEFAULT_LAYOUT = "alternating_fullscreen"
AROLL_BROLL_AUDIO_SUMMARY = "A-roll original"
AROLL_BROLL_BROLL_SUMMARY = "B-roll muted"
UI_RUNNER_ENABLED_VALUES = {"1", "true", "TRUE", "yes", "YES"}
FEATURE_FLAG_ENABLED_VALUES = {"1", "true", "yes", "on"}
AROLL_BROLL_QUEUE_FLAG = "KURUKIN_ENABLE_AROLL_BROLL_QUEUE"
AROLL_BROLL_RENDERER_FLAG = "KURUKIN_ENABLE_AROLL_BROLL_RENDERER"
SAFE_RUNNER_RELATIVE_PATH = "scripts/nightly_runner.py"
SAFE_RUNNER_COMMAND = ("python3", SAFE_RUNNER_RELATIVE_PATH)
CONTAINER_NIGHTLY_QUEUE_DIR = "/MoneyPrinterTurbo/storage/nightly_jobs"
CONTAINER_API_BASE_URL = "http://api:8080/api/v1"
MANUAL_RUNNER_EXECUTION_MODE = "manual_now"
DEFAULT_RUNNER_EXECUTION_MODE = "nightly_default"
MANUAL_RUNNER_MAX_JOBS = 1
MANUAL_RUNNER_COMMAND = (
    "python3",
    SAFE_RUNNER_RELATIVE_PATH,
    "--max-jobs",
    str(MANUAL_RUNNER_MAX_JOBS),
    "--ignore-window",
    "--queue-dir",
    CONTAINER_NIGHTLY_QUEUE_DIR,
    "--api-base-url",
    CONTAINER_API_BASE_URL,
)
CONTAINER_PROJECT_ROOT = Path("/MoneyPrinterTurbo")
RUNNER_CONFIRM_TEXT = "EJECUTAR RENDER"
RUNNER_QUEUE_CONFIRM_TEXT = "procesar cola pendiente"
RUNNER_CANDIDATE_PATHS = (
    (
        "Nightly runner",
        "scripts/nightly_runner.py",
        "python3 scripts/nightly_runner.py",
        "high",
        "Runner de cola detectado por archivo Python.",
    ),
    (
        "Local job wrapper",
        "scripts/local_job_wrapper.py",
        "python3 scripts/local_job_wrapper.py --help",
        "medium",
        "CLI de preparación/encolado; no ejecuta renders por sí solo.",
    ),
)


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


def _human_size(size_bytes: int | float | None) -> str:
    try:
        value = float(size_bytes or 0)
    except (TypeError, ValueError):
        value = 0.0
    units = ("B", "KB", "MB", "GB", "TB")
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


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


def _normalize_render_mode(value: Any) -> str:
    if str(value or "").strip() == RENDER_MODE_AROLL_BROLL:
        return RENDER_MODE_AROLL_BROLL
    return RENDER_MODE_NORMAL


def summarize_render_mode(render_mode: str) -> str:
    """Return the product-facing label for a render mode."""

    if _normalize_render_mode(render_mode) == RENDER_MODE_AROLL_BROLL:
        return "Presentador + B-roll"
    return "Video normal"


def _payload_render_mode(payload: dict[str, Any] | None) -> str:
    if not isinstance(payload, dict):
        return RENDER_MODE_NORMAL
    candidates = (
        payload.get("render_mode"),
        _nested_get(payload, "data", "render_mode"),
        _nested_get(payload, "task", "render_mode"),
        _nested_get(payload, "runner", "render_mode"),
        _nested_get(payload, "aroll_broll", "render_mode"),
    )
    for value in candidates:
        if _normalize_render_mode(value) == RENDER_MODE_AROLL_BROLL:
            return RENDER_MODE_AROLL_BROLL
    if isinstance(payload.get("aroll_broll"), dict):
        return RENDER_MODE_AROLL_BROLL
    return RENDER_MODE_NORMAL


def _looks_like_aroll_broll_task_id(value: Any) -> bool:
    return str(value or "").strip().startswith("aroll-broll")


def _extract_layout_preset(*payloads: dict[str, Any] | None) -> str:
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        candidates = (
            _nested_get(payload, "aroll_broll", "layout", "preset"),
            _nested_get(payload, "layout", "preset"),
            _nested_get(payload, "data", "layout_preset"),
            _nested_get(payload, "data", "aroll_broll", "layout", "preset"),
            _nested_get(payload, "task", "layout_preset"),
            payload.get("layout_preset"),
        )
        for value in candidates:
            text = str(value or "").strip()
            if text:
                return text
    return AROLL_BROLL_DEFAULT_LAYOUT


def _extract_broll_asset_count(*payloads: dict[str, Any] | None) -> int | None:
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        candidates = (
            payload.get("b_roll_asset_count"),
            _nested_get(payload, "data", "b_roll_asset_count"),
            _nested_get(payload, "task", "b_roll_asset_count"),
        )
        for value in candidates:
            try:
                count = int(value)
            except (TypeError, ValueError):
                continue
            if count > 0:
                return count

        asset_lists = (
            _nested_get(payload, "aroll_broll", "b_roll", "assets"),
            _nested_get(payload, "data", "aroll_broll", "b_roll", "assets"),
        )
        for assets in asset_lists:
            if isinstance(assets, list) and assets:
                return len(assets)
    return None


def _extract_asset_policy_summary(
    *payloads: dict[str, Any] | None,
) -> dict[str, Any] | None:
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        candidates = (
            payload.get("asset_policy"),
            _nested_get(payload, "aroll_broll", "asset_policy"),
            _nested_get(payload, "data", "asset_policy"),
            _nested_get(payload, "data", "aroll_broll", "asset_policy"),
            _nested_get(payload, "task", "asset_policy"),
        )
        for candidate in candidates:
            if isinstance(candidate, dict):
                return summarize_asset_source_policy(candidate)
    return None


def _render_mode_metadata(
    render_mode: str,
    *,
    layout_preset: str = "",
    b_roll_asset_count: int | None = None,
    asset_policy_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized = _normalize_render_mode(render_mode)
    metadata = {
        "render_mode": normalized,
        "render_mode_label": summarize_render_mode(normalized),
    }
    if normalized == RENDER_MODE_AROLL_BROLL:
        metadata.update(
            {
                "layout_preset": layout_preset or AROLL_BROLL_DEFAULT_LAYOUT,
                "audio_summary": AROLL_BROLL_AUDIO_SUMMARY,
                "broll_summary": AROLL_BROLL_BROLL_SUMMARY,
            }
        )
        if b_roll_asset_count is not None and b_roll_asset_count > 0:
            metadata["b_roll_asset_count"] = b_roll_asset_count
        if asset_policy_summary:
            metadata["asset_policy"] = asset_policy_summary
            metadata["asset_policy_label"] = asset_policy_summary.get("label")
            metadata["asset_policy_short_label"] = asset_policy_summary.get(
                "short_label"
            )
    return metadata


def _detect_render_mode_from_payloads(
    *payloads: dict[str, Any] | None,
    task_id: str = "",
    job_id: str = "",
    path_name: str = "",
) -> str:
    for payload in payloads:
        if _payload_render_mode(payload) == RENDER_MODE_AROLL_BROLL:
            return RENDER_MODE_AROLL_BROLL
    if _looks_like_aroll_broll_task_id(task_id) or _looks_like_aroll_broll_task_id(job_id):
        return RENDER_MODE_AROLL_BROLL
    if _looks_like_aroll_broll_task_id(path_name):
        return RENDER_MODE_AROLL_BROLL
    return RENDER_MODE_NORMAL


def detect_render_mode_for_job(job_or_completed_dir: Mapping[str, Any] | str | Path) -> str:
    """Detect whether a pending/completed/task summary belongs to A-roll/B-roll."""

    if isinstance(job_or_completed_dir, Mapping):
        task_id = str(job_or_completed_dir.get("task_id") or "")
        job_id = str(job_or_completed_dir.get("job_id") or "")
        raw = job_or_completed_dir.get("raw")
        final_task_summary = job_or_completed_dir.get("final_task_summary")
        return _detect_render_mode_from_payloads(
            raw if isinstance(raw, dict) else None,
            final_task_summary if isinstance(final_task_summary, dict) else None,
            dict(job_or_completed_dir),
            task_id=task_id,
            job_id=job_id,
        )

    path = Path(job_or_completed_dir)
    payloads = [
        _read_json_file(path / "job.json"),
        _read_json_file(path / "submit-response.json"),
        _read_json_file(path / "final-task.json"),
        _read_json_file(path / "render-result.json"),
        _read_json_file(path / "error.json"),
    ]
    task_id = (
        _extract_task_id_from_submit_response(payloads[1])
        or _extract_task_id_from_final_task(payloads[2])
    )
    job_id = _extract_job_id_from_completed_payload(payloads[0])
    if not task_id:
        for ref in _extract_video_refs_from_final_task(payloads[2]):
            task_id = _task_id_from_video_ref(ref)
            if task_id:
                break
    return _detect_render_mode_from_payloads(
        *payloads,
        task_id=task_id,
        job_id=job_id,
        path_name=path.name,
    )


def _pending_asset_source(payload: dict[str, Any]) -> str:
    if _payload_render_mode(payload) == RENDER_MODE_AROLL_BROLL:
        return "A-roll/B-roll"
    if payload.get("asset_hub_renderer_manifest_path") or payload.get(
        "asset_hub_bundle_uid"
    ):
        return "Asset Hub Bundle"
    if payload.get("video_materials"):
        return "Assets locales"
    return str(payload.get("video_source") or "-")


def _pending_subtitles(payload: dict[str, Any]) -> str:
    if _payload_render_mode(payload) == RENDER_MODE_AROLL_BROLL:
        source = _nested_get(payload, "aroll_broll", "subtitles", "source")
        return {
            "aroll_audio": "Audio A-roll",
            "custom_srt": "SRT propio",
            "none": "Sin subtítulos",
        }.get(str(source or ""), str(source or "-"))
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
    task_id = str(payload.get("task_id") or _nested_get(payload, "runner", "task_id") or "")
    render_mode = _detect_render_mode_from_payloads(
        payload,
        task_id=task_id,
        job_id=str(runner_job_id or payload.get("job_id") or pending_path.stem),
        path_name=pending_path.stem,
    )
    summary.update(
        {
            "valid_json": True,
            "job_id": str(runner_job_id or payload.get("job_id") or pending_path.stem),
            "task_id": task_id,
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
    summary.update(
        _render_mode_metadata(
            render_mode,
            layout_preset=_extract_layout_preset(payload),
            b_roll_asset_count=_extract_broll_asset_count(payload),
            asset_policy_summary=_extract_asset_policy_summary(payload),
        )
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


def _resolve_tasks_dir(tasks_dir: str | Path | None = None) -> Path:
    return (Path(tasks_dir) if tasks_dir is not None else DEFAULT_TASKS_DIR).resolve(
        strict=False
    )


def _safe_video_path(path: Path, tasks_root: Path) -> Path | None:
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(tasks_root)
    except (FileNotFoundError, OSError, ValueError):
        return None
    if not resolved.is_file() or resolved.suffix.lower() != ".mp4":
        return None
    return resolved


def _video_kind(path: Path) -> str:
    name = path.name.lower()
    if name.startswith("final-"):
        return "final"
    if name.startswith("combined-"):
        return "combined"
    return "other"


def _video_sort_key(video: Mapping[str, Any]) -> tuple[int, float, str]:
    kind_priority = {"final": 0, "combined": 1}.get(str(video.get("kind")), 2)
    return (
        kind_priority,
        -float(video.get("modified_at_timestamp") or 0),
        str(video.get("relative_path") or ""),
    )


def _read_json_file(path: Path) -> dict[str, Any] | None:
    stat = _safe_stat(path)
    if stat is None or stat.st_size > MAX_LOG_READ_BYTES:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _summarize_final_task(task_dir: Path) -> dict[str, Any]:
    payload = _read_json_file(task_dir / "final-task.json")
    if not payload:
        return _render_mode_metadata(
            RENDER_MODE_AROLL_BROLL
            if _looks_like_aroll_broll_task_id(task_dir.name)
            else RENDER_MODE_NORMAL
        )
    summary: dict[str, Any] = {}
    for key in ("state", "progress", "videos", "task_id", "job_id", "render_mode", "layout_preset"):
        if key in payload:
            summary[key] = payload.get(key)
    if "state" not in summary:
        state = _nested_get(payload, "task", "state") or _nested_get(payload, "data", "state")
        if state is not None:
            summary["state"] = state
    if "progress" not in summary:
        progress = _nested_get(payload, "task", "progress") or _nested_get(
            payload,
            "data",
            "progress",
        )
        if progress is not None:
            summary["progress"] = progress
    if "videos" not in summary:
        videos = _nested_get(payload, "task", "videos") or _nested_get(
            payload,
            "data",
            "videos",
        )
        if videos is not None:
            summary["videos"] = videos
    if "task_id" not in summary:
        task_id = _nested_get(payload, "task", "task_id") or _nested_get(
            payload,
            "data",
            "task_id",
        )
        if task_id is not None:
            summary["task_id"] = task_id
    if "job_id" not in summary:
        job_id = _nested_get(payload, "task", "job_id") or _nested_get(
            payload,
            "data",
            "job_id",
        )
        if job_id is not None:
            summary["job_id"] = job_id
    render_mode = _detect_render_mode_from_payloads(
        payload,
        task_id=str(summary.get("task_id") or task_dir.name),
        job_id=str(summary.get("job_id") or ""),
    )
    summary.update(
        _render_mode_metadata(
            render_mode,
            layout_preset=_extract_layout_preset(payload),
            b_roll_asset_count=_extract_broll_asset_count(payload),
            asset_policy_summary=_extract_asset_policy_summary(payload),
        )
    )
    return summary


def _coerce_json_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _extract_task_id_from_submit_response(payload: dict[str, Any] | None) -> str:
    if not payload:
        return ""
    value = payload.get("task_id") or _nested_get(payload, "data", "task_id")
    return str(value or "").strip()


def _extract_task_id_from_final_task(payload: dict[str, Any] | None) -> str:
    if not payload:
        return ""
    value = (
        payload.get("task_id")
        or _nested_get(payload, "data", "task_id")
        or _nested_get(payload, "task", "task_id")
    )
    return str(value or "").strip()


def _extract_job_id_from_completed_payload(payload: dict[str, Any] | None) -> str:
    if not payload:
        return ""
    value = payload.get("job_id") or _nested_get(payload, "runner", "job_id")
    return str(value or "").strip()


def _extract_video_refs_from_final_task(payload: dict[str, Any] | None) -> list[str]:
    if not payload:
        return []
    refs: list[str] = []
    for value in (
        payload.get("videos"),
        _nested_get(payload, "data", "videos"),
        _nested_get(payload, "task", "videos"),
        payload.get("combined_videos"),
        _nested_get(payload, "data", "combined_videos"),
        _nested_get(payload, "task", "combined_videos"),
    ):
        refs.extend(str(item) for item in _coerce_json_list(value) if item)
    return refs


def _task_id_from_video_ref(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parts = Path(text).parts
    if parts[:1] == ("/",):
        parts = parts[1:]
    if len(parts) >= 3 and parts[0] in {"tasks", "storage"}:
        if parts[0] == "storage" and len(parts) >= 4 and parts[1] == "tasks":
            return parts[2]
        if parts[0] == "tasks":
            return parts[1]
    if len(parts) == 2 and parts[1].lower().endswith(".mp4"):
        return parts[0]
    return ""


def _completed_job_sort_key(job: Mapping[str, Any]) -> tuple[float, str]:
    return (
        -float(job.get("modified_at_timestamp") or 0),
        str(job.get("completed_dir_relative") or ""),
    )


def _completed_job_metadata_by_task(
    completed_dir: str | Path | None = None,
) -> dict[str, dict[str, Any]]:
    completed_root = (
        Path(completed_dir) if completed_dir is not None else DEFAULT_COMPLETED_DIR
    ).resolve(strict=False)
    if not completed_root.exists() or not completed_root.is_dir():
        return {}

    by_task: dict[str, dict[str, Any]] = {}
    for path in sorted(completed_root.iterdir(), key=lambda item: item.name):
        if not path.is_dir():
            continue
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(completed_root)
        except (FileNotFoundError, OSError, ValueError):
            continue
        job_payload = _read_json_file(resolved / "job.json")
        submit_payload = _read_json_file(resolved / "submit-response.json")
        final_task_payload = _read_json_file(resolved / "final-task.json")
        render_result_payload = _read_json_file(resolved / "render-result.json")
        error_payload = _read_json_file(resolved / "error.json")
        video_refs = _extract_video_refs_from_final_task(final_task_payload)
        task_id = (
            _extract_task_id_from_submit_response(submit_payload)
            or _extract_task_id_from_final_task(final_task_payload)
        )
        if not task_id:
            for ref in video_refs:
                task_id = _task_id_from_video_ref(ref)
                if task_id:
                    break
        if not task_id:
            continue
        job_id = _extract_job_id_from_completed_payload(job_payload)
        render_mode = _detect_render_mode_from_payloads(
            job_payload,
            submit_payload,
            final_task_payload,
            render_result_payload,
            error_payload,
            task_id=task_id,
            job_id=job_id,
            path_name=resolved.name,
        )
        metadata: dict[str, Any] = {
            "job_id": job_id,
            "completed_job_id": job_id,
            "completed_dir": _relative_path(resolved, completed_root),
            "completed_dir_relative": _relative_path(resolved, completed_root),
            "completed_at": _safe_modified_at_iso(resolved),
        }
        metadata.update(
            _render_mode_metadata(
                render_mode,
                layout_preset=_extract_layout_preset(
                    job_payload,
                    submit_payload,
                    final_task_payload,
                    render_result_payload,
                    error_payload,
                ),
                b_roll_asset_count=_extract_broll_asset_count(
                    job_payload,
                    submit_payload,
                    final_task_payload,
                    render_result_payload,
                    error_payload,
                ),
                asset_policy_summary=_extract_asset_policy_summary(
                    job_payload,
                    submit_payload,
                    final_task_payload,
                    render_result_payload,
                    error_payload,
                ),
            )
        )
        by_task[task_id] = metadata
    return by_task


def list_completed_render_jobs(
    completed_dir: str | Path | None = None,
    *,
    tasks_dir: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Summarize completed nightly jobs and their detected task result signals.

    The scan is read-only. JSON files are used only as hints, and any returned
    MP4 metadata is resolved through ``list_rendered_videos`` under storage/tasks.
    """

    completed_root = (
        Path(completed_dir) if completed_dir is not None else DEFAULT_COMPLETED_DIR
    ).resolve(strict=False)
    if not completed_root.exists() or not completed_root.is_dir():
        return []

    videos = list_rendered_videos(tasks_dir, completed_dir=completed_root)
    videos_by_task: dict[str, list[dict[str, Any]]] = {}
    for video in videos:
        videos_by_task.setdefault(str(video.get("task_id") or ""), []).append(video)

    jobs: list[dict[str, Any]] = []
    for path in sorted(completed_root.iterdir(), key=lambda item: item.name):
        if not path.is_dir():
            continue
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(completed_root)
        except (FileNotFoundError, OSError, ValueError):
            continue

        job_payload = _read_json_file(resolved / "job.json")
        submit_payload = _read_json_file(resolved / "submit-response.json")
        final_task_payload = _read_json_file(resolved / "final-task.json")
        stat = _safe_stat(resolved)
        video_refs = _extract_video_refs_from_final_task(final_task_payload)

        task_id = (
            _extract_task_id_from_submit_response(submit_payload)
            or _extract_task_id_from_final_task(final_task_payload)
        )
        if not task_id:
            for ref in video_refs:
                task_id = _task_id_from_video_ref(ref)
                if task_id:
                    break

        final_task_summary = {}
        if final_task_payload:
            for key in ("state", "progress", "videos", "task_id", "job_id", "render_mode", "layout_preset"):
                value = final_task_payload.get(key)
                if value is not None:
                    final_task_summary[key] = value
            data = final_task_payload.get("data")
            if isinstance(data, dict):
                for key in ("state", "progress", "videos", "task_id", "job_id", "render_mode", "layout_preset"):
                    if key not in final_task_summary and data.get(key) is not None:
                        final_task_summary[key] = data.get(key)

        render_mode = _detect_render_mode_from_payloads(
            job_payload,
            submit_payload,
            final_task_payload,
            task_id=task_id,
            job_id=_extract_job_id_from_completed_payload(job_payload),
            path_name=resolved.name,
        )
        mode_metadata = _render_mode_metadata(
            render_mode,
            layout_preset=_extract_layout_preset(
                job_payload,
                submit_payload,
                final_task_payload,
            ),
            b_roll_asset_count=_extract_broll_asset_count(
                job_payload,
                submit_payload,
                final_task_payload,
            ),
            asset_policy_summary=_extract_asset_policy_summary(
                job_payload,
                submit_payload,
                final_task_payload,
            ),
        )
        final_task_summary.update(mode_metadata)

        detected_videos = list(videos_by_task.get(task_id, [])) if task_id else []
        jobs.append(
            {
                "job_id": _extract_job_id_from_completed_payload(job_payload),
                "task_id": task_id,
                "completed_dir": _relative_path(resolved, completed_root),
                "completed_dir_relative": _relative_path(resolved, completed_root),
                "completed_at": _safe_modified_at_iso(resolved),
                "modified_at": _safe_modified_at_iso(resolved),
                "modified_at_iso": _safe_modified_at_iso(resolved),
                "modified_at_timestamp": stat.st_mtime if stat else 0,
                "state": final_task_summary.get("state"),
                "progress": final_task_summary.get("progress"),
                "final_task_summary": final_task_summary,
                "result_video_refs": video_refs,
                "final_video_paths": [
                    video.get("relative_path")
                    for video in detected_videos
                    if video.get("kind") == "final"
                ],
                "videos": detected_videos,
                **mode_metadata,
            }
        )

    return sorted(jobs, key=_completed_job_sort_key)


def _summarize_error_json(task_dir: Path) -> str:
    payload = _read_json_file(task_dir / "error.json")
    if not payload:
        return ""
    for key in ("error", "message", "detail"):
        value = payload.get(key)
        if value:
            return str(value)[:500]
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)[:500]


def list_rendered_videos(
    tasks_dir: str | Path | None = None,
    *,
    preview_max_bytes: int = VIDEO_PREVIEW_MAX_BYTES,
    completed_dir: str | Path | None = None,
) -> list[dict[str, Any]]:
    """List final/combined MP4 render outputs under storage/tasks.

    The scan is read-only and every returned file is resolved back under
    ``tasks_dir`` so symlinks cannot expose files outside the render task tree.
    """

    tasks_root = _resolve_tasks_dir(tasks_dir)
    if not tasks_root.exists() or not tasks_root.is_dir():
        return []

    completed_metadata = (
        _completed_job_metadata_by_task(completed_dir)
        if completed_dir is not None or tasks_dir is None
        else {}
    )
    videos: list[dict[str, Any]] = []
    for task_dir in sorted(tasks_root.iterdir(), key=lambda item: item.name):
        if not task_dir.is_dir():
            continue
        try:
            task_resolved = task_dir.resolve(strict=True)
            task_resolved.relative_to(tasks_root)
        except (FileNotFoundError, OSError, ValueError):
            continue
        if not task_resolved.is_dir():
            continue

        for path in _iter_task_files(task_resolved):
            kind = _video_kind(path)
            if kind not in {"final", "combined"}:
                continue
            resolved = _safe_video_path(path, tasks_root)
            if resolved is None:
                continue
            stat = _safe_stat(resolved)
            if stat is None:
                continue
            relative_path = _relative_path(resolved, tasks_root)
            task_id = relative_path.split("/", 1)[0] if "/" in relative_path else task_resolved.name
            video = {
                "task_id": task_id,
                "file_name": resolved.name,
                "name": resolved.name,
                "relative_path": relative_path,
                "absolute_path": resolved.as_posix(),
                "size_bytes": stat.st_size,
                "size_label": _human_size(stat.st_size),
                "modified_at": datetime.fromtimestamp(
                    stat.st_mtime,
                    timezone.utc,
                ).isoformat(),
                "modified_at_iso": datetime.fromtimestamp(
                    stat.st_mtime,
                    timezone.utc,
                ).isoformat(),
                "modified_at_timestamp": stat.st_mtime,
                "kind": kind,
                "is_previewable": stat.st_size <= preview_max_bytes,
                "previewable": stat.st_size <= preview_max_bytes,
                "final_task_summary": _summarize_final_task(task_resolved),
                "error_summary": _summarize_error_json(task_resolved),
            }
            final_task_summary = video["final_task_summary"]
            if isinstance(final_task_summary, dict) and final_task_summary.get("job_id"):
                video["completed_job_id"] = final_task_summary.get("job_id")
            render_mode = detect_render_mode_for_job(video)
            video.update(
                _render_mode_metadata(
                    render_mode,
                    layout_preset=str(final_task_summary.get("layout_preset") or ""),
                    b_roll_asset_count=_extract_broll_asset_count(
                        final_task_summary
                    ),
                    asset_policy_summary=_extract_asset_policy_summary(
                        final_task_summary
                    ),
                )
            )
            metadata = completed_metadata.get(task_id)
            if metadata:
                video.update({key: value for key, value in metadata.items() if value})
            videos.append(video)

    return sorted(videos, key=_video_sort_key)


def _best_video_for_task(
    task_id: str,
    *,
    tasks_dir: str | Path | None = None,
    preview_max_bytes: int = VIDEO_PREVIEW_MAX_BYTES,
) -> dict[str, Any] | None:
    task_id_text = str(task_id or "").strip()
    if not task_id_text:
        return None
    videos = [
        video
        for video in list_rendered_videos(tasks_dir, preview_max_bytes=preview_max_bytes)
        if str(video.get("task_id") or "") == task_id_text
    ]
    if not videos:
        return None
    return sorted(videos, key=_video_sort_key)[0]


def find_result_for_job(
    job_id: str,
    *,
    completed_dir: str | Path | None = None,
    tasks_dir: str | Path | None = None,
    preview_max_bytes: int = VIDEO_PREVIEW_MAX_BYTES,
) -> dict[str, Any] | None:
    """Return the best final video result associated with a completed job id."""

    target_job_id = str(job_id or "").strip()
    if not target_job_id:
        return None

    jobs = [
        job
        for job in list_completed_render_jobs(
            completed_dir,
            tasks_dir=tasks_dir,
        )
        if str(job.get("job_id") or "") == target_job_id
    ]
    for job in sorted(jobs, key=_completed_job_sort_key):
        video = _best_video_for_task(
            str(job.get("task_id") or ""),
            tasks_dir=tasks_dir,
            preview_max_bytes=preview_max_bytes,
        )
        if not video:
            continue
        result = dict(video)
        result.update(
            {
                "job_id": job.get("job_id"),
                "completed_job_id": job.get("job_id") or video.get("completed_job_id"),
                "completed_dir": job.get("completed_dir"),
                "completed_at": job.get("completed_at"),
                "state": job.get("state"),
                "progress": job.get("progress"),
                "render_mode": job.get("render_mode") or video.get("render_mode"),
                "render_mode_label": job.get("render_mode_label")
                or video.get("render_mode_label"),
                "layout_preset": job.get("layout_preset") or video.get("layout_preset"),
                "audio_summary": job.get("audio_summary") or video.get("audio_summary"),
                "broll_summary": job.get("broll_summary") or video.get("broll_summary"),
                "asset_policy": job.get("asset_policy") or video.get("asset_policy"),
                "asset_policy_label": job.get("asset_policy_label")
                or video.get("asset_policy_label"),
                "asset_policy_short_label": job.get("asset_policy_short_label")
                or video.get("asset_policy_short_label"),
                "recommendation": "last_job",
                "recommendation_title": "Tu video más reciente",
            }
        )
        for optional_key in (
            "asset_policy",
            "asset_policy_label",
            "asset_policy_short_label",
        ):
            if result.get(optional_key) is None:
                result.pop(optional_key, None)
        return result
    return None


def _latest_final_video(
    *,
    tasks_dir: str | Path | None = None,
    preview_max_bytes: int = VIDEO_PREVIEW_MAX_BYTES,
) -> dict[str, Any] | None:
    final_videos = [
        video
        for video in list_rendered_videos(tasks_dir, preview_max_bytes=preview_max_bytes)
        if video.get("kind") == "final"
    ]
    if not final_videos:
        return None
    return max(
        final_videos,
        key=lambda video: float(video.get("modified_at_timestamp") or 0),
    )


def get_recommended_result(
    last_job_id: str | None = None,
    *,
    completed_dir: str | Path | None = None,
    tasks_dir: str | Path | None = None,
    preview_max_bytes: int = VIDEO_PREVIEW_MAX_BYTES,
) -> dict[str, Any] | None:
    """Return the user's most relevant render result, falling back to latest final."""

    if last_job_id:
        result = find_result_for_job(
            str(last_job_id),
            completed_dir=completed_dir,
            tasks_dir=tasks_dir,
            preview_max_bytes=preview_max_bytes,
        )
        if result:
            return result

    latest = _latest_final_video(
        tasks_dir=tasks_dir,
        preview_max_bytes=preview_max_bytes,
    )
    if not latest:
        return None
    result = dict(latest)
    result.update(
        {
            "job_id": latest.get("completed_job_id"),
            "recommendation": "latest_final",
            "recommendation_title": "Último video generado",
        }
    )
    return result


def get_latest_rendered_video(
    tasks_dir: str | Path | None = None,
    *,
    preview_max_bytes: int = VIDEO_PREVIEW_MAX_BYTES,
) -> dict[str, Any] | None:
    """Return the most recently modified rendered video, if any."""

    videos = list_rendered_videos(tasks_dir, preview_max_bytes=preview_max_bytes)
    if not videos:
        return None
    return max(videos, key=lambda video: float(video.get("modified_at_timestamp") or 0))


def read_video_bytes_for_download(
    video: Mapping[str, Any],
    *,
    tasks_dir: str | Path | None = None,
    max_bytes: int = VIDEO_DOWNLOAD_MEMORY_MAX_BYTES,
) -> bytes | None:
    """Read bytes for a previously detected MP4 video.

    The input must be a video object returned by ``list_rendered_videos``. The
    path is resolved and validated again before reading.
    """

    if not isinstance(video, Mapping):
        return None
    tasks_root = _resolve_tasks_dir(tasks_dir)
    raw_path = video.get("absolute_path")
    if not raw_path:
        relative_path = str(video.get("relative_path") or "")
        if not relative_path:
            return None
        candidate = tasks_root / relative_path
    else:
        candidate = Path(str(raw_path))

    resolved = _safe_video_path(candidate, tasks_root)
    if resolved is None:
        return None
    stat = _safe_stat(resolved)
    if stat is None or stat.st_size > max_bytes:
        return None
    try:
        return resolved.read_bytes()
    except (FileNotFoundError, OSError):
        return None


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
        final_task_summary = _summarize_final_task(task_dir)
        render_mode = detect_render_mode_for_job(
            {
                "task_id": task_dir.name,
                "job_id": final_task_summary.get("job_id") or task_dir.name,
                "final_task_summary": final_task_summary,
            }
        )
        mode_metadata = _render_mode_metadata(
            render_mode,
            layout_preset=str(final_task_summary.get("layout_preset") or ""),
            b_roll_asset_count=_extract_broll_asset_count(final_task_summary),
            asset_policy_summary=_extract_asset_policy_summary(final_task_summary),
        )
        for output in outputs:
            output.update(mode_metadata)
        status = "completed" if outputs else "failed" if errors else infer_task_status(task_dir)
        tasks.append(
            {
                "task_id": task_dir.name,
                "job_id": final_task_summary.get("job_id") or task_dir.name,
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
                "final_task_summary": final_task_summary,
                **mode_metadata,
            }
        )
    return tasks


def get_job_lifecycle_summary(
    *,
    pending_dir: str | Path | None = None,
    tasks_dir: str | Path | None = None,
    completed_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Return a read-only dashboard summary for queue and render artifacts."""

    pending_jobs = list_pending_jobs(pending_dir)
    tasks = list_task_summaries(tasks_dir)
    completed_jobs = list_completed_render_jobs(completed_dir, tasks_dir=tasks_dir)
    outputs = [output for task in tasks for output in task.get("outputs", [])]
    completed = [task for task in tasks if task.get("status") == "completed"]
    failed = [task for task in tasks if task.get("status") == "failed"]
    processing = [task for task in tasks if task.get("status") == "processing"]
    return {
        "pending_jobs": pending_jobs,
        "completed_jobs": completed_jobs,
        "tasks": tasks,
        "outputs": outputs,
        "counts": {
            "pending": len(pending_jobs),
            "processing": len(processing),
            "completed": len(completed_jobs) if completed_jobs else len(completed),
            "failed": len(failed),
            "videos": len(outputs),
        },
    }


def get_storage_usage_summary(
    storage_dir: str | Path | None = None,
    *,
    max_depth: int = MAX_STORAGE_SCAN_DEPTH,
    max_files: int = MAX_STORAGE_SCAN_FILES,
) -> dict[str, Any]:
    """Return bounded read-only storage usage information."""

    base = Path(storage_dir) if storage_dir is not None else Path("storage")
    summary: dict[str, Any] = {
        "path": base.as_posix(),
        "exists": base.exists(),
        "total_size_bytes": 0,
        "size_bytes": 0,
        "file_count": 0,
        "dir_count": 0,
        "scan_truncated": False,
        "warning": "",
    }
    if not base.exists():
        summary["warning"] = "Storage directory not found"
        return summary
    if base.is_file():
        size = _safe_size(base)
        summary.update({"total_size_bytes": size, "size_bytes": size, "file_count": 1})
        return summary

    base_depth = len(base.parts)
    for root, dirs, files in os.walk(base):
        root_path = Path(root)
        depth = len(root_path.parts) - base_depth
        if depth >= max_depth:
            dirs[:] = []
        dirs[:] = sorted(dirs)
        summary["dir_count"] += len(dirs)
        for filename in sorted(files):
            file_path = root_path / filename
            summary["file_count"] += 1
            summary["total_size_bytes"] += _safe_size(file_path)
            if summary["file_count"] >= max_files:
                summary["scan_truncated"] = True
                summary["warning"] = "Storage scan reached safety limit"
                summary["size_bytes"] = summary["total_size_bytes"]
                return summary
    summary["size_bytes"] = summary["total_size_bytes"]
    return summary


def detect_runner_candidates(project_root: str | Path | None = None) -> list[dict[str, Any]]:
    """Detect runner-related files without importing or executing them."""

    root = Path(project_root) if project_root is not None else Path(".")
    candidates = []
    for name, relative_path, command, confidence, notes in RUNNER_CANDIDATE_PATHS:
        path = root / relative_path
        if not path.exists():
            continue
        candidates.append(
            {
                "name": name,
                "path": path.as_posix(),
                "relative_path": relative_path,
                "exists": True,
                "suggested_command": command,
                "confidence": confidence,
                "notes": notes,
            }
        )
    return candidates


def build_preflight_checks(
    lifecycle_summary: dict[str, Any],
    runner_candidates: list[dict[str, Any]],
    storage_summary: dict[str, Any],
    *,
    pending_dir: str | Path | None = None,
    max_pending_jobs: int = 10,
) -> list[dict[str, Any]]:
    """Build human preflight checks from read-only summaries."""

    counts = lifecycle_summary.get("counts", {})
    pending_count = int(counts.get("pending") or 0)
    tasks = lifecycle_summary.get("tasks") or []
    needs_review_tasks = [
        task
        for task in tasks
        if task.get("status") in {"failed", "unknown"}
    ]
    pending_path = Path(pending_dir) if pending_dir is not None else DEFAULT_PENDING_DIR
    storage_exists = bool(storage_summary.get("exists"))
    checks = [
        {
            "name": "Hay al menos un trabajo pendiente",
            "status": "Listo" if pending_count > 0 else "Revisar",
            "detail": (
                f"{pending_count} trabajo(s) pendiente(s)"
                if pending_count
                else "No hay trabajos pendientes para procesar."
            ),
        },
        {
            "name": "El runner está disponible",
            "status": "Listo" if runner_candidates else "No disponible",
            "detail": (
                f"{len(runner_candidates)} candidato(s) detectado(s)"
                if runner_candidates
                else "No se detectó un runner en el repo."
            ),
        },
        {
            "name": "El directorio de storage existe",
            "status": "Listo" if storage_exists else "No disponible",
            "detail": storage_summary.get("path") or "storage",
        },
        {
            "name": "El directorio de pending jobs existe",
            "status": "Listo" if pending_path.exists() else "Revisar",
            "detail": pending_path.as_posix(),
        },
        {
            "name": "No hay demasiados jobs pendientes",
            "status": "Listo" if pending_count <= max_pending_jobs else "Revisar",
            "detail": f"Límite recomendado: {max_pending_jobs}; actual: {pending_count}.",
        },
        {
            "name": "No hay tasks en estado desconocido/fallido sin revisar",
            "status": "Listo" if not needs_review_tasks else "Revisar",
            "detail": (
                "Sin tasks pendientes de revisión."
                if not needs_review_tasks
                else f"{len(needs_review_tasks)} task(s) requieren revisión."
            ),
        },
        {
            "name": "Storage no parece crecer de forma anormal",
            "status": "Revisar" if storage_summary.get("scan_truncated") else "Listo",
            "detail": storage_summary.get("warning") or "Escaneo acotado completado.",
        },
    ]
    return checks


def get_runner_preflight_summary(
    *,
    project_root: str | Path | None = None,
    storage_dir: str | Path | None = None,
    pending_dir: str | Path | None = None,
    tasks_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Return a read-only runner preflight summary."""

    lifecycle = get_job_lifecycle_summary(
        pending_dir=pending_dir,
        tasks_dir=tasks_dir,
    )
    storage = get_storage_usage_summary(storage_dir)
    runners = detect_runner_candidates(project_root)
    checks = build_preflight_checks(
        lifecycle,
        runners,
        storage,
        pending_dir=pending_dir,
    )
    return {
        "lifecycle": lifecycle,
        "storage": storage,
        "runner_candidates": runners,
        "checks": checks,
        "counts": {
            **lifecycle.get("counts", {}),
            "tasks": len(lifecycle.get("tasks", [])),
            "runner_candidates": len(runners),
        },
    }


def is_ui_runner_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Return whether controlled UI runner execution is explicitly enabled."""

    values = env if env is not None else os.environ
    return values.get("KURUKIN_ENABLE_UI_RUNNER", "") in UI_RUNNER_ENABLED_VALUES


def _feature_flag_enabled(name: str, env: Mapping[str, str] | None = None) -> bool:
    values = env if env is not None else os.environ
    return str(values.get(name, "")).strip().lower() in FEATURE_FLAG_ENABLED_VALUES


def is_aroll_broll_queue_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Return whether A-roll/B-roll pending creation is explicitly enabled."""

    return _feature_flag_enabled(AROLL_BROLL_QUEUE_FLAG, env)


def is_aroll_broll_renderer_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Return whether A-roll/B-roll runner execution is explicitly enabled."""

    return _feature_flag_enabled(AROLL_BROLL_RENDERER_FLAG, env)


def build_safe_runner_command(
    project_root: str | Path | None = None,
    *,
    manual_override: bool = False,
    max_jobs: int = MANUAL_RUNNER_MAX_JOBS,
) -> dict[str, Any]:
    """Build the only supported runner command without executing it."""

    root = (
        Path(project_root)
        if project_root is not None
        else CONTAINER_PROJECT_ROOT
        if CONTAINER_PROJECT_ROOT.exists()
        else Path(".")
    )
    runner_path = root / SAFE_RUNNER_RELATIVE_PATH
    if not runner_path.is_file():
        scripts_dir = root / "scripts"
        reason = "No se detectó scripts/nightly_runner.py."
        if root.name == "MoneyPrinterTurbo" and root.exists() and not scripts_dir.exists():
            reason = (
                "El runner no está montado en este contenedor. "
                "Revisa docker-compose.local.yml."
            )
        return {
            "available": False,
            "runner_name": "",
            "command": [],
            "cwd": root.as_posix(),
            "reason": reason,
            "confidence": "none",
            "execution_mode": (
                MANUAL_RUNNER_EXECUTION_MODE
                if manual_override
                else DEFAULT_RUNNER_EXECUTION_MODE
            ),
            "max_jobs": max_jobs if manual_override else None,
        }
    if manual_override and int(max_jobs) != MANUAL_RUNNER_MAX_JOBS:
        return {
            "available": False,
            "runner_name": "Nightly runner",
            "command": [],
            "cwd": root.resolve().as_posix(),
            "reason": "La ejecución manual desde UI solo permite max_jobs=1.",
            "confidence": "none",
            "execution_mode": MANUAL_RUNNER_EXECUTION_MODE,
            "max_jobs": int(max_jobs),
        }

    command = list(MANUAL_RUNNER_COMMAND if manual_override else SAFE_RUNNER_COMMAND)
    return {
        "available": True,
        "runner_name": "Nightly runner",
        "command": command,
        "cwd": root.resolve().as_posix(),
        "reason": (
            "Comando manual seguro: procesa 1 job ahora y salta la ventana nocturna."
            if manual_override
            else "Comando seguro calculado desde candidato high-confidence."
        ),
        "confidence": "high",
        "execution_mode": (
            MANUAL_RUNNER_EXECUTION_MODE
            if manual_override
            else DEFAULT_RUNNER_EXECUTION_MODE
        ),
        "max_jobs": MANUAL_RUNNER_MAX_JOBS if manual_override else None,
        "queue_dir": CONTAINER_NIGHTLY_QUEUE_DIR if manual_override else None,
        "api_base_url": CONTAINER_API_BASE_URL if manual_override else None,
    }


def _has_critical_preflight_errors(checks: list[dict[str, Any]]) -> bool:
    return any(check.get("status") == "No disponible" for check in checks)


def _manual_runner_queue_dir_is_safe(command_info: dict[str, Any]) -> bool:
    command = command_info.get("command")
    if not isinstance(command, list):
        return False
    try:
        option_index = command.index("--queue-dir")
    except ValueError:
        return False
    if option_index + 1 >= len(command):
        return False
    return (
        command[option_index + 1] == CONTAINER_NIGHTLY_QUEUE_DIR
        and command_info.get("queue_dir") == CONTAINER_NIGHTLY_QUEUE_DIR
    )


def _manual_runner_api_base_url_is_safe(command_info: dict[str, Any]) -> bool:
    command = command_info.get("command")
    if not isinstance(command, list):
        return False
    try:
        option_index = command.index("--api-base-url")
    except ValueError:
        return False
    if option_index + 1 >= len(command):
        return False
    return (
        command[option_index + 1] == CONTAINER_API_BASE_URL
        and command_info.get("api_base_url") == CONTAINER_API_BASE_URL
    )


def validate_runner_execution_request(
    *,
    feature_enabled: bool,
    preflight_summary: dict[str, Any],
    command_info: dict[str, Any],
    understood: bool,
    confirm_text: str,
    queue_confirmation: str,
    execution_mode: str = MANUAL_RUNNER_EXECUTION_MODE,
    max_jobs: int = MANUAL_RUNNER_MAX_JOBS,
) -> dict[str, Any]:
    """Validate all gates required before the UI can execute the runner."""

    counts = preflight_summary.get("counts", {})
    pending_count = int(counts.get("pending") or 0)
    checks = preflight_summary.get("checks") or []
    errors = []

    if not feature_enabled:
        errors.append("Ejecución desde UI deshabilitada por seguridad.")
    if pending_count <= 0:
        errors.append("No hay trabajos pendientes.")
    if not command_info.get("available") or command_info.get("confidence") != "high":
        errors.append("Runner high-confidence no disponible.")
    if execution_mode != MANUAL_RUNNER_EXECUTION_MODE:
        errors.append("Modo de ejecución manual no confirmado.")
    if int(max_jobs) != MANUAL_RUNNER_MAX_JOBS:
        errors.append("La ejecución manual solo permite max_jobs=1.")
    if command_info.get("execution_mode") != MANUAL_RUNNER_EXECUTION_MODE:
        errors.append("Comando manual seguro no disponible.")
    if command_info.get("max_jobs") != MANUAL_RUNNER_MAX_JOBS:
        errors.append("Comando manual debe limitarse a max_jobs=1.")
    if command_info.get("command") != list(MANUAL_RUNNER_COMMAND):
        errors.append("Comando manual seguro incorrecto.")
    if not _manual_runner_queue_dir_is_safe(command_info):
        errors.append("Queue dir manual seguro incorrecto.")
    if not _manual_runner_api_base_url_is_safe(command_info):
        errors.append("API base URL manual seguro incorrecto.")
    if _has_critical_preflight_errors(checks):
        errors.append("Preflight tiene errores críticos.")
    if not understood:
        errors.append("Falta confirmar que esto ejecutará render real.")
    if str(confirm_text or "").strip() != RUNNER_CONFIRM_TEXT:
        errors.append("Texto de confirmación incorrecto.")
    if str(queue_confirmation or "").strip() != RUNNER_QUEUE_CONFIRM_TEXT:
        errors.append("Confirmación de cola pendiente incorrecta.")

    return {
        "allowed": not errors,
        "errors": errors,
        "pending_count": pending_count,
        "feature_enabled": feature_enabled,
        "command_available": bool(command_info.get("available")),
        "execution_mode": execution_mode,
        "max_jobs": max_jobs,
    }


def _is_safe_runner_command(command_info: dict[str, Any]) -> bool:
    command = command_info.get("command")
    if command == list(SAFE_RUNNER_COMMAND):
        return (
            command_info.get("available") is True
            and command_info.get("confidence") == "high"
        )
    return (
        command_info.get("available") is True
        and command_info.get("confidence") == "high"
        and command == list(MANUAL_RUNNER_COMMAND)
        and command_info.get("execution_mode") == MANUAL_RUNNER_EXECUTION_MODE
        and command_info.get("max_jobs") == MANUAL_RUNNER_MAX_JOBS
        and command_info.get("queue_dir") == CONTAINER_NIGHTLY_QUEUE_DIR
        and command_info.get("api_base_url") == CONTAINER_API_BASE_URL
    )


def run_controlled_runner(
    command_info: dict[str, Any],
    *,
    runner: Callable[..., Any] | None = None,
    timeout_seconds: int = 60 * 60,
) -> dict[str, Any]:
    """Run the precomputed safe runner command, or an injected fake in tests."""

    if not _is_safe_runner_command(command_info):
        raise KurukinJobQueueError("unsafe or unavailable runner command")

    command = list(command_info["command"])
    cwd = command_info["cwd"]
    if runner is not None:
        result = runner(command=command, cwd=cwd, timeout=timeout_seconds)
        if isinstance(result, dict):
            return result
        return {
            "returncode": getattr(result, "returncode", 0),
            "stdout": getattr(result, "stdout", ""),
            "stderr": getattr(result, "stderr", ""),
            "command": command,
            "cwd": cwd,
            "timed_out": False,
        }

    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            timeout=timeout_seconds,
            capture_output=True,
            text=True,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "returncode": None,
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or "",
            "command": command,
            "cwd": cwd,
            "timed_out": True,
        }

    return {
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "command": command,
        "cwd": cwd,
        "timed_out": False,
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
