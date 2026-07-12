"""Batch helpers for local audio job intents."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any

from app.custom.kurukin_job_intent import (
    DEFAULT_DURATION_SECONDS,
    DEFAULT_FORMAT,
    DEFAULT_LANGUAGE,
    DEFAULT_PRESET,
    MODE_AUDIO_TO_VIDEO,
)
from app.custom.kurukin_job_queue import enqueue_job_intent, sanitize_job_id


AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".ogg"}
DEFAULT_MAX_ITEMS = 10


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _is_remote_path(value: str) -> bool:
    lower = value.lower()
    return lower.startswith("http://") or lower.startswith("https://") or "://" in lower


def _has_hidden_part(path: PurePosixPath) -> bool:
    return any(part.startswith(".") for part in path.parts if part not in {".", ".."})


def _safe_audio_path(value: Any) -> str:
    text = _clean_text(value)
    if not text:
        return ""
    if _is_remote_path(text):
        raise ValueError("audio paths must be local, not URLs")
    path = PurePosixPath(text)
    if path.is_absolute() or "\\" in text or ".." in path.parts:
        raise ValueError("audio paths must be relative local paths")
    if _has_hidden_part(path):
        raise ValueError("hidden audio paths are not allowed")
    if path.suffix.lower() not in AUDIO_EXTENSIONS:
        return ""
    return path.as_posix()


def _safe_audio_folder(value: Any) -> str:
    text = _clean_text(value)
    if not text:
        return ""
    if _is_remote_path(text):
        raise ValueError("audio_folder must be local, not a URL")
    path = PurePosixPath(text)
    if path.is_absolute() or "\\" in text or ".." in path.parts:
        raise ValueError("audio_folder must be a relative local path")
    if _has_hidden_part(path):
        raise ValueError("hidden audio folders are not allowed")
    return path.as_posix()


def _clamp_max_items(value: Any) -> int:
    try:
        count = int(value)
    except (TypeError, ValueError):
        count = DEFAULT_MAX_ITEMS
    return max(1, min(count, DEFAULT_MAX_ITEMS))


def discover_audio_inputs(
    audio_folder: str | None = None,
    audio_paths: list[str] | None = None,
    *,
    max_items: int = DEFAULT_MAX_ITEMS,
    project_root: str | Path | None = None,
) -> list[str]:
    """Return safe local audio paths from an explicit list and/or folder."""

    root = Path(project_root).resolve() if project_root else Path.cwd().resolve()
    limit = _clamp_max_items(max_items)
    discovered: list[str] = []

    folder = _safe_audio_folder(audio_folder)
    if folder:
        folder_path = (root / folder).resolve()
        try:
            folder_path.relative_to(root)
        except ValueError as exc:
            raise ValueError("audio_folder must stay under project root") from exc
        if folder_path.is_dir():
            for item in sorted(folder_path.iterdir(), key=lambda path: path.name.lower()):
                if len(discovered) >= limit:
                    break
                if not item.is_file():
                    continue
                try:
                    relative = item.resolve().relative_to(root).as_posix()
                except ValueError:
                    continue
                safe = _safe_audio_path(relative)
                if safe and safe not in discovered:
                    discovered.append(safe)

    for item in audio_paths or []:
        if len(discovered) >= limit:
            break
        safe = _safe_audio_path(item)
        if safe:
            discovered.append(safe)

    return discovered[:limit]


def build_audio_batch_intents(
    *,
    audio_folder: str | None = None,
    audio_paths: list[str] | None = None,
    topic: str = "",
    language: str = DEFAULT_LANGUAGE,
    duration_seconds: int = DEFAULT_DURATION_SECONDS,
    format: str = DEFAULT_FORMAT,
    preset: str = DEFAULT_PRESET,
    task_id_prefix: str = "",
    max_items: int = DEFAULT_MAX_ITEMS,
    project_root: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Build one audio_to_video intent per safe local audio input."""

    inputs = discover_audio_inputs(
        audio_folder=audio_folder,
        audio_paths=audio_paths,
        max_items=max_items,
        project_root=project_root,
    )
    prefix = sanitize_job_id(task_id_prefix or "kurukin-batch-audio")
    intents: list[dict[str, Any]] = []
    for index, audio_path in enumerate(inputs, start=1):
        task_id = f"{prefix}-{index:03d}"
        intents.append(
            {
                "mode": MODE_AUDIO_TO_VIDEO,
                "task_id": task_id,
                "audio_path": audio_path,
                "topic": _clean_text(topic),
                "language": _clean_text(language) or DEFAULT_LANGUAGE,
                "duration_seconds": int(duration_seconds or DEFAULT_DURATION_SECONDS),
                "format": _clean_text(format) or DEFAULT_FORMAT,
                "preset": _clean_text(preset) or DEFAULT_PRESET,
            }
        )
    return intents


