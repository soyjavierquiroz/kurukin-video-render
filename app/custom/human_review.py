"""Filesystem-backed human review plans for batch video production."""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping
from urllib.parse import urljoin, urlparse

import requests

from app.custom.asset_search_v2 import (
    build_visual_queries_v2,
    normalize_editorial_profile,
)
from app.custom.material_discovery import MaterialCandidate
from app.custom.material_selection import MaterialSelectionDecision
from app.custom.candidate_ranking_v2 import (
    candidate_editorial_evidence,
    candidate_identity_keys,
    rank_candidates_v2,
    stable_secondary_dedupe,
)
from app.custom.scene_visual_intent import (
    build_scene_visual_intent,
    build_scene_retrieval_queries,
    inherited_subject_preference,
)
from app.models.schema import MaterialInfo
from app.utils import utils


SCHEMA_VERSION = 2
STATUS_PENDING = "pending_review"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_FAILED = "failed"
RENDER_MODE = "human_review_batch"
MAX_PREVIEW_BYTES = 2 * 1024 * 1024

# Human Review scene-duration policy.
#
# 0.90x is the ordinary slowdown floor.
# 0.85x is allowed only when it avoids an unnecessary visual cut.
# Backups shorter than 0.75s are avoided because they read as flashes.
PREFERRED_PLAYBACK_SPEED = 0.90
HARD_MIN_PLAYBACK_SPEED = 0.85
MIN_BACKUP_OUTPUT_SECONDS = 0.75
# Coverage is persisted for display, but it is never authoritative.  Keep a
# small tolerance for the rounded values stored in production plans.
COVERAGE_TOLERANCE_SECONDS = 0.01
# The renderer can safely hold a scene's final rendered piece for a short
# in-scene overrun.  This is never used to fill an unsegmented timeline tail.
MAX_SEGMENT_FREEZE_SECONDS = 1.25
# Kept as a recipe-fingerprint input for already-produced plans.  Timeline
# tails are no longer auto-filled, so the effective policy is zero seconds.
MAX_TIMELINE_AUTOFILL_SECONDS = 0.0
_LOG = logging.getLogger(__name__)
PREVIEW_KEYS = (
    "thumbnail_url", "thumbnail", "preview_url", "preview", "poster_url", "poster",
    "image_url", "image", "keyframe_url", "keyframe", "cover_url", "cover",
    "source_thumbnail_url", "source_thumbnail",
)
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv"}
FLIP_HORIZONTAL_DEFAULT = True
GENDER_TEXT_FIELDS = (
    "asset_uid",
    "canonical_id",
    "dedupe_key",
    "filename",
    "title",
    "name",
    "description",
    "alt",
    "search_term",
    "term",
    "query",
)
FEMININE_SIGNALS = {
    "femenina", "femenino", "hermana", "hija", "madre", "mama", "mamá",
    "mujer", "mujeres", "nina", "ninas", "niña", "niñas", "senora",
    "señora", "woman", "women", "girl", "girls", "mother", "sister",
    "daughter", "female",
}
MASCULINE_SIGNALS = {
    "ellos", "masculina", "masculino", "hermano", "hijo", "hombre",
    "hombres", "nino", "ninos", "niño", "niños", "padre", "papa", "papá",
    "senor", "señor", "man", "men", "boy", "boys", "father", "brother",
    "son", "male",
}


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def review_root(project_root: str | Path | None = None) -> Path:
    if project_root is not None:
        return Path(project_root) / "storage" / "review_queue"
    return Path(utils.storage_dir("review_queue"))


def plan_path(batch_id: str, stem: str, project_root: str | Path | None = None) -> Path:
    return review_root(project_root) / batch_id / stem / "production-plan.json"


def nightly_queue_pending_dir(project_root: str | Path | None = None) -> Path:
    root = Path(project_root) if project_root is not None else Path(utils.root_dir())
    return root / "storage" / "nightly_jobs" / "pending"


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{time.time_ns()}")
    try:
        with tmp.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "model_dump"):
        return dict(value.model_dump())
    if hasattr(value, "dict"):
        return dict(value.dict())
    return dict(getattr(value, "__dict__", {}) or {})


def _safe_metadata(candidate: Any) -> dict[str, Any]:
    source_info = getattr(candidate, "source_info", None)
    metadata = dict(source_info) if isinstance(source_info, dict) else {}
    for key in ("duration", "width", "height", "orientation", "filename"):
        value = getattr(candidate, key, None)
        if value not in (None, ""):
            metadata.setdefault(key, value)
    return {
        str(key): value
        for key, value in metadata.items()
        if not any(secret in str(key).lower() for secret in ("token", "secret", "password", "credential", "authorization"))
    }


def _fold_text(text: Any) -> str:
    return re.sub(
        r"\s+",
        " ",
        utils.normalize_script_for_subtitle_matching(str(text or "")),
    ).strip().lower()


def _asset_gender_text(asset: Any) -> str:
    values: list[str] = []

    def add_mapping(mapping: Mapping[str, Any]) -> None:
        for key in GENDER_TEXT_FIELDS:
            value = mapping.get(key)
            if value not in (None, ""):
                values.append(str(value))

    if isinstance(asset, Mapping):
        add_mapping(asset)
        metadata = asset.get("metadata")
        if isinstance(metadata, Mapping):
            add_mapping(metadata)
        search = asset.get("search")
        if isinstance(search, Mapping):
            add_mapping(search)
    else:
        for key in GENDER_TEXT_FIELDS:
            value = getattr(asset, key, None)
            if value not in (None, ""):
                values.append(str(value))
        source_info = getattr(asset, "source_info", None)
        if isinstance(source_info, Mapping):
            add_mapping(source_info)

    return _fold_text(" ".join(values))


def _asset_gender_counts(asset: Any) -> tuple[int, int]:
    tokens = set(re.findall(r"[\w]+", _asset_gender_text(asset), flags=re.UNICODE))
    feminine = len(tokens & {_fold_text(item) for item in FEMININE_SIGNALS})
    masculine = len(tokens & {_fold_text(item) for item in MASCULINE_SIGNALS})
    return feminine, masculine


def editorial_gender_evidence(asset: Any) -> str:
    feminine, masculine = _asset_gender_counts(asset)
    if feminine and masculine:
        return "mixed"
    if feminine:
        return "feminine"
    if masculine:
        return "masculine"
    return "unknown"


def _candidate_matches_editorial_profile(
    candidate: Any,
    editorial_profile: Mapping[str, Any] | None,
) -> bool:
    # Conservative temporary policy until Asset Hub exposes structured subject
    # metadata. Strict jobs require positive textual evidence and reject
    # contrary or ambiguous evidence from the metadata we already receive.
    subject_gender = normalize_editorial_profile(editorial_profile).get("subject_gender")
    if subject_gender in ("neutral", "mixed", None):
        return True
    evidence = editorial_gender_evidence(candidate)
    if subject_gender == "feminine":
        return evidence == "feminine"
    if subject_gender == "masculine":
        return evidence == "masculine"
    return True


def _editorial_rank_adjustment(
    candidate: Any,
    editorial_profile: Mapping[str, Any] | None,
) -> int:
    subject_gender = normalize_editorial_profile(editorial_profile).get("subject_gender")
    feminine, masculine = _asset_gender_counts(candidate)
    score = feminine - masculine
    if subject_gender == "feminine":
        return score
    if subject_gender == "masculine":
        return -score
    return 0


def candidate_uid(candidate: Any) -> str:
    return str(getattr(candidate, "canonical_id", "") or getattr(candidate, "dedupe_key", "") or "")


def serialize_candidate(candidate: Any, decision: Any | None = None, thumbnail_path: str = "") -> dict[str, Any]:
    ranking: dict[str, Any] = {}
    if decision is not None:
        for key in (
            "orientation_score",
            "rank_score",
            "quality_score",
            "duration_score",
            "freshness_score",
            "diversity_adjustment",
            "total_score",
            "effective_duration",
        ):
            value = getattr(decision, key, None)
            if value is not None:
                ranking[key] = value
    return {
        "asset_uid": candidate_uid(candidate),
        "source": str(getattr(candidate, "provider", "") or ""),
        "provider": str(getattr(candidate, "provider", "") or ""),
        "canonical_id": str(getattr(candidate, "canonical_id", "") or ""),
        "dedupe_key": str(getattr(candidate, "dedupe_key", "") or ""),
        "url": str(getattr(candidate, "url", "") or ""),
        "thumbnail_path": thumbnail_path,
        "material_ref": {
            "type": "url" if str(getattr(candidate, "url", "") or "").startswith(("http://", "https://")) else "local",
            "value": str(getattr(candidate, "url", "") or ""),
        },
        "metadata": _safe_metadata(candidate),
        "search": {
            "term": str(getattr(candidate, "search_term", "") or ""),
            "rank": getattr(candidate, "rank", None),
        },
        "ranking": ranking,
        "flip_horizontal": FLIP_HORIZONTAL_DEFAULT,
    }


def _serialize_v2_ranking(asset: dict[str, Any], ranking: Any | None) -> None:
    if ranking is None:
        return
    asset["ranking_v2"] = {
        "score": ranking.total_score,
        "score_components": ranking.score_components,
        "reason_codes": list(ranking.reason_codes),
        "penalty_codes": list(ranking.penalty_codes),
    }


def _unique_candidates_by_uid(candidates: list[Any]) -> list[Any]:
    unique = []
    seen = set()
    for candidate in candidates:
        uid = candidate_uid(candidate)
        if not uid or uid in seen:
            continue
        seen.add(uid)
        unique.append(candidate)
    return unique


def _unique_review_assets(assets: list[Any]) -> list[Any]:
    """Preserve order while removing duplicate Asset Hub source identities."""
    return stable_secondary_dedupe(assets)


def _ranked_segment_candidates(
    candidate: Any,
    all_candidates: list[Any],
    preferred_terms: list[str] | tuple[str, ...] | None = None,
    editorial_profile: Mapping[str, Any] | None = None,
    retrieval_queries: Mapping[str, tuple[str, ...]] | None = None,
) -> list[Any]:
    """
    Rank candidates for one scene.

    Use the segment's own retrieved subset first.  A global fallback is only
    used on real scarcity; ranking still applies its compatibility gate.
    """

    candidate_key = getattr(
        candidate,
        "dedupe_key",
        "",
    )

    candidate_term = str(
        getattr(
            candidate,
            "search_term",
            "",
        )
        or ""
    )

    preferred = [
        str(term or "").strip()
        for term in (preferred_terms or ())
        if str(term or "").strip()
    ]
    for queries in (retrieval_queries or {}).values():
        preferred.extend(str(query or "").strip() for query in queries if str(query or "").strip())
    preferred = list(dict.fromkeys(preferred))

    preferred_matches = [
        item
        for item in all_candidates
        if getattr(
            item,
            "dedupe_key",
            "",
        ) != candidate_key
        and str(
            getattr(
                item,
                "search_term",
                "",
            )
            or ""
        ) in preferred
    ]

    same_term = [
        item
        for item in all_candidates
        if getattr(
            item,
            "dedupe_key",
            "",
        ) != candidate_key
        and str(
            getattr(
                item,
                "search_term",
                "",
            )
            or ""
        ) == candidate_term
        and item not in preferred_matches
    ]

    remaining = [
        item
        for item in all_candidates
        if getattr(
            item,
            "dedupe_key",
            "",
        ) != candidate_key
        and item not in same_term
        and item not in preferred_matches
    ]

    local = _unique_candidates_by_uid(preferred_matches + [candidate] + same_term)
    # Do not indiscriminately feed global candidates to every segment.  The
    # caller can use this fallback only when the local subset is scarce.
    ranked = local if len(local) >= 4 else _unique_candidates_by_uid(local + remaining)
    indexed = list(enumerate(ranked))
    indexed.sort(
        key=lambda item: (
            -_editorial_rank_adjustment(item[1], editorial_profile),
            item[0],
        )
    )
    return [item for _index, item in indexed]


def _visible_editorial_candidates(
    candidates: list[Any],
    editorial_profile: Mapping[str, Any] | None,
) -> list[Any]:
    return [
        candidate
        for candidate in candidates
        if _candidate_matches_editorial_profile(candidate, editorial_profile)
    ]


