"""Filesystem-backed human review plans for batch video production."""

from __future__ import annotations

import json
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
from typing import Any, Mapping
from urllib.parse import urljoin, urlparse

import requests

from app.custom.asset_search_v2 import build_visual_queries_v2
from app.custom.material_discovery import MaterialCandidate
from app.custom.material_selection import MaterialSelectionDecision
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
PREVIEW_KEYS = (
    "thumbnail_url", "thumbnail", "preview_url", "preview", "poster_url", "poster",
    "image_url", "image", "keyframe_url", "keyframe", "cover_url", "cover",
    "source_thumbnail_url", "source_thumbnail",
)
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv"}
FLIP_HORIZONTAL_DEFAULT = True


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


def _ranked_segment_candidates(
    candidate: Any,
    all_candidates: list[Any],
    preferred_terms: list[str] | tuple[str, ...] | None = None,
) -> list[Any]:
    """
    Rank candidates for one scene.

    Exact semantic-term matches stay first, but the rest of the
    Human Review pool is always available as fallback. This avoids
    starving a scene merely because its own search term returned
    too few unique assets.
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

    return _unique_candidates_by_uid(
        preferred_matches
        + [candidate]
        + same_term
        + remaining
    )


def _select_segment_candidate(ranked_candidates: list[Any], used_selected_asset_uids: set[str]) -> tuple[Any, bool]:
    for candidate in ranked_candidates:
        uid = candidate_uid(candidate)
        if uid and uid not in used_selected_asset_uids:
            return candidate, False
    return ranked_candidates[0], True


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

    plan["coverage"] = coverage_summary(plan)
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

    plan["coverage"] = coverage_summary(plan)
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


def _usable_asset_duration(
    asset: Mapping[str, Any] | None,
    segment_duration: float,
) -> float:
    if not isinstance(asset, Mapping):
        return 0.0

    metadata = asset.get("metadata")
    if not isinstance(metadata, Mapping):
        metadata = {}

    try:
        duration = float(metadata.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0

    if duration <= 0:
        return 0.0

    try:
        cap = float(segment_duration or 0)
    except (TypeError, ValueError):
        cap = 0.0

    if cap > 0:
        return min(duration, cap)

    return duration


def coverage_summary(
    plan: Mapping[str, Any],
) -> dict[str, float]:
    """
    Coverage is derived from the exact renderer timeline.

    This prevents Human Review and production from disagreeing about
    slowdown, backup usage or scene duration.
    """

    timeline = render_timeline_from_plan(
        plan
    )

    try:
        audio_duration = float(
            plan.get("duration") or 0
        )
    except (TypeError, ValueError):
        audio_duration = 0.0

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
        "deficit": round(
            deficit,
            3,
        ),
        "coverage_ratio": round(
            coverage_ratio,
            4,
        ),
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
            authorized_elsewhere = _authorized_asset_location(
                plan,
                asset_uid,
                exclude_segment_id=segment_id,
            )

            if authorized_elsewhere is not None:
                raise ValueError(
                    f"{asset_uid} is already authorized in "
                    f"{authorized_elsewhere}"
                )

        choices = _unique_assets_by_uid(
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

        backups = _unique_assets_by_uid(
            list(segment.get("backup_assets") or [])
        )

        if enabled:
            backups = _unique_assets_by_uid(backups + [candidate])
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

    plan["coverage"] = coverage_summary(plan)
    plan["updated_at"] = utc_timestamp()

    write_json_atomic(plan_file, plan)
    return plan


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

    def asset_duration(
        asset: Mapping[str, Any],
    ) -> float:
        metadata = (
            asset.get("metadata")
            if isinstance(
                asset.get("metadata"),
                Mapping,
            )
            else {}
        )

        try:
            return max(
                0.0,
                float(
                    metadata.get("duration")
                    or 0
                ),
            )
        except (TypeError, ValueError):
            return 0.0

    def append_piece(
        *,
        segment_id: str,
        role: str,
        asset: Mapping[str, Any],
        source_duration: float,
        output_duration: float,
        playback_speed: float,
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
            raise ValueError(
                f"{segment_id}: missing PRIMARY"
            )

        primary_uid = _asset_uid_value(primary)

        if not primary_uid:
            raise ValueError(
                f"{segment_id}: PRIMARY has no asset_uid"
            )

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

        available = asset_duration(primary)

        if available <= 0:
            raise ValueError(
                f"{segment_id}: PRIMARY {primary_uid} "
                "has unknown or zero duration"
            )

        # ------------------------------------------------------
        # CASE 1: PRIMARY already covers scene.
        # ------------------------------------------------------

        if available >= target:
            append_piece(
                segment_id=segment_id,
                role="PRIMARY",
                asset=primary,
                source_duration=target,
                output_duration=target,
                playback_speed=1.0,
            )
            continue

        required_speed = (
            available / target
            if target > 0
            else 1.0
        )

        # ------------------------------------------------------
        # CASE 2: mild slowdown (>= 0.90x) completes scene.
        # ------------------------------------------------------

        if required_speed >= PREFERRED_PLAYBACK_SPEED:
            append_piece(
                segment_id=segment_id,
                role="PRIMARY",
                asset=primary,
                source_duration=available,
                output_duration=target,
                playback_speed=required_speed,
            )
            continue

        # ------------------------------------------------------
        # CASE 3: 0.85x–0.90x completes scene.
        #
        # We accept the deeper slowdown because it avoids inserting
        # a tiny extra shot.
        # ------------------------------------------------------

        if required_speed >= HARD_MIN_PLAYBACK_SPEED:
            append_piece(
                segment_id=segment_id,
                role="PRIMARY",
                asset=primary,
                source_duration=available,
                output_duration=target,
                playback_speed=required_speed,
            )
            continue

        # ------------------------------------------------------
        # CASE 4: PRIMARY cannot safely cover scene alone.
        #
        # Start at preferred 0.90x and let an explicitly approved
        # backup cover the remainder.
        # ------------------------------------------------------

        primary_output = min(
            target,
            available / PREFERRED_PLAYBACK_SPEED,
        )

        remaining = max(
            0.0,
            target - primary_output,
        )

        usable_backups = [
            backup
            for backup in backups
            if asset_duration(backup)
            >= MIN_BACKUP_OUTPUT_SECONDS
        ]

        # If the remaining cut would be a flash (< 0.75s), allocate
        # a proper 0.75s backup shot and reduce slowdown instead.
        #
        # Example:
        #   primary available = 4.125
        #   target = 5.000
        #
        # Instead of:
        #   4.583 primary @ 0.90x + 0.417 backup
        #
        # use:
        #   4.250 primary @ 0.97x + 0.750 backup
        if (
            usable_backups
            and 0.0001 < remaining
            < MIN_BACKUP_OUTPUT_SECONDS
            and target > MIN_BACKUP_OUTPUT_SECONDS
        ):
            desired_primary_output = (
                target - MIN_BACKUP_OUTPUT_SECONDS
            )

            if desired_primary_output >= available:
                candidate_speed = (
                    available
                    / desired_primary_output
                )

                if (
                    candidate_speed
                    >= HARD_MIN_PLAYBACK_SPEED
                    and candidate_speed <= 1.0
                ):
                    primary_output = (
                        desired_primary_output
                    )
                    remaining = (
                        MIN_BACKUP_OUTPUT_SECONDS
                    )

        primary_speed = (
            available / primary_output
            if primary_output > 0
            else 1.0
        )

        append_piece(
            segment_id=segment_id,
            role="PRIMARY",
            asset=primary,
            source_duration=available,
            output_duration=primary_output,
            playback_speed=primary_speed,
        )

        # ------------------------------------------------------
        # Explicit approved backups only.
        # ------------------------------------------------------

        for backup in usable_backups:
            if remaining <= 0.0001:
                break

            backup_available = asset_duration(
                backup
            )

            use = min(
                backup_available,
                remaining,
            )

            # Never intentionally create a micro-cut.
            if use < MIN_BACKUP_OUTPUT_SECONDS:
                continue

            append_piece(
                segment_id=segment_id,
                role="BACKUP",
                asset=backup,
                source_duration=use,
                output_duration=use,
                playback_speed=1.0,
            )

            remaining -= use

        remaining = max(
            0.0,
            remaining,
        )

        if remaining > 0.01:
            segment_shortfalls.append(
                {
                    "segment_id": segment_id,
                    "shortfall": round(
                        remaining,
                        6,
                    ),
                }
            )

    # If there are too few scenes to span the whole audio, report
    # that as an explicit timeline-tail shortfall instead of letting
    # combine_videos() silently loop.
    unsegmented_shortfall = max(
        0.0,
        required_duration - total_scene_target,
    )

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

    primary_count = 0
    backup_count = 0

    for piece in timeline.pieces:
        segment_id = str(
            piece["segment_id"]
        )

        segment = segment_map.get(
            segment_id,
            {},
        )

        asset = piece["asset"]

        decisions.append(
            _selection_decision_from_plan_asset(
                asset,
                segment,
            )
        )

        if piece["role"] == "PRIMARY":
            primary_count += 1
        else:
            backup_count += 1

    return SimpleNamespace(
        decisions=tuple(decisions),
        primary_count=primary_count,
        backup_count=backup_count,
        timeline=timeline,
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
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-ss", "00:00:01", "-i", source_path.as_posix(), "-frames:v", "1", "-q:v", "3", thumb.as_posix()],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
        return thumb if thumb.exists() and thumb.stat().st_size > 0 else None
    except Exception:
        return None


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


def visual_queries_for_review_segments(
    script_text: str,
    segment_count: int,
    existing_terms: list[str] | tuple[str, ...] | None = None,
) -> list[tuple[str, ...]]:
    """Build scene-scoped Asset Search V2 queries for Human Review."""
    fragments = split_script_for_segments(script_text, segment_count)
    return [
        build_visual_queries_v2(fragment, existing_terms or ())
        for fragment in fragments
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
    clip_duration = float(getattr(getattr(selection_result, "options", None), "clip_duration", 5) or 5)
    script_fragments = split_script_for_segments(script_text, len(selected_decisions))
    segment_queries = visual_queries_for_review_segments(
        script_text,
        len(selected_decisions),
        tuple(getattr(selection_result, "search_terms", ()) or ()),
    )
    review_inputs = [
        (
            decision,
            script_fragments[index],
            segment_queries[index] if index < len(segment_queries) else (),
        )
        for index, decision in enumerate(selected_decisions)
        if index < len(script_fragments)
        and str(script_fragments[index] or "").strip()
    ]
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

    for decision, _script_fragment, queries in review_inputs:
        original_candidate = decision.candidate

        ranked_candidates = _ranked_segment_candidates(
            original_candidate,
            all_candidates,
            queries,
        )

        candidate, forced_repeat = _select_segment_candidate(
            ranked_candidates,
            used_selected_asset_uids,
        )

        selected_uid = candidate_uid(candidate)

        if selected_uid:
            used_selected_asset_uids.add(selected_uid)

        reserved_segments.append(
            (
                decision,
                _script_fragment,
                queries,
                original_candidate,
                ranked_candidates,
                candidate,
                forced_repeat,
            )
        )

    # ----------------------------------------------------------
    # PASS 2
    #
    # Allocate suggestions fairly.
    #
    # Old behavior filled 3 suggestions for early scenes first,
    # starving later scenes. V3 uses round-robin:
    #
    #   every scene gets suggestion #1 before anyone gets #2;
    #   every scene gets #2 before anyone gets #3.
    #
    # All visible asset_uids remain globally unique.
    # ----------------------------------------------------------

    used_suggestion_asset_uids: set[str] = set()
    assigned_suggestions: list[list[Any]] = [
        []
        for _ in reserved_segments
    ]

    candidate_queues: list[list[Any]] = []

    for reserved in reserved_segments:
        (
            decision,
            script_fragment,
            queries,
            original_candidate,
            ranked_candidates,
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

            # No PRIMARY may appear anywhere as suggestion.
            if alt_uid in used_selected_asset_uids:
                continue

            queue.append(alt)

        candidate_queues.append(queue)

    # First give every scene one option, then second, then third.
    for slot in range(3):
        for segment_index, queue in enumerate(
            candidate_queues
        ):
            if len(
                assigned_suggestions[segment_index]
            ) > slot:
                continue

            chosen = None

            for alt in queue:
                alt_uid = candidate_uid(alt)

                if (
                    not alt_uid
                    or alt_uid
                    in used_suggestion_asset_uids
                ):
                    continue

                chosen = alt
                break

            if chosen is None:
                continue

            chosen_uid = candidate_uid(chosen)

            used_suggestion_asset_uids.add(
                chosen_uid
            )

            assigned_suggestions[
                segment_index
            ].append(chosen)

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
            candidate,
            forced_repeat,
        ) = reserved

        selected_uid = candidate_uid(candidate)

        selected_decision = (
            decision
            if candidate_uid(
                original_candidate
            ) == selected_uid
            else None
        )

        selected_preview, selected_preview_warnings = (
            ensure_candidate_preview(
                candidate,
                thumbnails_dir,
            )
        )

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

        selected_asset["preview"] = (
            selected_preview
        )

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

        for alt in assigned_suggestions[
            index - 1
        ]:
            alt_preview, alt_preview_warnings = (
                ensure_candidate_preview(
                    alt,
                    thumbnails_dir,
                )
            )

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

            payload["preview"] = alt_preview

            if alt_thumb_url:
                payload[
                    "thumbnail_url"
                ] = alt_thumb_url

            alternatives.append(payload)

            segment_warnings.extend(
                alt_preview_warnings
            )

        start = (
            index - 1
        ) * clip_duration

        end = min(
            float(
                duration
                or index * clip_duration
            ),
            start + clip_duration,
        )

        segments.append(
            {
                "segment_id": (
                    f"segment-{index:03d}"
                ),
                "start": start,
                "end": end,
                "duration": max(
                    0.0,
                    end - start,
                ),
                "script_text": (
                    script_fragment
                ),
                "search_terms": [
                    str(query)
                    for query in queries
                ] or [
                    str(
                        getattr(
                            candidate,
                            "search_term",
                            "",
                        )
                        or ""
                    )
                ],
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
                        selected_asset.get(
                            "asset_uid"
                        )
                    ),
                    "final_selected_asset_uid": (
                        selected_asset.get(
                            "asset_uid"
                        )
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
        "review_required": True,
        "review_status": STATUS_PENDING,
        "created_at": existing.get("created_at") or utc_timestamp(),
        "updated_at": utc_timestamp(),
        "segments": segments,
        "warnings": collect_warnings(segments, aspect_ratio),
    }
    plan["coverage"] = coverage_summary(plan)
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

    authorized_elsewhere = _authorized_asset_location(
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
        choices = _unique_assets_by_uid(
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
            for item in _unique_assets_by_uid(backups)
            if _asset_uid_value(item) != asset_uid
        ]

        # The new PRIMARY must not remain in SUGGESTED.
        # Put the old PRIMARY back into suggestions so the human can
        # undo/reconsider the choice.
        suggestion_pool = _unique_assets_by_uid(
            [old_primary] + alternatives
        )

        segment["alternatives"] = [
            item
            for item in suggestion_pool
            if _asset_uid_value(item) != asset_uid
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

    plan["coverage"] = coverage_summary(plan)
    plan["updated_at"] = utc_timestamp()

    write_json_atomic(plan_file, plan)
    return plan


def approve_plan(
    plan_file: Path,
    project_root: str | Path | None = None,
    *,
    allow_insufficient_coverage: bool = False,
) -> dict[str, Any]:
    plan = normalize_plan_editorial_fields(read_json(plan_file))

    if plan.get("review_status") == STATUS_APPROVED:
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

    if errors:
        raise ValueError(
            "cannot approve production plan:\n- "
            + "\n- ".join(errors)
        )

    plan["schema_version"] = SCHEMA_VERSION
    plan["coverage"] = coverage
    plan["review_status"] = STATUS_APPROVED
    plan["review_required"] = False
    plan["reviewed_at"] = utc_timestamp()
    plan["reviewed_by"] = "human"
    plan["updated_at"] = plan["reviewed_at"]

    write_json_atomic(plan_file, plan)

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