def enqueue_audio_batch_intents(
    *,
    audio_folder: str | None = None,
    audio_paths: list[str] | None = None,
    topic: str = "",
    language: str = DEFAULT_LANGUAGE,
    duration_seconds: int = DEFAULT_DURATION_SECONDS,
    format: str = DEFAULT_FORMAT,
    preset: str = DEFAULT_PRESET,
    task_id_prefix: str = "",
    max_items: int = DEFAULT_MAX_ITEMS,
    queue_dir: str | Path = "storage/nightly_jobs/pending",
    project_root: str | Path | None = None,
    enqueuer=enqueue_job_intent,
) -> dict[str, Any]:
    """Compile and enqueue audio_to_video intents without rendering."""

    try:
        intents = build_audio_batch_intents(
            audio_folder=audio_folder,
            audio_paths=audio_paths,
            topic=topic,
            language=language,
            duration_seconds=duration_seconds,
            format=format,
            preset=preset,
            task_id_prefix=task_id_prefix,
            max_items=max_items,
            project_root=project_root,
        )
    except ValueError as exc:
        return {
            "ok": False,
            "created": 0,
            "skipped": 0,
            "items": [],
            "errors": [str(exc)],
            "reason": "invalid_audio_inputs",
        }

    if not intents:
        return {
            "ok": False,
            "created": 0,
            "skipped": 0,
            "items": [],
            "errors": [],
            "reason": "no_audio_inputs",
        }

    items: list[dict[str, Any]] = []
    errors: list[str] = []
    created = 0
    skipped = 0
    for intent in intents:
        result = enqueuer(
            intent,
            queue_dir=queue_dir,
            project_root=project_root,
        )
        compiled = result.get("compiled") if isinstance(result, dict) else {}
        normalized = compiled.get("intent") if isinstance(compiled, dict) else {}
        if result.get("ok"):
            created += 1
            items.append(
                {
                    "ok": True,
                    "task_id": result.get("task_id", intent["task_id"]),
                    "status": result.get("status", "QUEUED"),
                    "audio_path": intent["audio_path"],
                    "resolved_visual_path": (
                        result.get("payload", {}).get("resolved_visual_path", "")
                    ),
                    "visual_autofill_source": (
                        result.get("payload", {}).get("visual_autofill_source", "")
                    ),
                    "queue_item_path": result.get("pending_path", ""),
                }
            )
            continue

        skipped += 1
        reasons = list(result.get("reasons") or [])
        if not reasons and result.get("errors"):
            reasons = [
                str(error.get("message") or error.get("field") or error)
                if isinstance(error, dict)
                else str(error)
                for error in result.get("errors") or []
            ]
        items.append(
            {
                "ok": False,
                "task_id": result.get("task_id") or intent["task_id"],
                "status": result.get("status", "SKIPPED"),
                "audio_path": normalized.get("audio_path") or intent["audio_path"],
                "resolved_visual_path": normalized.get("resolved_visual_path", ""),
                "visual_autofill_source": normalized.get("visual_autofill_source", ""),
                "queue_item_path": "",
                "reasons": reasons,
            }
        )
        errors.extend(reasons)

    return {
        "ok": created > 0 and not errors,
        "created": created,
        "skipped": skipped,
        "items": items,
        "errors": errors,
    }