EDITORIAL_TIERS = ("POSITIVE", "UNKNOWN", "MISMATCH / CONTRADICTION")


def _allocation_editorial_tier(intent: Any, candidate: Any, ranking: Any | None) -> str:
    """Classify an already-ranked candidate without introducing a new score."""
    if ranking is not None and "explicit_narrative_contradiction" in ranking.penalty_codes:
        return "MISMATCH / CONTRADICTION"
    if candidate_editorial_evidence(intent, candidate) > 0:
        return "POSITIVE"
    return "UNKNOWN"


def _tier_counts(candidates: list[Any], tier_for: Callable[[Any], str]) -> dict[str, int]:
    counts = {tier: 0 for tier in EDITORIAL_TIERS}
    seen: set[str] = set()
    for candidate in candidates:
        uid = candidate_uid(candidate)
        if not uid or uid in seen:
            continue
        seen.add(uid)
        counts[tier_for(candidate)] += 1
    return counts


def _select_segment_candidate(
    ranked_candidates: list[Any],
    used_selected_asset_uids: set[str],
    used_selected_identity_keys: set[str] | None = None,
    *,
    is_review_previewable: Callable[[Any], bool] | None = None,
    is_primary_eligible: Callable[[Any], bool] | None = None,
) -> tuple[Any, bool]:
    # V2 has already established the editorial order.  Previewability is an
    # actionability constraint for Human Review, so use that same order within
    # the inspectable subset rather than treating it as a ranking signal.
    candidates = ranked_candidates
    if is_primary_eligible is not None:
        non_mismatch = [item for item in candidates if is_primary_eligible(item)]
        candidates = non_mismatch or candidates
    if is_review_previewable is not None:
        previewable = [item for item in candidates if is_review_previewable(item)]
        candidates = previewable or candidates
    for candidate in candidates:
        uid = candidate_uid(candidate)
        identity_keys = candidate_identity_keys(candidate)
        if (
            uid
            and uid not in used_selected_asset_uids
            and not (used_selected_identity_keys and any(key in used_selected_identity_keys for key in identity_keys))
        ):
            return candidate, False
    return None, False


def _alternative_candidates(
    ranked_candidates: list[Any],
    selected_uid: str,
    used_selected_asset_uids: set[str],
) -> list[Any]:
    candidates = [item for item in ranked_candidates if candidate_uid(item) != selected_uid]
    fresh = [item for item in candidates if candidate_uid(item) not in used_selected_asset_uids]
    repeated = [item for item in candidates if candidate_uid(item) in used_selected_asset_uids]
    return (fresh + repeated)[:3]



def _asset_uid_value(asset: Mapping[str, Any] | None) -> str:
    if not isinstance(asset, Mapping):
        return ""
    return str(
        asset.get("asset_uid")
        or asset.get("canonical_id")
        or ""
    ).strip()


def asset_flip_horizontal(asset: Mapping[str, Any] | None) -> bool:
    if not isinstance(asset, Mapping):
        return FLIP_HORIZONTAL_DEFAULT
    value = asset.get("flip_horizontal", FLIP_HORIZONTAL_DEFAULT)
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off"}
    return bool(value)


def _normalize_asset_editorial_fields(asset: Any) -> None:
    if isinstance(asset, dict) and "flip_horizontal" not in asset:
        asset["flip_horizontal"] = FLIP_HORIZONTAL_DEFAULT


def normalize_plan_editorial_fields(plan: dict[str, Any]) -> dict[str, Any]:
    for segment in plan.get("segments") or []:
        if not isinstance(segment, dict):
            continue
        _normalize_asset_editorial_fields(segment.get("selected_asset"))
        _normalize_asset_editorial_fields(segment.get("original_selected_asset"))
        for key in ("alternatives", "backup_assets"):
            for asset in segment.get(key) or []:
                _normalize_asset_editorial_fields(asset)
    return plan


def _iter_visible_editable_assets(plan: dict[str, Any]):
    for segment in plan.get("segments") or []:
        if not isinstance(segment, dict):
            continue
        segment_id = str(segment.get("segment_id") or "")
        selected = segment.get("selected_asset")
        if isinstance(selected, dict):
            yield segment_id, selected
        for key in ("alternatives", "backup_assets"):
            for asset in segment.get(key) or []:
                if isinstance(asset, dict):
                    yield segment_id, asset


def set_asset_flip_horizontal(
    plan_file: Path,
    segment_id: str,
    asset_uid: str,
    enabled: bool,
) -> dict[str, Any]:
    plan = normalize_plan_editorial_fields(read_json(plan_file))

    if plan.get("review_status") == STATUS_APPROVED:
        raise ValueError("approved production plans are frozen")

    changed = False
    for current_segment_id, asset in _iter_visible_editable_assets(plan):
        if current_segment_id != segment_id:
            continue
        if _asset_uid_value(asset) != asset_uid:
            continue
        asset["flip_horizontal"] = bool(enabled)
        changed = True

    if not changed:
        raise ValueError(f"asset {asset_uid} is not available for {segment_id}")

    refresh_plan_coverage(plan)
    plan["updated_at"] = utc_timestamp()
    write_json_atomic(plan_file, plan)
    return plan


def set_all_visible_flip_horizontal(
    plan_file: Path,
    enabled: bool,
) -> dict[str, Any]:
    plan = normalize_plan_editorial_fields(read_json(plan_file))

    if plan.get("review_status") == STATUS_APPROVED:
        raise ValueError("approved production plans are frozen")

    for _segment_id, asset in _iter_visible_editable_assets(plan):
        asset["flip_horizontal"] = bool(enabled)

    refresh_plan_coverage(plan)
    plan["updated_at"] = utc_timestamp()
    write_json_atomic(plan_file, plan)
    return plan


def _unique_assets_by_uid(assets: list[Any]) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()

    for asset in assets:
        if not isinstance(asset, dict):
            continue

        _normalize_asset_editorial_fields(asset)
        uid = _asset_uid_value(asset)
        if not uid or uid in seen:
            continue

        seen.add(uid)
        unique.append(asset)

    return unique



def _authorized_asset_location(
    plan: Mapping[str, Any],
    asset_uid: str,
    *,
    exclude_segment_id: str = "",
) -> str | None:
    uid = str(asset_uid or "").strip()
    if not uid:
        return None

    for segment in plan.get("segments") or []:
        if not isinstance(segment, Mapping):
            continue

        segment_id = str(segment.get("segment_id") or "")

        if exclude_segment_id and segment_id == exclude_segment_id:
            continue

        assets = [
            segment.get("selected_asset"),
            *list(segment.get("backup_assets") or []),
        ]

        for asset in assets:
            if (
                isinstance(asset, Mapping)
                and _asset_uid_value(asset) == uid
            ):
                return segment_id or "<unknown>"

    return None


def authorized_asset_location(
    plan: Mapping[str, Any],
    asset_uid: str,
    *,
    exclude_segment_id: str = "",
) -> str | None:
    """Return the other segment that already authorizes ``asset_uid``.

    Alternatives do not authorize an asset; only a PRIMARY or BACKUP does.
    Keep Review UI actions on this same authorization rule.
    """
    return _authorized_asset_location(
        plan,
        asset_uid,
        exclude_segment_id=exclude_segment_id,
    )


def _usable_asset_duration(
    asset: Mapping[str, Any] | None,
    segment_duration: float,
) -> float:
    if not isinstance(asset, Mapping):
        return 0.0

    duration = _asset_source_duration(asset)

    if duration <= 0:
        return 0.0

    try:
        cap = float(segment_duration or 0)
    except (TypeError, ValueError):
        cap = 0.0

    if cap > 0:
        return min(duration, cap)

    return duration


def _asset_source_duration(asset: Mapping[str, Any] | None) -> float:
    """Return the frozen asset duration used by the renderer timeline.

    Human Review's canonical plan representation stores source metadata under
    ``asset.metadata``.  In particular, do not substitute a scene target for
    a missing source duration: doing so would make cached coverage appear
    valid without any renderable media behind it.
    """
    if not isinstance(asset, Mapping):
        return 0.0
    metadata = asset.get("metadata")
    if not isinstance(metadata, Mapping):
        return 0.0
    try:
        duration = float(metadata.get("duration") or 0)
    except (TypeError, ValueError):
        return 0.0
    return duration if duration > 0 else 0.0


def coverage_summary(
    plan: Mapping[str, Any],
) -> dict[str, float]:
    """
    Coverage is derived from the exact renderer timeline.

    This prevents Human Review and production from disagreeing about
    slowdown, backup usage or scene duration.
    """

    try:
        audio_duration = float(
            plan.get("duration") or 0
        )
    except (TypeError, ValueError):
        audio_duration = 0.0
    try:
        timeline = render_timeline_from_plan(
            plan
        )
    except ValueError:
        required_duration = max(0.0, audio_duration + 0.10)
        return {
            "audio_duration": round(audio_duration, 3),
            "target_duration": round(required_duration, 3),
            "required_duration": round(required_duration, 3),
            "primary_duration": 0.0,
            "backup_duration": 0.0,
            "slowdown_gain": 0.0,
            "approved_duration": 0.0,
            "covered_duration": 0.0,
            "missing_duration": round(required_duration, 3),
            "deficit": round(required_duration, 3),
            "coverage_ratio": 0.0,
        }

    primary_duration = sum(
        float(piece["output_duration"])
        for piece in timeline.pieces
        if piece["role"] == "PRIMARY"
    )

    backup_duration = sum(
        float(piece["output_duration"])
        for piece in timeline.pieces
        if piece["role"] == "BACKUP"
    )

    recovery_duration = sum(
        float(piece["output_duration"])
        for piece in timeline.pieces
        if piece["role"] in {"FREEZE", "EXTEND", "LOOP"}
    )

    slowdown_gain = sum(
        max(
            0.0,
            float(piece["output_duration"])
            - float(piece["source_duration"]),
        )
        for piece in timeline.pieces
        if piece["role"] == "PRIMARY"
    )

    approved_duration = (
        primary_duration
        + backup_duration
        + recovery_duration
    )

    required_duration = float(
        timeline.required_duration
    )

    deficit = max(
        0.0,
        required_duration
        - approved_duration,
    )

    # render_timeline also knows about structural/tail shortfalls.
    deficit = max(
        deficit,
        float(timeline.shortfall),
    )

    coverage_ratio = (
        approved_duration / required_duration
        if required_duration > 0
        else 1.0
    )

    return {
        "audio_duration": round(
            audio_duration,
            3,
        ),
        "target_duration": round(
            required_duration,
            3,
        ),
        "required_duration": round(
            required_duration,
            3,
        ),
        "primary_duration": round(
            primary_duration,
            3,
        ),
        "backup_duration": round(
            backup_duration,
            3,
        ),
        "slowdown_gain": round(
            slowdown_gain,
            3,
        ),
        "approved_duration": round(
            approved_duration,
            3,
        ),
        "covered_duration": round(
            approved_duration,
            3,
        ),
        "missing_duration": round(
            deficit,
            3,
        ),
        "deficit": round(
            deficit,
            3,
        ),
        "coverage_ratio": round(
            coverage_ratio,
            4,
        ),
    }


def segment_coverage_metrics(plan: Mapping[str, Any]) -> dict[str, dict[str, float]]:
    try:
        timeline = render_timeline_from_plan(plan)
    except ValueError:
        timeline = SimpleNamespace(pieces=(), segment_shortfalls=())
    segments = [
        segment
        for segment in (plan.get("segments") or [])
        if isinstance(segment, Mapping)
    ]
    shortfalls = {
        str(item["segment_id"]): float(item["shortfall"])
        for item in timeline.segment_shortfalls
    }
    covered: dict[str, float] = {}
    for piece in timeline.pieces:
        segment_id = str(piece["segment_id"])
        covered[segment_id] = covered.get(segment_id, 0.0) + float(
            piece["output_duration"]
        )

    metrics: dict[str, dict[str, float]] = {}
    for index, segment in enumerate(segments):
        segment_id = str(segment.get("segment_id") or f"segment-{index + 1:03d}")
        try:
            target = float(segment.get("duration") or 0)
        except (TypeError, ValueError):
            target = 0.0
        if index == len(segments) - 1:
            target += 0.10
        missing = max(0.0, float(shortfalls.get(segment_id, 0.0)))
        scene_covered = max(0.0, float(covered.get(segment_id, 0.0)))
        metrics[segment_id] = {
            "target_duration": round(target, 3),
            "covered_duration": round(scene_covered, 3),
            "missing_duration": round(missing, 3),
        }
    try:
        required_duration = max(0.0, float(plan.get("duration") or 0) + 0.10)
    except (TypeError, ValueError):
        required_duration = 0.0
    scene_target = sum(item["target_duration"] for item in metrics.values())
    tail = max(0.0, required_duration - scene_target)
    if tail > 0.01:
        tail_covered = sum(
            float(piece["output_duration"])
            for piece in timeline.pieces
            if piece["segment_id"] == "timeline-tail"
        )
        metrics["timeline-tail"] = {
            "target_duration": round(tail, 3),
            "covered_duration": round(tail_covered, 3),
            "missing_duration": round(max(0.0, tail - tail_covered), 3),
        }
    return metrics


