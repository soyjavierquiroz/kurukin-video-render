"""Small local visual picker for Kurukin audio intents."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


LOCAL_PICKER_SOURCE = "local_picker_v1"

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
VISUAL_EXTENSIONS = VIDEO_EXTENSIONS | IMAGE_EXTENSIONS

DEFAULT_SAFE_VISUAL_DIRS = (
    Path("storage/local_videos"),
    Path("storage/local_images"),
    Path("storage/job-assets"),
    Path("storage/local_assets"),
    Path("resource/videos"),
    Path("resource/images"),
    Path("tests/fixtures"),
)

VERTICAL_NAME_HINTS = (
    "vertical",
    "reel",
    "short",
    "1080x1920",
    "9x16",
)


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _allowed_dirs(extra_dirs: list[str] | None = None) -> tuple[Path, ...]:
    dirs = list(DEFAULT_SAFE_VISUAL_DIRS)
    for item in extra_dirs or []:
        text = _clean_text(item)
        if text:
            dirs.append(Path(text))
    return tuple(dirs)


def _has_hidden_part(path: Path) -> bool:
    return any(part.startswith(".") for part in path.parts if part not in {".", ".."})


def _is_url_or_remote_path(text: str) -> bool:
    lower = text.lower()
    return lower.startswith("http://") or lower.startswith("https://") or "://" in lower


def _is_under_allowed_dir(path: Path, allowed_dirs: tuple[Path, ...]) -> bool:
    return any(
        path == allowed_dir or allowed_dir in path.parents
        for allowed_dir in allowed_dirs
    )


def _safe_relative_visual_path(
    path: str,
    *,
    allowed_dirs: tuple[Path, ...] | None = None,
) -> str:
    text = _clean_text(path)
    if not text or _is_url_or_remote_path(text):
        return ""

    candidate = Path(text)
    if candidate.is_absolute() or ".." in candidate.parts:
        return ""
    if _has_hidden_part(candidate):
        return ""
    if candidate.suffix.lower() not in VISUAL_EXTENSIONS:
        return ""
    if not _is_under_allowed_dir(candidate, allowed_dirs or DEFAULT_SAFE_VISUAL_DIRS):
        return ""
    return candidate.as_posix()


def is_safe_local_visual_path(path: str) -> bool:
    return bool(_safe_relative_visual_path(path))


def _visual_type(path: Path) -> str:
    return "video" if path.suffix.lower() in VIDEO_EXTENSIONS else "image"


def _keyword_tokens(intent: dict[str, Any]) -> set[str]:
    text = " ".join(
        _clean_text(intent.get(key))
        for key in ("topic", "preset")
    ).lower()
    return {
        token
        for token in re.split(r"[^a-z0-9]+", text)
        if len(token) >= 3
    }


def _candidate_score(
    candidate: dict[str, Any],
    *,
    intent: dict[str, Any] | None = None,
) -> int:
    score = 0
    reasons = candidate.setdefault("score_reasons", [])

    if candidate.get("type") == "video":
        score += 100
        reasons.append("video")
    else:
        score += 30
        reasons.append("image")

    name = Path(candidate.get("path", "")).name.lower()
    tokens = _keyword_tokens(intent or {})
    matches = sorted(token for token in tokens if token in name)
    if matches:
        score += 20 * len(matches)
        reasons.append("name_matches_intent")

    if any(hint in name for hint in VERTICAL_NAME_HINTS):
        score += 15
        reasons.append("vertical_name_hint")

    size_bytes = int(candidate.get("size_bytes") or 0)
    if size_bytes and size_bytes <= 25 * 1024 * 1024:
        score += 10
        reasons.append("smoke_friendly_size")
    elif size_bytes and size_bytes <= 150 * 1024 * 1024:
        score += 4
        reasons.append("reasonable_size")
    elif size_bytes:
        score -= 10
        reasons.append("large_file")

    return score


def discover_local_visual_candidates(
    extra_dirs: list[str] | None = None,
    *,
    project_root: str | Path | None = None,
) -> list[dict[str, Any]]:
    root = Path(project_root).resolve() if project_root else Path.cwd().resolve()
    allowed_dirs = _allowed_dirs(extra_dirs)
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()

    for search_dir in allowed_dirs:
        if (
            search_dir.is_absolute()
            or ".." in search_dir.parts
            or _has_hidden_part(search_dir)
        ):
            continue
        absolute_dir = (root / search_dir).resolve()
        try:
            absolute_dir.relative_to(root)
        except ValueError:
            continue
        if not absolute_dir.is_dir():
            continue

        for file_path in absolute_dir.rglob("*"):
            if not file_path.is_file():
                continue
            try:
                relative = file_path.resolve().relative_to(root)
            except ValueError:
                continue

            safe_path = _safe_relative_visual_path(
                relative.as_posix(),
                allowed_dirs=allowed_dirs,
            )
            if not safe_path or safe_path in seen:
                continue
            seen.add(safe_path)
            candidates.append(
                {
                    "path": safe_path,
                    "type": _visual_type(relative),
                    "size_bytes": file_path.stat().st_size,
                    "source": LOCAL_PICKER_SOURCE,
                }
            )

    candidates.sort(key=lambda item: (item["path"].lower(), item["size_bytes"]))
    return candidates


def pick_local_visual_for_intent(
    intent: dict[str, Any],
    *,
    extra_dirs: list[str] | None = None,
    project_root: str | Path | None = None,
) -> dict[str, Any] | None:
    candidates = discover_local_visual_candidates(
        extra_dirs=extra_dirs,
        project_root=project_root,
    )
    if not candidates:
        return None

    scored: list[dict[str, Any]] = []
    for candidate in candidates:
        item = dict(candidate)
        item["score"] = _candidate_score(item, intent=intent)
        scored.append(item)

    scored.sort(
        key=lambda item: (
            -int(item["score"]),
            int(item.get("size_bytes") or 0),
            item["path"].lower(),
        )
    )
    return scored[0]
