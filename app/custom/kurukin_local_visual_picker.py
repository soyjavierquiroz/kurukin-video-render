"""Small local visual picker for Kurukin audio intents."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Any


LOCAL_PICKER_SOURCE = "local_picker_v1"

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
VISUAL_EXTENSIONS = VIDEO_EXTENSIONS | IMAGE_EXTENSIONS

# These are production asset roots, not a convenient recursive search list.
# In particular test fixtures and review-generated assets must never become
# candidates just because they happen to be below the repository root.
DEFAULT_PRODUCTION_VISUAL_DIRS = (
    Path("storage/local_videos"),
    Path("storage/local_images"),
    Path("storage/job-assets"),
    Path("storage/local_assets"),
    Path("resource/videos"),
    Path("resource/images"),
)

# Backwards-compatible name for callers that import it.  Its contents are
# deliberately production-only.
DEFAULT_SAFE_VISUAL_DIRS = DEFAULT_PRODUCTION_VISUAL_DIRS
_NON_PRODUCTION_PATH_PARTS = {"tests", "test", "fixtures", "fixture", "tmp", "temp"}

VERTICAL_NAME_HINTS = (
    "vertical",
    "reel",
    "short",
    "1080x1920",
    "9x16",
)

_STOPWORDS = {
    "a",
    "al",
    "and",
    "como",
    "con",
    "de",
    "del",
    "el",
    "en",
    "for",
    "la",
    "las",
    "lo",
    "los",
    "para",
    "por",
    "que",
    "the",
    "una",
    "un",
    "used",
    "y",
}

_STRONG_TOPIC_TOKENS = {
    "casa",
    "casas",
    "comprar",
    "compra",
    "errores",
    "error",
    "inmobiliaria",
    "inmobiliario",
    "propiedad",
    "propiedades",
    "usada",
    "usadas",
    "vivienda",
    "viviendas",
    "checklist",
}

LOW_RELEVANCE_CONFIDENCE = "low"
MEDIUM_RELEVANCE_CONFIDENCE = "medium"
HIGH_RELEVANCE_CONFIDENCE = "high"


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _normalize_for_match(value: Any) -> str:
    text = unicodedata.normalize("NFKD", _clean_text(value).lower())
    return "".join(char for char in text if not unicodedata.combining(char))


def _allowed_dirs(extra_dirs: list[str] | None = None) -> tuple[Path, ...]:
    dirs = list(DEFAULT_PRODUCTION_VISUAL_DIRS)
    for item in extra_dirs or []:
        text = _clean_text(item)
        candidate = Path(text) if text else None
        # Callers cannot widen the production boundary.  Retain the argument
        # only for compatibility with callers that repeat a declared root.
        if candidate in DEFAULT_PRODUCTION_VISUAL_DIRS and candidate not in dirs:
            dirs.append(candidate)
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
    normalized_parts = tuple(part.lower() for part in candidate.parts)
    if any(part in _NON_PRODUCTION_PATH_PARTS or part.startswith("test-") for part in normalized_parts):
        return ""
    # Human-review output is a generated review artifact, never a source
    # library.  This blocks e.g. storage/local_videos/human-review/test-*.
    if "human-review" in normalized_parts:
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


def _keyword_values(value: Any) -> list[str]:
    if isinstance(value, dict):
        values: list[str] = []
        for item in value.values():
            values.extend(_keyword_values(item))
        return values
    if isinstance(value, (list, tuple, set)):
        values = []
        for item in value:
            values.extend(_keyword_values(item))
        return values
    return [_clean_text(value)] if _clean_text(value) else []


def _tokens_from_text(value: Any) -> set[str]:
    text = _normalize_for_match(value)
    return {
        token
        for token in re.findall(r"[a-z0-9]+", text)
        if len(token) >= 3 and token not in _STOPWORDS
    }


def _keyword_tokens(intent: dict[str, Any]) -> set[str]:
    values = [
        _clean_text(intent.get(key))
        for key in ("topic", "preset")
    ]
    values.extend(_keyword_values(intent.get("visual_keywords")))
    topic_plan = intent.get("topic_plan")
    if isinstance(topic_plan, dict):
        values.extend(_keyword_values(topic_plan.get("visual_keywords")))

    return _tokens_from_text(" ".join(value for value in values if value))


def _topic_tokens(intent: dict[str, Any]) -> set[str]:
    values = [_clean_text(intent.get("topic"))]
    topic_plan = intent.get("topic_plan")
    if isinstance(topic_plan, dict):
        values.extend(_keyword_values(topic_plan.get("visual_keywords")))
    values.extend(_keyword_values(intent.get("visual_keywords")))
    return _tokens_from_text(" ".join(value for value in values if value))


def _relevance_for_candidate(
    candidate: dict[str, Any],
    *,
    intent: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source_intent = intent or {}
    topic_tokens = sorted(_topic_tokens(source_intent))
    path_text = _normalize_for_match(candidate.get("path", ""))
    path_tokens = _tokens_from_text(path_text)
    matched_tokens = sorted(token for token in topic_tokens if token in path_tokens)
    strong_tokens = sorted(token for token in topic_tokens if token in _STRONG_TOPIC_TOKENS)
    strong_matches = sorted(token for token in matched_tokens if token in _STRONG_TOPIC_TOKENS)

    score = 0
    reasons: list[str] = []
    if matched_tokens:
        score += 18 * len(matched_tokens)
        reasons.append("filename_matches_topic_keywords")
    if strong_matches:
        score += 18 * len(strong_matches)
        reasons.append("filename_matches_strong_topic_keywords")

    if any(hint in path_text for hint in VERTICAL_NAME_HINTS):
        score += 8
        reasons.append("vertical_name_hint")

    if topic_tokens and not matched_tokens:
        score -= 25
        reasons.append("no_topic_keyword_match")
    if strong_tokens and not strong_matches:
        score -= 20
        reasons.append("no_strong_topic_keyword_match")

    if score >= 60:
        confidence = HIGH_RELEVANCE_CONFIDENCE
    elif score >= 25 or matched_tokens:
        confidence = MEDIUM_RELEVANCE_CONFIDENCE
    else:
        confidence = LOW_RELEVANCE_CONFIDENCE

    if not topic_tokens:
        confidence = MEDIUM_RELEVANCE_CONFIDENCE
        reasons.append("no_topic_keywords_available")

    return {
        "visual_relevance_score": score,
        "visual_relevance_confidence": confidence,
        "visual_relevance_reason": ", ".join(reasons) or "topic_keyword_check",
        "visual_relevance_matches": matched_tokens,
        "visual_relevance_strong_matches": strong_matches,
        "visual_relevance_topic_tokens": topic_tokens,
    }


def _candidate_score(
    candidate: dict[str, Any],
    *,
    intent: dict[str, Any] | None = None,
) -> int:
    score = 0
    reasons = candidate.setdefault("score_reasons", [])

    if candidate.get("type") == "video":
        score += 170
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

    relevance = _relevance_for_candidate(candidate, intent=intent)
    score += int(relevance["visual_relevance_score"])

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
        item.update(_relevance_for_candidate(item, intent=intent))
        scored.append(item)

    scored.sort(
        key=lambda item: (
            -int(item["score"]),
            int(item.get("size_bytes") or 0),
            item["path"].lower(),
        )
    )
    return scored[0]