def apply_segment_coverage_metrics(plan: dict[str, Any]) -> dict[str, Any]:
    metrics = segment_coverage_metrics(plan)
    for index, segment in enumerate(plan.get("segments") or []):
        if not isinstance(segment, dict):
            continue
        segment_id = str(segment.get("segment_id") or f"segment-{index + 1:03d}")
        segment["coverage"] = metrics.get(
            segment_id,
            {
                "target_duration": 0.0,
                "covered_duration": 0.0,
                "missing_duration": 0.0,
            },
        )
    return plan


def refresh_plan_coverage(plan: dict[str, Any]) -> dict[str, Any]:
    plan["coverage"] = coverage_summary(plan)
    return apply_segment_coverage_metrics(plan)


def resolve_human_review_segment_duration(
    segment: Mapping[str, Any],
    target_duration: float,
) -> dict[str, Any]:
    """Resolve one approved segment using the Human Review timeline policy.

    This is deliberately the single source of truth for duration feasibility:
    callers receive the pieces the renderer will use and any remaining
    shortfall.  It never selects an alternative; only the frozen primary and
    explicitly approved backups are considered.
    """
    target = max(0.0, float(target_duration or 0))
    primary = segment.get("selected_asset")
    if not isinstance(primary, Mapping) or not _asset_uid_value(primary):
        return {"pieces": [], "shortfall": target}

    available = _asset_source_duration(primary)
    if available <= 0:
        return {"pieces": [], "shortfall": target}

    def piece(
        role: str,
        asset: Mapping[str, Any],
        source_duration: float,
        output_duration: float,
        playback_speed: float,
    ) -> dict[str, Any]:
        return {
            "role": role,
            "asset": asset,
            "source_duration": source_duration,
            "output_duration": output_duration,
            "playback_speed": playback_speed,
        }

    if available >= target:
        return {"pieces": [piece("PRIMARY", primary, target, target, 1.0)], "shortfall": 0.0}

    required_speed = available / target if target > 0 else 1.0
    if required_speed >= HARD_MIN_PLAYBACK_SPEED:
        return {
            "pieces": [piece("PRIMARY", primary, available, target, required_speed)],
            "shortfall": 0.0,
        }

    backups = _unique_assets_by_uid(list(segment.get("backup_assets") or []))
    primary_output = min(target, available / PREFERRED_PLAYBACK_SPEED)
    remaining = max(0.0, target - primary_output)
    usable_backups = [
        backup for backup in backups
        if _asset_source_duration(backup) >= MIN_BACKUP_OUTPUT_SECONDS
    ]

    # A short remainder becomes a proper backup cut when reducing the primary
    # slowdown can do so within the same hard floor.
    if (
        usable_backups
        and 0.0001 < remaining < MIN_BACKUP_OUTPUT_SECONDS
        and target > MIN_BACKUP_OUTPUT_SECONDS
    ):
        desired_primary_output = target - MIN_BACKUP_OUTPUT_SECONDS
        if desired_primary_output >= available:
            candidate_speed = available / desired_primary_output
            if HARD_MIN_PLAYBACK_SPEED <= candidate_speed <= 1.0:
                primary_output = desired_primary_output
                remaining = MIN_BACKUP_OUTPUT_SECONDS

    pieces = [piece(
        "PRIMARY", primary, available, primary_output,
        available / primary_output if primary_output > 0 else 1.0,
    )]
    for backup in usable_backups:
        if remaining <= 0.0001:
            break
        use = min(_asset_source_duration(backup), remaining)
        if use < MIN_BACKUP_OUTPUT_SECONDS:
            continue
        pieces.append(piece("BACKUP", backup, use, use, 1.0))
        remaining -= use

    if 0.0001 < remaining <= MAX_SEGMENT_FREEZE_SECONDS and pieces:
        final_piece = pieces[-1]
        pieces.append(piece(
            "FREEZE", final_piece["asset"], 0.04, remaining, 1.0,
        ) | {"freeze_seconds": remaining, "source_start": max(0.0, _asset_source_duration(final_piece["asset"]) - 0.04)})
        remaining = 0.0

    return {"pieces": pieces, "shortfall": max(0.0, remaining)}


def can_resolve_human_review_segment_duration(
    segment: Mapping[str, Any],
    target_duration: float,
) -> bool:
    """Whether the current frozen approvals can span this segment."""
    return resolve_human_review_segment_duration(segment, target_duration)["shortfall"] == 0.0


def validate_approved_plan_integrity(
    plan: Mapping[str, Any],
    *,
    require_approved: bool = True,
) -> dict[str, Any]:
    """Recompute and validate the frozen evidence behind an approved plan.

    The stored coverage summary is a cache for the review UI.  This validator
    deliberately derives its result with the same timeline helpers used by
    Human Review and rendering, so it cannot certify a plan from stale cache
    values alone.
    """
    errors: list[str] = []
    if require_approved and plan.get("review_status") != STATUS_APPROVED:
        errors.append("review_status is not approved")

    segments = plan.get("segments") or []
    if not segments:
        errors.append("production plan has no segments")

    valid_segments = [item for item in segments if isinstance(item, Mapping)]
    if len(valid_segments) != len(segments):
        errors.append("production plan contains an invalid segment")

    for index, segment in enumerate(valid_segments):
        segment_id = str(segment.get("segment_id") or f"segment-{index + 1:03d}")
        try:
            target = float(segment.get("duration") or 0)
        except (TypeError, ValueError):
            target = 0.0
        if target <= 0:
            errors.append(f"{segment_id} has no usable target_duration")

        primary = segment.get("selected_asset")
        primary_uid = _asset_uid_value(primary)
        if not isinstance(primary, Mapping) or not primary_uid:
            errors.append(f"{segment_id} has no selected_asset")
            continue

        primary_duration = _asset_source_duration(primary)
        if primary_duration <= 0:
            errors.append(
                f"{segment_id} primary {primary_uid} has no usable duration"
            )

        for backup in segment.get("backup_assets") or []:
            backup_uid = _asset_uid_value(backup) if isinstance(backup, Mapping) else ""
            if not isinstance(backup, Mapping) or not backup_uid:
                errors.append(f"{segment_id} has an invalid backup_asset")
            elif _asset_source_duration(backup) <= 0:
                errors.append(
                    f"{segment_id} backup {backup_uid} has no usable duration"
                )

    try:
        recomputed_coverage = coverage_summary(plan)
        segment_metrics = segment_coverage_metrics(plan)
    except (TypeError, ValueError) as exc:
        errors.append(f"coverage recomputation failed: {exc}")
        recomputed_coverage = {}
        segment_metrics = {}

    if recomputed_coverage:
        stored_coverage = plan.get("coverage")
        if isinstance(stored_coverage, Mapping):
            for key in ("target_duration", "covered_duration", "missing_duration"):
                try:
                    stored = float(stored_coverage.get(key))
                    recomputed = float(recomputed_coverage[key])
                except (KeyError, TypeError, ValueError):
                    errors.append(f"stored coverage has invalid {key}")
                    continue
                if abs(stored - recomputed) > COVERAGE_TOLERANCE_SECONDS:
                    _LOG.info(
                        "COVERAGE REFRESH stored_missing=%.3f recomputed_missing=%.3f",
                        float(stored_coverage.get("missing_duration") or 0),
                        float(recomputed_coverage["missing_duration"]),
                    )

        segment_target = sum(
            float(metrics["target_duration"])
            for metrics in segment_metrics.values()
        )
        segment_missing = sum(
            float(metrics["missing_duration"])
            for metrics in segment_metrics.values()
        )
        if abs(segment_target - float(recomputed_coverage["target_duration"])) > COVERAGE_TOLERANCE_SECONDS:
            errors.append(
                "segment target coverage differs from global target: "
                f"segments={segment_target:.3f} global={float(recomputed_coverage['target_duration']):.3f}"
            )
        if abs(segment_missing - float(recomputed_coverage["missing_duration"])) > COVERAGE_TOLERANCE_SECONDS:
            errors.append(
                "segment missing coverage differs from global missing: "
                f"segments={segment_missing:.3f} global={float(recomputed_coverage['missing_duration']):.3f}"
            )
        # Coverage is a cached accounting view.  Renderability is instead
        # determined segment-by-segment by the same resolver as the renderer.
        for index, segment in enumerate(valid_segments):
            target = float(segment.get("duration") or 0)
            if index == len(valid_segments) - 1:
                target += 0.10
            if target > 0 and not can_resolve_human_review_segment_duration(segment, target):
                shortfall = resolve_human_review_segment_duration(segment, target)["shortfall"]
                errors.append(
                    f"insufficient approved visual coverage: {shortfall:.2f}s missing in "
                    f"{segment.get('segment_id') or f'segment-{index + 1:03d}'}"
                )
        tail = segment_metrics.get("timeline-tail", {}).get("missing_duration", 0.0)
        if float(tail) > COVERAGE_TOLERANCE_SECONDS:
            errors.append(f"insufficient approved visual coverage: {float(tail):.2f}s timeline tail")

    return {
        "ok": not errors,
        "errors": errors,
        "coverage": recomputed_coverage,
        "segment_coverage": segment_metrics,
        "stored_coverage": plan.get("coverage"),
    }


def validate_plan_for_approval(
    plan: Mapping[str, Any],
    *,
    allow_insufficient_coverage: bool = False,
) -> tuple[list[str], dict[str, float]]:
    errors: list[str] = []

    # No approved asset may appear twice anywhere in the same video.
    seen_authorized: dict[str, str] = {}

    segments = plan.get("segments") or []
    if not segments:
        errors.append("production plan has no segments")

    for segment in segments:
        if not isinstance(segment, Mapping):
            errors.append("production plan contains an invalid segment")
            continue

        segment_id = str(segment.get("segment_id") or "<unknown>")
        primary = segment.get("selected_asset")

        primary_uid = _asset_uid_value(primary)
        if not primary_uid:
            errors.append(
                f"{segment_id}: PRIMARY has no asset_uid"
            )
            continue

        backups = [
            item
            for item in segment.get("backup_assets") or []
            if isinstance(item, Mapping)
        ]

        local_seen: set[str] = set()

        authorized = [
            ("PRIMARY", primary),
            *[("BACKUP", item) for item in backups],
        ]

        for role, asset in authorized:
            uid = _asset_uid_value(asset)

            if not uid:
                errors.append(
                    f"{segment_id}: {role} has no asset_uid"
                )
                continue

            if uid in local_seen:
                errors.append(
                    f"{segment_id}: duplicate authorized asset_uid {uid}"
                )
                continue

            local_seen.add(uid)

            previous = seen_authorized.get(uid)
            if previous is not None:
                errors.append(
                    f"{segment_id}: asset {uid} is already authorized "
                    f"in {previous}"
                )
            else:
                seen_authorized[uid] = segment_id

    coverage = coverage_summary(plan)

    if (
        coverage["required_duration"] > 0
        and coverage["deficit"] > 0.01
        and not allow_insufficient_coverage
    ):
        errors.append(
            "insufficient approved visual coverage: "
            f"{coverage['approved_duration']:.2f}s available, "
            f"{coverage['required_duration']:.2f}s required, "
            f"{coverage['deficit']:.2f}s missing"
        )

    return errors, coverage


