"""Deterministic, side-effect-free material candidate selection."""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from typing import Any, Iterable


@dataclass(frozen=True)
class MaterialSelectionOptions:
    video_aspect: str
    target_duration: float
    clip_duration: float
    recent_dedupe_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class MaterialSelectionDecision:
    candidate: Any
    orientation_score: int
    rank_score: int
    quality_score: int
    duration_score: int
    freshness_score: int
    diversity_adjustment: int
    total_score: int
    effective_duration: float
    used_recent_fallback: bool = False


@dataclass(frozen=True)
class MaterialSelectionResult:
    options: MaterialSelectionOptions
    decisions: tuple[MaterialSelectionDecision, ...]
    target_count: int
    selected_count: int
    shortfall: int
    used_recent_fallback: bool
    covered_terms: tuple[str, ...]
    selected_effective_duration: float


def _aspect_text(video_aspect: Any) -> str:
    return str(getattr(video_aspect, "value", video_aspect) or "")


def _target_orientation(video_aspect: Any) -> str | None:
    aspect = _aspect_text(video_aspect).replace(" ", "").replace("x", ":").lower()
    if aspect in {"9:16", "4:5", "vertical", "portrait"}:
        return "portrait"
    if aspect in {"16:9", "horizontal", "landscape", "wide"}:
        return "landscape"
    parts = aspect.split(":", 1)
    if len(parts) == 2:
        try:
            width, height = float(parts[0]), float(parts[1])
        except ValueError:
            return None
        if width > 0 and height > 0:
            if height > width:
                return "portrait"
            if width > height:
                return "landscape"
    return None


def _metadata_orientation(value: Any) -> str | None:
    orientation = str(value or "").replace(" ", "").replace("x", ":").lower()
    if not orientation:
        return None
    if any(token in orientation for token in ("portrait", "vertical", "9:16", "4:5")):
        return "portrait"
    if any(token in orientation for token in ("landscape", "horizontal", "wide", "16:9")):
        return "landscape"
    return None


def _positive_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _is_orientation_compatible(candidate: Any, video_aspect: str) -> bool:
    """Strictly match portrait/landscape targets using geometry first."""
    target = _target_orientation(video_aspect)
    if target is None:
        return True

    width = _positive_number(getattr(candidate, "width", None))
    height = _positive_number(getattr(candidate, "height", None))
    if width is not None and height is not None:
        if height > width:
            return target == "portrait"
        if width > height:
            return target == "landscape"
        return False

    orientation = _metadata_orientation(getattr(candidate, "orientation", None))
    return orientation == target


def orientation_score(candidate: Any, video_aspect: str) -> int:
    """Return a stable compatibility score without changing candidate metadata."""
    orientation = str(getattr(candidate, "orientation", "") or "").lower()
    width, height = getattr(candidate, "width", None), getattr(candidate, "height", None)
    if not orientation and isinstance(width, (int, float)) and isinstance(height, (int, float)):
        orientation = "portrait" if height > width else "landscape" if width > height else "square"
    aspect = _aspect_text(video_aspect).replace(" ", "").lower()
    normalized = orientation.replace("x", ":")
    square = "square" in orientation or normalized == "1:1"
    portrait = any(value in orientation for value in ("portrait", "vertical", "9:16", "4:5"))
    landscape = any(value in orientation for value in ("landscape", "horizontal", "16:9"))
    if aspect in {"9:16", "vertical", "portrait"}:
        if "9:16" in normalized or portrait and "4:5" not in normalized: return 40
        if "4:5" in normalized: return 32
        if square: return 18
        if landscape: return 4
    elif aspect in {"16:9", "horizontal", "landscape"}:
        if "16:9" in normalized or landscape: return 40
        if square: return 18
        if portrait: return 4
    elif aspect in {"1:1", "square"}:
        if square: return 40
        if portrait or landscape: return 12
    return 10


def _rank_score(rank: Any) -> int:
    try: return max(0, 30 - min(int(rank), 30))
    except (TypeError, ValueError): return 0