def set_segment_backup(
    plan_file: Path,
    segment_id: str,
    asset_uid: str,
    enabled: bool,
) -> dict[str, Any]:
    plan = normalize_plan_editorial_fields(read_json(plan_file))

    if plan.get("review_status") == STATUS_APPROVED:
        raise ValueError("approved production plans are frozen")

    changed = False

    for segment in plan.get("segments") or []:
        if segment.get("segment_id") != segment_id:
            continue

        primary = segment.get("selected_asset")
        primary_uid = _asset_uid_value(primary)

        if asset_uid == primary_uid:
            raise ValueError(
                f"{asset_uid} is already PRIMARY for {segment_id}"
            )

        if enabled:
            authorized_elsewhere = authorized_asset_location(
                plan,
                asset_uid,
                exclude_segment_id=segment_id,
            )

            if authorized_elsewhere is not None:
                raise ValueError(
                    f"{asset_uid} is already authorized in "
                    f"{authorized_elsewhere}"
                )

        choices = _unique_review_assets(
            list(segment.get("alternatives") or [])
            + list(segment.get("backup_assets") or [])
            + [segment.get("original_selected_asset")]
        )

        candidate = next(
            (
                item
                for item in choices
                if _asset_uid_value(item) == asset_uid
            ),
            None,
        )

        if candidate is None:
            raise ValueError(
                f"asset {asset_uid} is not available for {segment_id}"
            )

        backups = _unique_review_assets(
            list(segment.get("backup_assets") or [])
        )

        if enabled:
            if any(
                set(candidate_identity_keys(candidate)) & set(candidate_identity_keys(item))
                for item in [primary] + backups
            ):
                raise ValueError(
                    f"{asset_uid} duplicates an existing primary or backup source"
                )
            backups = _unique_review_assets(backups + [candidate])
        else:
            backups = [
                item
                for item in backups
                if _asset_uid_value(item) != asset_uid
            ]

        segment["backup_assets"] = backups
        changed = True
        break

    if not changed:
        raise ValueError(f"segment not found: {segment_id}")

    refresh_plan_coverage(plan)
    plan["updated_at"] = utc_timestamp()

    write_json_atomic(plan_file, plan)
    return plan


def reorder_segment_backups(
    plan_file: Path,
    segment_id: str,
    ordered_asset_uids: list[str] | tuple[str, ...],
) -> dict[str, Any]:
    plan = normalize_plan_editorial_fields(read_json(plan_file))

    if plan.get("review_status") == STATUS_APPROVED:
        raise ValueError("approved production plans are frozen")

    ordered = [str(uid or "").strip() for uid in ordered_asset_uids if str(uid or "").strip()]
    changed = False

    for segment in plan.get("segments") or []:
        if segment.get("segment_id") != segment_id:
            continue

        backups = _unique_review_assets(list(segment.get("backup_assets") or []))
        by_uid = {_asset_uid_value(asset): asset for asset in backups}
        unknown = [uid for uid in ordered if uid not in by_uid]
        if unknown:
            raise ValueError(
                f"backup asset is not available for {segment_id}: {unknown[0]}"
            )

        seen: set[str] = set()
        reordered = []
        for uid in ordered:
            if uid in seen:
                continue
            reordered.append(by_uid[uid])
            seen.add(uid)
        for asset in backups:
            uid = _asset_uid_value(asset)
            if uid not in seen:
                reordered.append(asset)

        segment["backup_assets"] = reordered
        changed = True
        break

    if not changed:
        raise ValueError(f"segment not found: {segment_id}")

    refresh_plan_coverage(plan)
    plan["updated_at"] = utc_timestamp()
    write_json_atomic(plan_file, plan)
    return plan


def promote_segment_backup(
    plan_file: Path,
    segment_id: str,
    asset_uid: str,
) -> dict[str, Any]:
    plan = read_json(plan_file)
    for segment in plan.get("segments") or []:
        if not isinstance(segment, Mapping) or segment.get("segment_id") != segment_id:
            continue
        if any(
            _asset_uid_value(asset) == asset_uid
            for asset in segment.get("backup_assets") or []
        ):
            return replace_segment_asset(plan_file, segment_id, asset_uid)
        raise ValueError(f"asset {asset_uid} is not a BACKUP for {segment_id}")
    raise ValueError(f"segment not found: {segment_id}")


def _project_relative_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(Path(utils.root_dir()).resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def resolve_local_asset_path(path_value: str | Path | None, project_root: str | Path | None = None) -> Path | None:
    value = str(path_value or "").strip()
    if not value:
        return None

    root = Path(project_root) if project_root is not None else Path(utils.root_dir())
    path = Path(value)
    candidates = []
    if path.is_absolute():
        candidates.append(path)
        for container_root in (Path("/MoneyPrinterTurbo"),):
            try:
                candidates.append(root / path.relative_to(container_root))
            except ValueError:
                pass
    else:
        candidates.append(root / path)

    for candidate in candidates:
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate
    return None


def _candidate_from_asset(asset: Mapping[str, Any]) -> MaterialCandidate:
    metadata = asset.get("metadata") if isinstance(asset.get("metadata"), dict) else {}
    search = asset.get("search") if isinstance(asset.get("search"), dict) else {}
    return MaterialCandidate(
        provider=str(asset.get("provider") or asset.get("source") or ""),
        canonical_id=str(asset.get("canonical_id") or asset.get("asset_uid") or ""),
        dedupe_key=str(asset.get("dedupe_key") or asset.get("asset_uid") or ""),
        search_term=str(search.get("term") or ""),
        rank=search.get("rank"),
        url=str(asset.get("url") or ""),
        duration=metadata.get("duration"),
        width=metadata.get("width"),
        height=metadata.get("height"),
        orientation=metadata.get("orientation"),
        filename=metadata.get("filename"),
        source_info=metadata,
    )


def _selection_decision_from_plan_asset(
    asset: Mapping[str, Any],
    segment: Mapping[str, Any],
) -> MaterialSelectionDecision:
    candidate = _candidate_from_asset(asset)

    ranking = (
        asset.get("ranking")
        if isinstance(asset.get("ranking"), dict)
        else {}
    )

    effective_duration = ranking.get("effective_duration")

    if effective_duration in (None, ""):
        effective_duration = _usable_asset_duration(
            asset,
            float(segment.get("duration") or 0),
        )

    return MaterialSelectionDecision(
        candidate=candidate,
        orientation_score=int(
            ranking.get("orientation_score") or 0
        ),
        rank_score=int(
            ranking.get("rank_score") or 0
        ),
        quality_score=int(
            ranking.get("quality_score") or 0
        ),
        duration_score=int(
            ranking.get("duration_score") or 0
        ),
        freshness_score=int(
            ranking.get("freshness_score") or 0
        ),
        diversity_adjustment=int(
            ranking.get("diversity_adjustment") or 0
        ),
        total_score=int(
            ranking.get("total_score") or 0
        ),
        effective_duration=float(
            effective_duration or 0
        ),
    )


def render_timeline_from_plan(
    plan: Mapping[str, Any],
) -> Any:
    """
    Build the exact Human Review render timeline.

    Scene policy:

      PRIMARY
        -> normal playback when long enough
        -> slowdown >= 0.90x when possible
        -> slowdown as low as 0.85x when that completely avoids a cut

      BACKUP
        -> only explicitly human-approved backups
        -> only when PRIMARY cannot safely cover the scene
        -> avoid cuts shorter than 0.75s

    Suggestions never enter the timeline.
    """

    segments = [
        segment
        for segment in (plan.get("segments") or [])
        if isinstance(segment, Mapping)
    ]

    try:
        audio_duration = float(
            plan.get("duration") or 0
        )
    except (TypeError, ValueError):
        audio_duration = 0.0

    required_duration = max(
        0.0,
        audio_duration + 0.10,
    )

    pieces: list[dict[str, Any]] = []
    segment_shortfalls: list[dict[str, Any]] = []

    seen_authorized: dict[str, str] = {}
    total_scene_target = 0.0

    def append_piece(
        *,
        segment_id: str,
        role: str,
        asset: Mapping[str, Any],
        source_duration: float,
        output_duration: float,
        playback_speed: float,
        **extra: Any,
    ) -> None:
        uid = _asset_uid_value(asset)

        if not uid:
            raise ValueError(
                f"{segment_id}: {role} has no asset_uid"
            )

        pieces.append(
            {
                "segment_id": segment_id,
                "role": role,
                "asset_uid": uid,
                "asset": dict(asset),
                "flip_horizontal": asset_flip_horizontal(asset),
                "source_duration": round(
                    source_duration,
                    6,
                ),
                "output_duration": round(
                    output_duration,
                    6,
                ),
                "playback_speed": round(
                    playback_speed,
                    6,
                ),
                **extra,
            }
        )

    for index, segment in enumerate(segments):
        segment_id = str(
            segment.get("segment_id")
            or f"segment-{index + 1:03d}"
        )

        try:
            target = float(
                segment.get("duration") or 0
            )
        except (TypeError, ValueError):
            target = 0.0

        if index == len(segments) - 1:
            target += 0.10

        target = max(0.0, target)
        total_scene_target += target

        primary = segment.get("selected_asset")

        if not isinstance(primary, Mapping):
            segment_shortfalls.append(
                {
                    "segment_id": segment_id,
                    "shortfall": round(target, 6),
                }
            )
            continue

        primary_uid = _asset_uid_value(primary)

        if not primary_uid:
            segment_shortfalls.append(
                {
                    "segment_id": segment_id,
                    "shortfall": round(target, 6),
                }
            )
            continue

        previous = seen_authorized.get(primary_uid)

        if previous is not None:
            raise ValueError(
                f"{segment_id}: authorized asset "
                f"{primary_uid} already used in {previous}"
            )

        seen_authorized[primary_uid] = segment_id

        backups = _unique_assets_by_uid(
            list(
                segment.get("backup_assets")
                or []
            )
        )

        # Validate globally authorized BACKUPS even if slowdown later
        # makes them unnecessary for rendering.
        for backup in backups:
            backup_uid = _asset_uid_value(backup)

            if not backup_uid:
                raise ValueError(
                    f"{segment_id}: BACKUP has no asset_uid"
                )

            previous = seen_authorized.get(backup_uid)

            if previous is not None:
                raise ValueError(
                    f"{segment_id}: authorized asset "
                    f"{backup_uid} already used in {previous}"
                )

            seen_authorized[backup_uid] = segment_id

        resolution = resolve_human_review_segment_duration(segment, target)
        for resolved_piece in resolution["pieces"]:
            append_piece(segment_id=segment_id, **resolved_piece)

        if resolution["shortfall"] > 0.0:
            segment_shortfalls.append(
                {
                    "segment_id": segment_id,
                    "shortfall": round(resolution["shortfall"], 6),
                }
            )

    # A tail is not a new semantic scene.  It must be represented by a scene
    # in the review plan, never silently covered by looping or freezing the
    # final approved asset.
    unsegmented_shortfall = max(0.0, required_duration - total_scene_target)
    # If there are too few scenes to span the whole audio, report that as an
    # explicit timeline-tail shortfall instead of letting production silently
    # loop, extend, or freeze the final scene.
    if unsegmented_shortfall > 0.01:
        segment_shortfalls.append(
            {
                "segment_id": "timeline-tail",
                "shortfall": round(
                    unsegmented_shortfall,
                    6,
                ),
            }
        )

    total_output_duration = sum(
        float(piece["output_duration"])
        for piece in pieces
    )

    total_shortfall = sum(
        float(item["shortfall"])
        for item in segment_shortfalls
    )

    return SimpleNamespace(
        pieces=tuple(pieces),
        segment_shortfalls=tuple(
            segment_shortfalls
        ),
        total_output_duration=round(
            total_output_duration,
            6,
        ),
        required_duration=round(
            required_duration,
            6,
        ),
        shortfall=round(
            total_shortfall,
            6,
        ),
    )


def selection_result_from_plan(
    plan: Mapping[str, Any],
) -> Any:
    """
    Build only the assets that are actually required by the
    frozen scene-scoped render timeline.

    Unused BACKUPS are authorized reserves but are not
    materialized unnecessarily.
    """

    timeline = render_timeline_from_plan(plan)

    if timeline.shortfall > 0.01:
        details = ", ".join(
            f"{item['segment_id']}="
            f"{item['shortfall']:.2f}s"
            for item in timeline.segment_shortfalls
        )

        raise ValueError(
            "approved production plan has insufficient "
            f"scene coverage: {details}"
        )

    segment_map = {
        str(
            segment.get("segment_id") or ""
        ): segment
        for segment in (
            plan.get("segments") or []
        )
        if isinstance(segment, Mapping)
    }

    decisions: list[
        MaterialSelectionDecision
    ] = []
    # Decisions deliberately remain the established public selection type.  Keep
    # the frozen scene identity alongside them so production materialization can
    # request Asset Hub bundles by approved segment, never by search term.
    decision_segment_ids: list[str] = []

    primary_count = 0
    backup_count = 0
    materialized_uids: set[str] = set()

    for piece in timeline.pieces:
        segment_id = str(
            piece["segment_id"]
        )

        segment = segment_map.get(
            segment_id,
            {},
        )

        asset = piece["asset"]

        # Timeline autofill may replay/freeze the final approved asset.  It
        # remains one frozen selection and must be materialized only once.
        uid = str(piece["asset_uid"])
        if uid not in materialized_uids:
            decisions.append(
                _selection_decision_from_plan_asset(asset, segment)
            )
            decision_segment_ids.append(segment_id)
            materialized_uids.add(uid)

        if piece["role"] == "PRIMARY":
            primary_count += 1
        else:
            backup_count += 1

    return SimpleNamespace(
        decisions=tuple(decisions),
        primary_count=primary_count,
        backup_count=backup_count,
        timeline=timeline,
        decision_segment_ids=tuple(decision_segment_ids),
    )


def materials_from_approved_plan(plan: Mapping[str, Any]) -> list[MaterialInfo]:
    if plan.get("review_status") != STATUS_APPROVED:
        raise ValueError("production plan is not approved")
    materials = []
    for segment in plan.get("segments") or []:
        selected = segment.get("selected_asset") if isinstance(segment, dict) else None
        if not isinstance(selected, dict):
            raise ValueError("approved production plan has a segment without selected_asset")
        metadata = selected.get("metadata") if isinstance(selected.get("metadata"), dict) else {}
        local_path = str(metadata.get("local_path") or selected.get("url") or "")
        if not local_path:
            raise ValueError(f"approved asset cannot be materialized: {selected.get('asset_uid')}")
        materials.append(
            MaterialInfo(
                provider=str(selected.get("provider") or selected.get("source") or "local"),
                url=local_path,
                duration=int(float(metadata.get("duration") or 0)),
                source_info={
                    "asset_id": selected.get("asset_uid"),
                    "dedupe_key": selected.get("dedupe_key"),
                    "search_term": (selected.get("search") or {}).get("term") if isinstance(selected.get("search"), dict) else "",
                    "human_review_plan": True,
                },
            )
        )
    return materials


def _preview_value_from_mapping(info: Mapping[str, Any], *, depth: int = 0) -> str:
    for key in PREVIEW_KEYS:
        value = str(info.get(key) or "").strip()
        if value:
            return value
    if depth >= 2:
        return ""
    for value in info.values():
        if isinstance(value, Mapping):
            found = _preview_value_from_mapping(value, depth=depth + 1)
            if found:
                return found
    return ""


def _thumbnail_url(candidate: Any) -> str:
    info = getattr(candidate, "source_info", None)
    if not isinstance(info, dict):
        return ""
    return _preview_value_from_mapping(info)


def _safe_preview_filename(uid: str) -> str:
    return (uid.replace("/", "-").replace(":", "-") or "asset")[:140]


def _is_url(value: str) -> bool:
    return value.startswith(("http://", "https://"))


def _provider(candidate: Any) -> str:
    return str(getattr(candidate, "provider", "") or "").strip().lower()


def _asset_hub_base_url() -> str:
    return str(os.environ.get("ASSET_HUB_URL") or os.environ.get("ASSET_HUB_BASE_URL") or "").strip().rstrip("/")


def _same_origin(left: str, right: str) -> bool:
    left_parts = urlparse(left)
    right_parts = urlparse(right)
    return (
        left_parts.scheme.lower(),
        left_parts.hostname.lower() if left_parts.hostname else "",
        left_parts.port,
    ) == (
        right_parts.scheme.lower(),
        right_parts.hostname.lower() if right_parts.hostname else "",
        right_parts.port,
    )


def _asset_hub_preview_request(url: str) -> tuple[str, dict[str, str]] | None:
    base_url = _asset_hub_base_url()
    api_key = str(os.environ.get("ASSET_HUB_API_KEY") or "").strip()
    if not base_url or not api_key:
        return None

    resolved = urljoin(f"{base_url}/", url.lstrip("/")) if not _is_url(url) else url
    if not _same_origin(resolved, base_url):
        return None
    return resolved, {"X-Asset-Hub-Api-Key": api_key}


def _path_suffix(value: str) -> str:
    return Path(value.split("?", 1)[0]).suffix.lower()


def _looks_like_video(value: str) -> bool:
    return _path_suffix(value) in VIDEO_EXTENSIONS


def _looks_like_image(value: str) -> bool:
    return _path_suffix(value) in IMAGE_EXTENSIONS


def _preview_warning(candidate: Any, code: str, message: str) -> dict[str, str]:
    return {
        "asset_uid": candidate_uid(candidate),
        "source": str(getattr(candidate, "provider", "") or ""),
        "code": code,
        "message": message,
    }


def _write_placeholder_thumbnail(path: Path, label: str) -> None:
    text = escape((label or "NO PREVIEW AVAILABLE")[:42])
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="360" height="640" viewBox="0 0 360 640">'
        '<rect width="360" height="640" fill="#1f2937"/>'
        '<rect x="18" y="18" width="324" height="604" fill="#374151" stroke="#9ca3af" stroke-width="2"/>'
        '<text x="180" y="300" text-anchor="middle" fill="#f9fafb" font-family="Arial" font-size="21">NO PREVIEW</text>'
        f'<text x="180" y="334" text-anchor="middle" fill="#f9fafb" font-family="Arial" font-size="18">{text}</text>'
        '</svg>'
    )
    path.write_text(svg, encoding="utf-8")


def _existing_local_preview(thumbnails_dir: Path, uid: str) -> Path | None:
    for extension in (".jpg", ".jpeg", ".png", ".webp"):
        path = thumbnails_dir / f"{uid}{extension}"
        if path.exists() and path.stat().st_size > 0:
            return path
    return None


def _cache_local_image(value: str, thumbnails_dir: Path, uid: str) -> Path | None:
    path = Path(value)
    if not path.is_file() or path.stat().st_size <= 0 or not _looks_like_image(value):
        return None
    target = thumbnails_dir / f"{uid}{path.suffix.lower()}"
    if target.resolve() != path.resolve():
        shutil.copyfile(path, target)
    return target if target.exists() and target.stat().st_size > 0 else None


def _cache_remote_image(
    url: str,
    thumbnails_dir: Path,
    uid: str,
    *,
    headers: Mapping[str, str] | None = None,
    allow_redirects: bool = True,
) -> Path | None:
    if _looks_like_video(url):
        return None
    try:
        with requests.get(
            url,
            stream=True,
            timeout=(5, 20),
            headers=dict(headers or {}),
            allow_redirects=allow_redirects,
        ) as response:
            response.raise_for_status()
            content_type = str(response.headers.get("content-type") or "").split(";", 1)[0].lower()
            if content_type and not content_type.startswith("image/"):
                return None
            extension = mimetypes.guess_extension(content_type) if content_type else ""
            if extension == ".jpe":
                extension = ".jpg"
            if extension not in IMAGE_EXTENSIONS:
                extension = _path_suffix(url) if _looks_like_image(url) else ".jpg"
            target = thumbnails_dir / f"{uid}{extension}"
            total = 0
            with target.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > MAX_PREVIEW_BYTES:
                        handle.close()
                        target.unlink(missing_ok=True)
                        return None
                    handle.write(chunk)
            return target if target.exists() and target.stat().st_size > 0 else None
    except Exception:
        return None


def _cache_frame_from_local_video(candidate: Any, thumbnails_dir: Path, uid: str) -> Path | None:
    source_path = Path(str(getattr(candidate, "url", "") or ""))
    thumb = thumbnails_dir / f"{uid}.jpg"
    if not source_path.is_file():
        return None
    return _extract_video_frame(source_path.as_posix(), thumb)