def _quality_score(candidate: Any) -> int:
    try: pixels = int(getattr(candidate, "width", 0) or 0) * int(getattr(candidate, "height", 0) or 0)
    except (TypeError, ValueError): pixels = 0
    return min(20, pixels // 250_000)


def _duration_score(candidate: Any, clip_duration: float) -> int:
    try: duration = float(getattr(candidate, "duration", 0) or 0)
    except (TypeError, ValueError): duration = 0
    if duration <= 0: return 10
    return min(20, int(20 * min(duration, clip_duration) / max(clip_duration, 0.1)))


def effective_duration(candidate: Any, clip_duration: float) -> float:
    try: duration = float(getattr(candidate, "duration", 0) or 0)
    except (TypeError, ValueError): duration = 0
    return min(duration, clip_duration) if duration > 0 else clip_duration


def _base(candidate: Any, options: MaterialSelectionOptions, recent: set[str]) -> tuple[int, int, int, int, int]:
    scores = (orientation_score(candidate, options.video_aspect), _rank_score(getattr(candidate, "rank", None)),
              _quality_score(candidate), _duration_score(candidate, options.clip_duration),
              -35 if getattr(candidate, "dedupe_key", "") in recent else 15)
    return scores


def select_material_candidates(*, discovery_result: Any, video_aspect: str, target_duration: float,
                               clip_duration: float, recent_dedupe_keys: Iterable[str] = ()) -> MaterialSelectionResult:
    """Choose a bounded, diverse set.  Candidate ordering is the final tie-break."""
    if clip_duration <= 0: raise ValueError("clip_duration must be positive")
    options = MaterialSelectionOptions(str(video_aspect), float(target_duration), float(clip_duration), tuple(recent_dedupe_keys))
    target = max(0, int(ceil(max(0.0, options.target_duration) / options.clip_duration)))
    recent, seen, decisions, providers = set(options.recent_dedupe_keys), set(), [], []
    candidates = [
        item for item in (getattr(discovery_result, "candidates", ()) or ())
        if _is_orientation_compatible(item, options.video_aspect)
    ]
    # First choose one candidate per term (when available), then fill capacity.
    terms = []
    for item in candidates:
        term = str(getattr(item, "search_term", "") or "")
        if term and term not in terms: terms.append(term)
    recent_fallback = False
    def choose(pool):
        nonlocal recent_fallback
        fresh = [(i, c) for i, c in pool if getattr(c, "dedupe_key", "") not in recent]
        if fresh:
            pool = fresh
        elif pool:
            recent_fallback = True
        if not pool:
            return False
        def key(pair):
            index, candidate = pair
            base = _base(candidate, options, recent)
            # A small penalty only breaks near-equivalent candidates; clear quality wins.
            diversity = -6 if getattr(candidate, "provider", "") in providers else 3
            return (-(sum(base) + diversity), index)
        index, chosen = min(pool, key=key)
        orientation, rank, quality, duration, freshness = _base(chosen, options, recent)
        diversity = -6 if getattr(chosen, "provider", "") in providers else 3
        fallback = getattr(chosen, "dedupe_key", "") in recent
        decisions.append(MaterialSelectionDecision(chosen, orientation, rank, quality, duration, freshness, diversity,
                                                   sum((orientation, rank, quality, duration, freshness, diversity)),
                                                   effective_duration(chosen, options.clip_duration), fallback))
        seen.add(getattr(chosen, "dedupe_key", "")); providers.append(getattr(chosen, "provider", ""))
        return True
    # Prefer fresh assets globally. This pass still gives every term one chance.
    for allow_recent in (False, True):
        for term in terms:
            if len(decisions) >= target: break
            pool = [(index, item) for index, item in enumerate(candidates)
                    if getattr(item, "dedupe_key", "") not in seen and getattr(item, "search_term", "") == term
                    and (allow_recent or getattr(item, "dedupe_key", "") not in recent)]
            choose(pool)
        while len(decisions) < target:
            pool = [(index, item) for index, item in enumerate(candidates)
                    if getattr(item, "dedupe_key", "") not in seen
                    and (allow_recent or getattr(item, "dedupe_key", "") not in recent)]
            if not choose(pool): break
        if len(decisions) >= target: break
    covered = tuple(dict.fromkeys(str(getattr(d.candidate, "search_term", "") or "") for d in decisions if getattr(d.candidate, "search_term", "")))
    return MaterialSelectionResult(options, tuple(decisions), target, len(decisions), max(0, target-len(decisions)),
                                   recent_fallback, covered, sum(item.effective_duration for item in decisions))