def _extract_video_frame(source: str, thumb: Path) -> Path | None:
    """Extract a bounded review image without materializing the video asset.

    FFmpeg can seek HTTP(S) MP4s itself, so a review preview does not need a
    full provider download.  The thumbnail is the durable cache entry; callers
    only expose it after FFmpeg has successfully produced a non-empty file.
    """
    try:
        subprocess.run(
            [utils.get_ffmpeg_binary(), "-y", "-ss", "00:00:01", "-i", source, "-frames:v", "1", "-q:v", "3", thumb.as_posix()],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
        return thumb if thumb.exists() and thumb.stat().st_size > 0 else None
    except Exception:
        thumb.unlink(missing_ok=True)
        return None


def _cache_frame_from_remote_video(candidate: Any, thumbnails_dir: Path, uid: str) -> Path | None:
    source_url = str(getattr(candidate, "url", "") or "").strip()
    if not _is_url(source_url) or not _looks_like_video(source_url):
        return None
    return _extract_video_frame(source_url, thumbnails_dir / f"{uid}.jpg")


def ensure_candidate_preview(candidate: Any, thumbnails_dir: Path) -> tuple[dict[str, str], list[dict[str, str]]]:
    thumbnails_dir.mkdir(parents=True, exist_ok=True)
    uid = _safe_preview_filename(candidate_uid(candidate))
    url = _thumbnail_url(candidate)
    is_asset_hub = _provider(candidate) == "asset_hub"

    cached = _existing_local_preview(thumbnails_dir, uid)
    if cached:
        return {"type": "local", "value": _project_relative_path(cached), "status": "available"}, []

    if url:
        if is_asset_hub:
            request = _asset_hub_preview_request(url)
            if request:
                preview_url, headers = request
                cached = _cache_remote_image(
                    preview_url,
                    thumbnails_dir,
                    uid,
                    headers=headers,
                    allow_redirects=False,
                )
                if cached:
                    return {"type": "local", "value": _project_relative_path(cached), "status": "available"}, []
            url = ""
        elif not _is_url(url):
            cached = _cache_local_image(url, thumbnails_dir, uid)
            if cached:
                return {"type": "local", "value": _project_relative_path(cached), "status": "available"}, []
        else:
            cached = _cache_remote_image(url, thumbnails_dir, uid)
            if cached:
                return {"type": "local", "value": _project_relative_path(cached), "status": "available"}, []
            if not _looks_like_video(url):
                return {"type": "url", "value": url, "status": "available"}, []

    cached = _cache_frame_from_local_video(candidate, thumbnails_dir, uid)
    if cached:
        return {"type": "local", "value": _project_relative_path(cached), "status": "available"}, []

    # Asset Hub owns its preview authorization contract.  Stock candidates
    # with a public remote video URL can be inspected directly by FFmpeg.
    if not is_asset_hub:
        cached = _cache_frame_from_remote_video(candidate, thumbnails_dir, uid)
        if cached:
            return {"type": "local", "value": _project_relative_path(cached), "status": "available"}, []

    placeholder = thumbnails_dir / f"{uid}.svg"
    if not placeholder.exists():
        _write_placeholder_thumbnail(placeholder, candidate_uid(candidate))
    return (
        {
            "type": "none",
            "value": "",
            "status": "unavailable",
            "placeholder_path": _project_relative_path(placeholder),
        },
        [_preview_warning(candidate, "preview_unavailable", "NO PREVIEW AVAILABLE")],
    )


def review_previewable(preview: Mapping[str, Any] | None) -> bool:
    """Whether Human Review has a usable representation for an asset.

    This is intentionally based on the normalized result of
    ``ensure_candidate_preview`` rather than the presence of any one optional
    provider metadata field.
    """
    if not isinstance(preview, Mapping):
        return False
    return (
        str(preview.get("status") or "") == "available"
        and str(preview.get("type") or "") in {"local", "url"}
        and bool(str(preview.get("value") or "").strip())
    )


def ensure_thumbnail(candidate: Any, thumbnails_dir: Path) -> tuple[str, str]:
    preview, _warnings = ensure_candidate_preview(candidate, thumbnails_dir)
    if preview.get("type") == "local":
        return preview.get("value", ""), preview.get("source_url", "")
    if preview.get("type") == "url":
        return "", preview.get("value", "")
    return preview.get("placeholder_path", ""), ""


def resolve_candidate_preview(candidate: Mapping[str, Any]) -> str | None:
    preview = candidate.get("preview") if isinstance(candidate.get("preview"), Mapping) else {}
    preview_type = str(preview.get("type") or "")
    value = str(preview.get("value") or "").strip()
    if preview_type == "local" and value:
        local = resolve_local_asset_path(value)
        return local.as_posix() if local else None
    if preview_type == "url" and value:
        return value
    thumb = str(candidate.get("thumbnail_path") or "")
    local_thumb = resolve_local_asset_path(thumb)
    if local_thumb and local_thumb.suffix.lower() in IMAGE_EXTENSIONS:
        return local_thumb.as_posix()
    thumb_url = str(candidate.get("thumbnail_url") or "").strip()
    return thumb_url or None


def _word_chunks(text: str, count: int) -> list[str]:
    words = text.split()
    if count <= 0:
        return []
    if not words:
        return [""] * count
    base, extra = divmod(len(words), count)
    chunks = []
    offset = 0
    for index in range(count):
        size = base + (1 if index < extra else 0)
        chunks.append(" ".join(words[offset:offset + size]))
        offset += size
    return chunks


def split_script_for_segments(script_text: str, segment_count: int) -> list[str]:
    """Split canonical script into contiguous editorial fragments without LLM/timestamps."""
    count = max(0, int(segment_count or 0))
    if count == 0:
        return []
    text = re.sub(r"\s+", " ", str(script_text or "")).strip()
    if not text:
        return [""] * count
    if count == 1:
        return [text]

    sentences = [item.strip() for item in re.findall(r"[^.!?。！？]+[.!?。！？]*", text) if item.strip()]
    if len(sentences) < count:
        return _word_chunks(text, count)

    total_words = sum(len(sentence.split()) for sentence in sentences)
    target = max(1.0, total_words / count)
    chunks = []
    current: list[str] = []
    current_words = 0
    sentence_index = 0
    for bucket in range(count):
        current = []
        current_words = 0
        while sentence_index < len(sentences):
            remaining_sentences = len(sentences) - sentence_index
            remaining_buckets = count - bucket
            sentence = sentences[sentence_index]
            sentence_words = len(sentence.split())
            must_take = not current
            should_take = current_words + sentence_words <= target or remaining_sentences > remaining_buckets
            if not must_take and not should_take:
                break
            current.append(sentence)
            current_words += sentence_words
            sentence_index += 1
            if current_words >= target and remaining_sentences - 1 >= remaining_buckets - 1:
                break
        chunks.append(" ".join(current).strip())

    if sentence_index < len(sentences):
        tail = " ".join(sentences[sentence_index:]).strip()
        chunks[-1] = f"{chunks[-1]} {tail}".strip()
    return chunks


def allocate_script_segment_durations(
    script_fragments: list[str],
    total_duration: float,
) -> list[float]:
    """Allocate an audio timeline across contiguous script fragments.

    ``clip_duration`` still determines the editorial scene cadence and thus
    how many fragments are made.  It is deliberately not used for timeline
    timing: before subtitle timestamps exist, spoken-word count is the best
    deterministic proxy for how much audio belongs to each fragment.

    Values are rounded to microseconds for stable JSON.  The final value only
    receives the accumulated rounding correction, not an unsegmented audio
    remainder.
    """
    count = len(script_fragments)
    if count == 0:
        return []

    duration = max(0.0, float(total_duration or 0))
    word_counts = [len(re.findall(r"\S+", str(fragment or ""))) for fragment in script_fragments]
    total_words = sum(word_counts)
    weights = word_counts if total_words else [1] * count
    weight_total = total_words or count

    allocated = [
        round(duration * weight / weight_total, 6)
        for weight in weights
    ]
    # Keep the plan's scene durations equal to the audio duration despite
    # serialization rounding.  This correction is bounded by half a
    # microsecond per segment.
    allocated[-1] = round(duration - sum(allocated[:-1]), 6)
    return allocated


def visual_queries_for_review_segments(
    script_text: str,
    segment_count: int,
    existing_terms: list[str] | tuple[str, ...] | None = None,
    editorial_profile: Mapping[str, Any] | None = None,
) -> list[tuple[str, ...]]:
    """Compatibility API: return Asset Hub's segment-local visual queries."""
    fragments = split_script_for_segments(script_text, segment_count)
    return [
        build_scene_retrieval_queries(
            build_scene_visual_intent(fragment, editorial_profile=editorial_profile),
            "asset_hub",
        )
        for fragment in fragments
    ]


def retrieval_queries_for_review_segments(
    script_text: str,
    segment_count: int,
    editorial_profile: Mapping[str, Any] | None = None,
    video_terms: list[str] | tuple[str, ...] | None = None,
    material_title: str = "",
    content_title: str = "",
) -> list[dict[str, tuple[str, ...]]]:
    """Build provider representations from one SceneVisualIntent per scene."""
    inherited_subject = inherited_subject_preference(
        material_title=content_title or material_title,
        script_text=script_text,
    )
    return [
        {
            provider: build_scene_retrieval_queries(intent, provider, video_terms)
            for provider in ("pexels", "pixabay", "coverr", "asset_hub")
        }
        for intent in (
            build_scene_visual_intent(
                fragment,
                editorial_profile=editorial_profile,
                inherited_subject=inherited_subject,
                subject_hints=video_terms,
            )
            for fragment in split_script_for_segments(script_text, segment_count)
        )
    ]


def build_plan(
    *,
    batch_id: str,
    task_id: str,
    stem: str,
    audio_path: str,
    script_path: str,
    script_text: str,
    duration: float,
    aspect_ratio: str,
    visual_style: str,
    editorial_profile: Mapping[str, Any] | None = None,
    material_source_policy: Mapping[str, Any] | None = None,
    asset_hub_source_policy: Mapping[str, Any] | None = None,
    material_title: str = "",
    source_policy: str = "",
    provider_diagnostics: list[dict[str, Any]] | None = None,
    video_terms: list[str] | tuple[str, ...] | None = None,
    content_title: str = "",
    selection_result: Any,
    discovery_result: Any,
    output_path: Path,
) -> dict[str, Any]:
    existing = read_json(output_path) if output_path.exists() else {}
    if existing.get("review_status") == STATUS_APPROVED:
        return existing

    thumbnails_dir = output_path.parent / "thumbnails"
    all_candidates = list(getattr(discovery_result, "candidates", ()) or [])
    selected_decisions = list(getattr(selection_result, "decisions", ()) or [])
    target_segment_count = max(
        len(selected_decisions),
        int(getattr(selection_result, "target_count", 0) or 0),
    )
    # Selection's clip duration has already determined this scene count.  It
    # remains an editorial cadence, but is not a fixed-duration timeline grid.
    editorial_profile = normalize_editorial_profile(editorial_profile)
    script_fragments = split_script_for_segments(script_text, target_segment_count)
    inherited_subject = inherited_subject_preference(
        material_title=content_title or stem,
        script_text=script_text,
    )
    segment_query_maps = retrieval_queries_for_review_segments(
        script_text,
        target_segment_count,
        editorial_profile,
        video_terms,
        material_title,
        content_title or stem,
    )
    review_inputs = [
        (
            selected_decisions[index] if index < len(selected_decisions) else None,
            script_fragments[index],
            segment_query_maps[index] if index < len(segment_query_maps) else {},
        )
        for index in range(target_segment_count)
        if index < len(script_fragments)
        and (
            index >= len(selected_decisions)
            or str(script_fragments[index] or "").strip()
        )
    ]
    segment_durations = allocate_script_segment_durations(
        [script_fragment for _decision, script_fragment, _queries in review_inputs],
        duration,
    )
    segments = []

    # ----------------------------------------------------------
    # PASS 1
    #
    # Reserve every PRIMARY before assigning suggestions.
    #
    # This prevents an asset from being shown as a suggestion in
    # segment-001 and later becoming PRIMARY in segment-002.
    # ----------------------------------------------------------

    reserved_segments = []
    used_selected_asset_uids: set[str] = set()
    used_selected_identity_keys: set[str] = set()
    previous_selected_candidates: list[Any] = []
    # Materialize the canonical Human Review representation before PRIMARY
    # allocation.  The result is cached so later serialization and suggestion
    # allocation observe exactly the same previewability decision.
    preview_cache: dict[str, tuple[dict[str, str], list[dict[str, str]]]] = {}

    def cached_preview(item: Any) -> tuple[dict[str, str], list[dict[str, str]]]:
        uid = candidate_uid(item)
        if uid not in preview_cache:
            preview_cache[uid] = ensure_candidate_preview(item, thumbnails_dir)
        return preview_cache[uid]

    for decision, script_fragment, query_map in review_inputs:
        original_candidate = getattr(decision, "candidate", None)

        ranked_candidates = _ranked_segment_candidates(
            original_candidate,
            all_candidates,
            tuple(query for values in query_map.values() for query in values),
            editorial_profile,
            retrieval_queries=query_map,
        )

        visible_ranked_candidates = _visible_editorial_candidates(
            ranked_candidates,
            editorial_profile,
        )
        intent = build_scene_visual_intent(
            script_fragment,
            editorial_profile=editorial_profile,
            visual_style=visual_style,
            inherited_subject=inherited_subject,
            subject_hints=video_terms,
        )
        # Discovery stays provider-specific, while all normalized eligible
        # candidates compete here against the same scene intent.
        if visible_ranked_candidates:
            v2_ranked = rank_candidates_v2(intent, visible_ranked_candidates, video_aspect=aspect_ratio, clip_duration=float(getattr(getattr(selection_result, "options", None), "clip_duration", 5) or 5), previous_candidates=previous_selected_candidates)
            visible_ranked_candidates = stable_secondary_dedupe(
                [item for item, _ranking in v2_ranked]
            )
            ranking_by_uid = {candidate_uid(item): ranking for item, ranking in v2_ranked}
        else:
            ranking_by_uid = {}

        candidate, forced_repeat = _select_segment_candidate(
            visible_ranked_candidates,
            used_selected_asset_uids,
            used_selected_identity_keys,
            is_review_previewable=lambda item: review_previewable(cached_preview(item)[0]),
            is_primary_eligible=lambda item: "explicit_narrative_contradiction" not in (
                ranking_by_uid.get(candidate_uid(item)).penalty_codes
                if ranking_by_uid.get(candidate_uid(item)) else ()
            ),
        )

        selected_uid = candidate_uid(candidate) if candidate is not None else ""

        if selected_uid:
            used_selected_asset_uids.add(selected_uid)
            used_selected_identity_keys.update(candidate_identity_keys(candidate))
            previous_selected_candidates.append(candidate)

        reserved_segments.append(
            (
                decision,
                script_fragment,
                query_map,
                original_candidate,
                visible_ranked_candidates,
                intent,
                ranking_by_uid,
                candidate,
                forced_repeat,
            )
        )

    # ----------------------------------------------------------
    # PASS 2
    #
    # Allocate up to three segment-local suggestions. Once all PRIMARYs have
    # been finalized, their UIDs are globally authorized. Prefer candidates
    # outside that set so displayed suggestions are promotable whenever the
    # retrieval pool permits it.  Within the existing editorial ordering,
    # prefer assets that are not yet visible anywhere in this review plan.
    # This is deliberately a soft allocation preference: a positive editorial
    # match remains ahead of an UNKNOWN or explicit contradiction even when
    # the latter has not been shown before.
    # ----------------------------------------------------------

    assigned_suggestions: list[list[Any]] = [
        []
        for _ in reserved_segments
    ]
    allocation_audit: list[dict[str, Any]] = [
        {
            "previewable_eligible_count": 0,
            "promotable_excluding_primary_count": 0,
            "globally_unseen_promotable_per_slot": [],
            "suggestion_slots": [],
        }
        for _ in reserved_segments
    ]

    candidate_queues: list[list[Any]] = []
    for reserved in reserved_segments:
        (
            decision,
            script_fragment,
            query_map,
            original_candidate,
            ranked_candidates,
            intent,
            ranking_by_uid,
            candidate,
            forced_repeat,
        ) = reserved

        selected_uid = candidate_uid(candidate)

        queue = []

        for alt in ranked_candidates:
            alt_uid = candidate_uid(alt)

            if not alt_uid:
                continue

            if alt_uid == selected_uid:
                continue

            queue.append(alt)

        candidate_queues.append(queue)

    authorized_primary_uids = {
        candidate_uid(reserved[7])
        for reserved in reserved_segments
        if reserved[7] is not None and candidate_uid(reserved[7])
    }
    # PRIMARY reservation happens before this pass, so every primary is
    # already visible to suggestion allocation, including primaries in later
    # segments.  Add suggestions as they are assigned to avoid repeating them
    # in normal visible slots when another equally eligible candidate exists.
    visible_asset_uids = set(authorized_primary_uids)
    visible_asset_frequencies: dict[str, int] = {
        uid: 1 for uid in authorized_primary_uids
    }

    for segment_index, queue in enumerate(candidate_queues):
        local_identities: set[str] = set()
        # Keep V2's editorial order inside a tier.  Allocation adds one
        # deterministic guardrail only: an unseen, previewable, promotable
        # candidate wins over a previously-visible candidate in *that same*
        # tier.  It never trades a stronger editorial tier for novelty.
        intent = reserved_segments[segment_index][5]
        ranking_by_uid = reserved_segments[segment_index][6]
        tier_for = lambda item: _allocation_editorial_tier(
            intent, item, ranking_by_uid.get(candidate_uid(item))
        )
        tier_rank = {tier: index for index, tier in enumerate(EDITORIAL_TIERS)}
        queue = sorted(
            enumerate(queue),
            key=lambda item: (
                tier_rank[tier_for(item[1])],
                1 if candidate_uid(item[1]) in visible_asset_uids else 0,
                item[0],
            ),
        )
        # Previewability is a Human Review contract, not an optional provider
        # metadata check.  Fill normal slots with inspectable assets first;
        # retain genuinely unavailable assets only for honest scarcity.
        previewable_queue = [
            item for item in queue
            if review_previewable(cached_preview(item[1])[0])
        ]
        unavailable_queue = [
            item for item in queue
            if not review_previewable(cached_preview(item[1])[0])
        ]
        queue = previewable_queue + unavailable_queue
        # Always exhaust promotable candidates before a scarcity fallback
        # already authorized by another segment.  Suggestions remain reusable
        # unless an operator promotes one.
        promotable_queue = [
            item for item in queue
            if candidate_uid(item[1]) not in authorized_primary_uids
        ]
        blocked_queue = [
            item for item in queue
            if candidate_uid(item[1]) in authorized_primary_uids
        ]
        allocation_audit[segment_index]["previewable_eligible_count"] = len({
            candidate_uid(item)
            for _position, item in previewable_queue
            if candidate_uid(item)
        })
        allocation_audit[segment_index]["promotable_excluding_primary_count"] = len({
            candidate_uid(item)
            for _position, item in promotable_queue
            if candidate_uid(item)
        })
        for _position, alt in promotable_queue + blocked_queue:
            alt_uid = candidate_uid(alt)
            identity_keys = candidate_identity_keys(alt)
            if not alt_uid or any(key in local_identities for key in identity_keys):
                continue
            def usable(item: Any) -> bool:
                return (
                    bool(candidate_uid(item))
                    and review_previewable(cached_preview(item)[0])
                    and not any(key in local_identities for key in candidate_identity_keys(item))
                )

            previewable_eligible = [item for _p, item in queue if usable(item)]
            promotable = [item for item in previewable_eligible if candidate_uid(item) not in authorized_primary_uids]
            unseen_promotable = [item for item in promotable if candidate_uid(item) not in visible_asset_uids]
            previously_visible_promotable = [item for item in promotable if candidate_uid(item) in visible_asset_uids]
            primary_used_blocked = [
                item for item in previewable_eligible
                if candidate_uid(item) in authorized_primary_uids
            ]
            slot_telemetry = {
                "slot": len(assigned_suggestions[segment_index]) + 1,
                "previewable_eligible_count": len({candidate_uid(item) for item in previewable_eligible}),
                "promotable_count": len({candidate_uid(item) for item in promotable}),
                "globally_unseen_promotable_count": len({candidate_uid(item) for item in unseen_promotable}),
                "previously_visible_promotable_count": len({candidate_uid(item) for item in previously_visible_promotable}),
                "primary_used_blocked_count": len({candidate_uid(item) for item in primary_used_blocked}),
                "editorial_tiers": {
                    "previewable_eligible": _tier_counts(previewable_eligible, tier_for),
                    "promotable": _tier_counts(promotable, tier_for),
                    "globally_unseen_promotable": _tier_counts(unseen_promotable, tier_for),
                    "previously_visible_promotable": _tier_counts(previously_visible_promotable, tier_for),
                    "primary_used_blocked": _tier_counts(primary_used_blocked, tier_for),
                },
            }
            alt_tier = tier_for(alt)
            previously_visible = alt_uid in visible_asset_uids
            if alt_uid in authorized_primary_uids:
                selection_reason = "BLOCKED_DIAGNOSTIC"
            elif not previously_visible:
                selection_reason = "UNSEEN_SAME_TIER"
            elif slot_telemetry["editorial_tiers"]["globally_unseen_promotable"][alt_tier] == 0:
                selection_reason = "REPEATED_TRUE_SCARCITY"
            elif any(
                slot_telemetry["editorial_tiers"]["globally_unseen_promotable"][tier] > 0
                for tier in EDITORIAL_TIERS[tier_rank[alt_tier] + 1:]
            ):
                selection_reason = "REPEATED_STRONGER_EDITORIAL"
            else:
                selection_reason = "OTHER"
            slot_telemetry.update({
                "asset_uid": alt_uid,
                "editorial_tier": alt_tier,
                "previously_visible": previously_visible,
                "global_frequency_before_selection": visible_asset_frequencies.get(alt_uid, 0),
                "selection_reason": selection_reason,
            })
            allocation_audit[segment_index]["suggestion_slots"].append(slot_telemetry)
            allocation_audit[segment_index]["globally_unseen_promotable_per_slot"].append(
                slot_telemetry["globally_unseen_promotable_count"]
            )
            assigned_suggestions[segment_index].append(alt)
            local_identities.update(identity_keys)
            visible_asset_uids.add(alt_uid)
            visible_asset_frequencies[alt_uid] = visible_asset_frequencies.get(alt_uid, 0) + 1
            if len(assigned_suggestions[segment_index]) == 3:
                break

    # Persist the union actually supplied to plan construction.  This is not
    # a provider-cache proxy: a UID is counted only when it entered at least
    # one segment's ranked pool.  It makes a later review able to distinguish
    # allocation reuse from a genuinely narrow discovery pool.
    union_candidates: dict[str, Any] = {}
    positive_uids: set[str] = set()
    unknown_uids: set[str] = set()
    for reserved in reserved_segments:
        ranked_candidates, intent, ranking_by_uid = reserved[4], reserved[5], reserved[6]
        for item in ranked_candidates:
            uid = candidate_uid(item)
            if not uid:
                continue
            union_candidates.setdefault(uid, item)
            ranking = ranking_by_uid.get(uid)
            mismatch = bool(ranking and "explicit_narrative_contradiction" in ranking.penalty_codes)
            if not mismatch and candidate_editorial_evidence(intent, item) > 0:
                positive_uids.add(uid)
            elif not mismatch:
                unknown_uids.add(uid)

    previewable_union_uids = {
        uid
        for uid, item in union_candidates.items()
        if review_previewable(cached_preview(item)[0])
    }
    promotable_union_uids = set(union_candidates) - authorized_primary_uids
    visible_allocation_audit = {
        "unique_candidate_uids": len(union_candidates),
        "unique_previewable_uids": len(previewable_union_uids),
        "unique_promotable_excluding_primary_uids": len(promotable_union_uids),
        "unique_positive_previewable_uids": len(positive_uids & previewable_union_uids),
        "unique_positive_promotable_uids": len(positive_uids & promotable_union_uids),
        "unique_unknown_previewable_uids": len(unknown_uids & previewable_union_uids),
        "unique_unknown_promotable_uids": len(unknown_uids & promotable_union_uids),
        "unique_primary_uids": len(authorized_primary_uids),
    }

    for index, reserved in enumerate(
        reserved_segments,
        1,
    ):
        (
            decision,
            script_fragment,
            queries,
            original_candidate,
            ranked_candidates,
            intent,
            ranking_by_uid,
            candidate,
            forced_repeat,
        ) = reserved

        selected_uid = candidate_uid(candidate) if candidate is not None else ""

        selected_decision = (
            decision
            if (
                candidate is not None
                and original_candidate is not None
                and candidate_uid(original_candidate) == selected_uid
            )
            else None
        )

        selected_asset = None
        selected_preview_warnings = []
        if candidate is not None:
            selected_preview, selected_preview_warnings = cached_preview(candidate)

            selected_thumb = (
                selected_preview.get("value")
                if selected_preview.get("type") == "local"
                else selected_preview.get(
                    "placeholder_path",
                    "",
                )
            )

            selected_thumb_url = (
                selected_preview.get("value")
                if selected_preview.get("type") == "url"
                else selected_preview.get(
                    "source_url",
                    "",
                )
            )

            selected_asset = serialize_candidate(
                candidate,
                selected_decision,
                selected_thumb,
            )
            _serialize_v2_ranking(selected_asset, ranking_by_uid.get(selected_uid))

            selected_asset["preview"] = (
                selected_preview
            )
            selected_asset["review_previewable"] = review_previewable(selected_preview)

            if selected_thumb_url:
                selected_asset[
                    "thumbnail_url"
                ] = selected_thumb_url

        alternatives = []
        segment_warnings = list(
            selected_preview_warnings
        )

        if forced_repeat:
            segment_warnings.append(
                {
                    "type": "forced_asset_repeat",
                    "code": "forced_asset_repeat",
                    "message": (
                        "selected asset repeated because all "
                        "ranked candidates were already used"
                    ),
                    "asset_uid": selected_uid,
                    "segment_id": (
                        f"segment-{index:03d}"
                    ),
                }
            )
        if selected_asset is None:
            segment_warnings.append(
                {
                    "type": "review_required",
                    "code": "missing_primary",
                    "message": "no visible candidate satisfies this review segment",
                    "segment_id": f"segment-{index:03d}",
                }
            )

        for alternative_index, alt in enumerate(assigned_suggestions[index - 1]):
            alt_preview, alt_preview_warnings = cached_preview(alt)

            alt_thumb = (
                alt_preview.get("value")
                if alt_preview.get("type") == "local"
                else alt_preview.get(
                    "placeholder_path",
                    "",
                )
            )

            alt_thumb_url = (
                alt_preview.get("value")
                if alt_preview.get("type") == "url"
                else alt_preview.get(
                    "source_url",
                    "",
                )
            )

            payload = serialize_candidate(
                alt,
                None,
                alt_thumb,
            )
            _serialize_v2_ranking(payload, ranking_by_uid.get(candidate_uid(alt)))
            allocation = allocation_audit[index - 1]["suggestion_slots"][alternative_index]
            payload["allocation"] = {
                "asset_uid": allocation["asset_uid"],
                "editorial_tier": allocation["editorial_tier"],
                "previously_visible": allocation["previously_visible"],
                "global_frequency_before_selection": allocation["global_frequency_before_selection"],
                "selection_reason": allocation["selection_reason"],
            }

            payload["preview"] = alt_preview
            payload["review_previewable"] = review_previewable(alt_preview)
            if not payload["review_previewable"]:
                # Preserve scarcity provenance without presenting an
                # uninspectable item as a normal operator choice.
                payload["diagnostic_only"] = True
            elif candidate_uid(alt) in authorized_primary_uids:
                # A PRIMARY is already committed to another segment.  Keep it
                # as honest scarcity provenance, but never expose it as a
                # normal choice that the operator cannot promote here.
                payload["diagnostic_only"] = True
                payload["diagnostic_reason"] = "primary_used_elsewhere"

            if alt_thumb_url:
                payload[
                    "thumbnail_url"
                ] = alt_thumb_url

            alternatives.append(payload)

            segment_warnings.extend(
                alt_preview_warnings
            )

        if candidate is not None and len(ranked_candidates) == 1:
            segment_warnings.append(
                {
                    "type": "candidate_scarcity",
                    "code": "candidate_scarcity",
                    "message": "only one eligible unique candidate was available for this scene",
                    "segment_id": f"segment-{index:03d}",
                    "candidate_pool_count": 1,
                }
            )

        segment_duration = segment_durations[index - 1]
        start = round(sum(segment_durations[:index - 1]), 6)
        end = round(start + segment_duration, 6)

        segments.append(
            {
                "segment_id": (
                    f"segment-{index:03d}"
                ),
                "start": start,
                "end": end,
                "duration": segment_duration,
                "script_text": (
                    script_fragment
                ),
                "scene_visual_intent": intent.to_dict(),
                "candidate_pool_count": len(ranked_candidates),
                "search_terms": list(dict.fromkeys([
                    *[str(query) for query in query_map.get("asset_hub", ())],
                    str(getattr(original_candidate, "search_term", "") or ""),
                    str(getattr(candidate, "search_term", "") or ""),
                ])) or [
                    str(
                        getattr(
                            candidate,
                            "search_term",
                            "",
                        )
                        or ""
                    )
                ],
                "retrieval_queries": {provider: list(values) for provider, values in query_map.items()},
                "candidate_funnel": {
                    "providers": {
                        provider: {
                            "retrieved": sum(str(getattr(item, "provider", "")) == provider for item in ranked_candidates),
                            "eligible": sum(str(getattr(item, "provider", "")) == provider for item in visible_ranked_candidates),
                            "ranked": sum(str(getattr(item, "provider", "")) == provider for item in visible_ranked_candidates if candidate_uid(item) in ranking_by_uid),
                        }
                        for provider in sorted({str(getattr(item, "provider", "")) for item in ranked_candidates if str(getattr(item, "provider", ""))})
                    },
                    "scarcity_reason": "segment_local_scarcity_global_compatibility_fallback" if len(ranked_candidates) < 4 else "",
                    "previewable_eligible_count": allocation_audit[index - 1]["previewable_eligible_count"],
                    "promotable_excluding_primary_count": allocation_audit[index - 1]["promotable_excluding_primary_count"],
                    "globally_unseen_promotable_per_slot": allocation_audit[index - 1]["globally_unseen_promotable_per_slot"],
                    "suggestion_slots": allocation_audit[index - 1]["suggestion_slots"],
                },
                "selected_asset": (
                    selected_asset
                ),
                "original_selected_asset": (
                    selected_asset
                ),
                "backup_assets": [],
                "alternatives": alternatives,
                "feedback": {
                    "original_selected_asset_uid": (
                        selected_asset.get("asset_uid")
                        if isinstance(selected_asset, Mapping)
                        else ""
                    ),
                    "final_selected_asset_uid": (
                        selected_asset.get("asset_uid")
                        if isinstance(selected_asset, Mapping)
                        else ""
                    ),
                    "human_changed": False,
                },
                "warnings": segment_warnings,
            }
        )

    plan = {
        "schema_version": SCHEMA_VERSION,
        "batch_id": batch_id,
        "task_id": task_id,
        "stem": stem,
        "job_name": stem,
        "audio_path": audio_path,
        "script_path": script_path,
        "duration": duration,
        "aspect_ratio": aspect_ratio,
        "visual_style": visual_style,
        "editorial_profile": editorial_profile,
        "material_source_policy": dict(material_source_policy or {}),
        "asset_hub_source_policy": dict(asset_hub_source_policy or {}),
        "material_title": str(material_title or ""),
        "title_scope": str(material_title or ""),
        "source_policy": str(source_policy or ""),
        "provider_diagnostics": [dict(item) for item in (provider_diagnostics or [])],
        "visible_allocation_audit": visible_allocation_audit,
        "review_required": True,
        "review_status": STATUS_PENDING,
        "visual_search_version": "scene-intent-v2",
        "visual_ranking_version": "candidate-ranking-v2",
        "created_at": existing.get("created_at") or utc_timestamp(),
        "updated_at": utc_timestamp(),
        "segments": segments,
        "warnings": collect_warnings(segments, aspect_ratio),
    }
    visible_by_provider: dict[str, int] = {}
    for segment in segments:
        for asset in [segment.get("selected_asset")] + list(segment.get("alternatives") or []):
            if isinstance(asset, Mapping):
                provider = str(asset.get("provider") or "").strip()
                if provider:
                    visible_by_provider[provider] = visible_by_provider.get(provider, 0) + 1
    for item in plan["provider_diagnostics"]:
        item["review_visible_count"] = visible_by_provider.get(str(item.get("provider") or ""), 0)
    refresh_plan_coverage(plan)
    write_json_atomic(output_path, plan)
    return plan


def collect_warnings(segments: list[dict[str, Any]], aspect_ratio: str) -> list[dict[str, str]]:
    warnings = []
    seen: dict[str, str] = {}
    target_portrait = str(aspect_ratio).replace(" ", "") in {"9:16", "portrait", "vertical"}
    for segment in segments:
        for warning in segment.get("warnings") or []:
            if isinstance(warning, dict):
                warnings.append({
                    "segment_id": str(segment.get("segment_id") or ""),
                    "type": str(warning.get("type") or warning.get("code") or ""),
                    "code": str(warning.get("code") or ""),
                    "message": str(warning.get("message") or ""),
                    "asset_uid": str(warning.get("asset_uid") or ""),
                    "source": str(warning.get("source") or ""),
                })
        selected = segment.get("selected_asset") or {}
        uid = str(selected.get("asset_uid") or "")
        if not uid:
            warnings.append({"segment_id": segment.get("segment_id", ""), "code": "candidate_missing", "message": "selected asset is missing an id"})
        elif uid in seen:
            warnings.append({"segment_id": segment.get("segment_id", ""), "code": "asset_repeated", "message": f"asset repeats {seen[uid]}"})
        seen[uid] = str(segment.get("segment_id") or "")
        metadata = selected.get("metadata") if isinstance(selected.get("metadata"), dict) else {}
        orientation = str(metadata.get("orientation") or "").lower()
        if target_portrait and orientation and "landscape" in orientation:
            warnings.append({"segment_id": segment.get("segment_id", ""), "code": "orientation_mismatch", "message": "landscape asset in portrait video"})
    return warnings


def replace_segment_asset(
    plan_file: Path,
    segment_id: str,
    asset_uid: str,
) -> dict[str, Any]:
    plan = normalize_plan_editorial_fields(read_json(plan_file))

    if plan.get("review_status") == STATUS_APPROVED:
        raise ValueError("approved production plans are frozen")

    authorized_elsewhere = authorized_asset_location(
        plan,
        asset_uid,
        exclude_segment_id=segment_id,
    )

    if authorized_elsewhere is not None:
        raise ValueError(
            f"{asset_uid} is already authorized in "
            f"{authorized_elsewhere}"
        )

    changed = False

    for segment in plan.get("segments") or []:
        if segment.get("segment_id") != segment_id:
            continue

        current = segment.get("selected_asset")
        alternatives = [
            item
            for item in segment.get("alternatives") or []
            if isinstance(item, dict)
        ]
        backups = [
            item
            for item in segment.get("backup_assets") or []
            if isinstance(item, dict)
        ]

        # A replacement may come from suggestions or an explicitly
        # approved backup.
        choices = _unique_review_assets(
            [current] + alternatives + backups
        )

        replacement_asset = next(
            (
                item
                for item in choices
                if _asset_uid_value(item) == asset_uid
            ),
            None,
        )

        if replacement_asset is None:
            raise ValueError(
                f"asset {asset_uid} is not available for {segment_id}"
            )

        original = (
            segment.get("original_selected_asset")
            or current
        )

        old_primary = current
        segment["selected_asset"] = replacement_asset
        segment["original_selected_asset"] = original

        # A PRIMARY cannot simultaneously be a BACKUP.
        segment["backup_assets"] = [
            item
            for item in _unique_review_assets(backups)
            if _asset_uid_value(item) != asset_uid
            and not (
                set(candidate_identity_keys(item))
                & set(candidate_identity_keys(replacement_asset))
            )
        ]

        # The new PRIMARY must not remain in SUGGESTED.
        # Put the old PRIMARY back into suggestions so the human can
        # undo/reconsider the choice.
        suggestion_pool = _unique_review_assets(
            [old_primary] + alternatives
        )

        segment["alternatives"] = [
            item
            for item in suggestion_pool
            if _asset_uid_value(item) != asset_uid
            and not (
                set(candidate_identity_keys(item))
                & set(candidate_identity_keys(replacement_asset))
            )
        ][:3]

        segment["feedback"] = {
            "original_selected_asset_uid": (
                _asset_uid_value(original)
            ),
            "final_selected_asset_uid": asset_uid,
            "human_changed": (
                _asset_uid_value(original) != asset_uid
            ),
        }

        changed = True
        break

    if not changed:
        raise ValueError(f"segment not found: {segment_id}")

    refresh_plan_coverage(plan)
    plan["updated_at"] = utc_timestamp()

    write_json_atomic(plan_file, plan)
    return plan


def approve_plan(
    plan_file: Path,
    project_root: str | Path | None = None,
    *,
    allow_insufficient_coverage: bool = False,
    enqueue_nightly: bool = True,
) -> dict[str, Any]:
    plan = normalize_plan_editorial_fields(read_json(plan_file))

    if plan.get("review_status") == STATUS_APPROVED:
        integrity = validate_approved_plan_integrity(plan)
        if not integrity["ok"]:
            raise ValueError(
                "cannot approve production plan:\n- "
                + "\n- ".join(integrity["errors"])
            )
        if enqueue_nightly:
            enqueue_approved_plan(
                plan_file,
                plan,
                project_root=project_root,
            )
        return plan

    errors, coverage = validate_plan_for_approval(
        plan,
        allow_insufficient_coverage=allow_insufficient_coverage,
    )

    integrity = validate_approved_plan_integrity(
        plan,
        require_approved=False,
    )
    errors.extend(integrity["errors"])

    if errors:
        raise ValueError(
            "cannot approve production plan:\n- "
            + "\n- ".join(errors)
        )

    plan["schema_version"] = SCHEMA_VERSION
    plan["coverage"] = integrity["coverage"] or coverage
    apply_segment_coverage_metrics(plan)
    plan["review_status"] = STATUS_APPROVED
    plan["review_required"] = False
    plan["reviewed_at"] = utc_timestamp()
    plan["reviewed_by"] = "human"
    plan["updated_at"] = plan["reviewed_at"]

    write_json_atomic(plan_file, plan)

    if enqueue_nightly:
        enqueue_approved_plan(
            plan_file,
            plan,
            project_root=project_root,
        )

    return plan

def reject_plan(plan_file: Path) -> dict[str, Any]:
    plan = read_json(plan_file)
    plan["review_status"] = STATUS_REJECTED
    plan["updated_at"] = utc_timestamp()
    write_json_atomic(plan_file, plan)
    return plan


def enqueue_approved_plan(plan_file: Path, plan: Mapping[str, Any] | None = None, project_root: str | Path | None = None) -> Path:
    plan = plan or read_json(plan_file)
    root = Path(project_root) if project_root is not None else Path(utils.root_dir())
    task_id = str(plan.get("task_id") or "")
    if task_id:
        task_dir = root / "storage" / "tasks" / task_id
        if (task_dir / "final-subtitled.mp4").is_file() or (task_dir / "final-1.mp4").is_file():
            return task_dir
    queue_dir = nightly_queue_pending_dir(project_root)
    queue_dir.mkdir(parents=True, exist_ok=True)
    job_id = f"review-{plan.get('batch_id')}-{plan.get('stem')}"
    queue_file = queue_dir / f"{job_id}.json"
    if queue_file.exists():
        return queue_file
    payload = {
        "render_mode": RENDER_MODE,
        "job_id": job_id,
        "task_id": plan.get("task_id"),
        "production_plan_path": Path(plan_file).as_posix(),
        "batch_id": plan.get("batch_id"),
        "stem": plan.get("stem"),
        "preset": "karaoke",
        "position": "bottom",
        "visual_style": plan.get("visual_style") or "none",
    }
    write_json_atomic(queue_file, payload)
    return queue_file
