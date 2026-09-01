#!/usr/bin/env python3
"""Minimal Streamlit UI for human-in-the-loop production plans."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import sys
from urllib.parse import urlencode

import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.custom import human_review
from scripts import content_ingest, review_preparation


def _clean_text(value: object) -> str:
    return str(value or "").strip()


def _is_content_job_plan(plan: Mapping[str, object]) -> bool:
    """Recognize both legacy and current automation content-job plans."""
    if isinstance(plan.get("content_job"), Mapping):
        return True

    # Current automation plans do not yet persist content_job metadata.
    # Their task identity is generated from deterministic batch ids:
    # batch-content-<niche>-<content_id>-<stem>.
    task_id = _clean_text(plan.get("task_id"))
    if task_id.startswith("batch-content-"):
        return True

    # Keep a second deterministic signal in case task-id formatting changes.
    for key in ("audio_path", "script_path"):
        value = _clean_text(plan.get(key)).replace("\\", "/")
        if "/storage/content_jobs/" in value:
            return True

    return False


def should_enqueue_nightly(plan: Mapping[str, object]) -> bool:
    """Content-job plans await external production scheduling after review."""
    return not _is_content_job_plan(plan)


def discover_plan_files() -> list[Path]:
    """Return all readable plans, including retained diagnostic plans."""
    root = human_review.review_root(PROJECT_ROOT)
    return sorted(root.glob("*/*/production-plan.json"))


def content_job_niche_id(plan: Mapping[str, object]) -> str | None:
    """Return the content-job niche identity embedded in a review plan."""
    content_job = plan.get("content_job")
    if isinstance(content_job, Mapping):
        niche_id = _clean_text(content_job.get("niche_id"))
        if niche_id:
            return niche_id

    content_id = plan_content_id(plan)
    for key in ("audio_path", "script_path"):
        parts = Path(_clean_text(plan.get(key))).as_posix().split("/")
        try:
            index = parts.index("content_jobs")
            if parts[index + 2] == content_id:
                return parts[index + 1] or None
        except (ValueError, IndexError):
            continue
    return None


def review_public_state(plan: Mapping[str, object]) -> tuple[str, str | None]:
    """Use durable preparation state as the authority for content-job review.

    Legacy batch plans have no content-job state machine and retain their
    established pending-review behavior.
    """
    if not _is_content_job_plan(plan):
        return "HUMAN_REVIEW_READY", None
    content_id = plan_content_id(plan)
    niche_id = content_job_niche_id(plan)
    if not content_id or not niche_id:
        return "PREPARING_REVIEW", None
    path = review_preparation.state_path(
        niche_id, content_id, job_root=content_ingest.DEFAULT_JOB_ROOT,
    )
    try:
        record = human_review.read_json(path)
    except (OSError, ValueError):
        return "PREPARING_REVIEW", None
    if not isinstance(record, Mapping):
        return "PREPARING_REVIEW", None
    if (
        record.get("content_id") != content_id
        or record.get("niche_id") != niche_id
    ):
        return "PREPARING_REVIEW", None
    return (
        review_preparation.public_state(record),
        review_preparation.sheet_error_message(record),
    )


def is_actionable_review(plan: Mapping[str, object]) -> bool:
    """Whether this plan may expose editor controls to a human reviewer."""
    return (
        plan.get("review_status") == human_review.STATUS_PENDING
        and review_public_state(plan)[0] == "HUMAN_REVIEW_READY"
    )


def discover_plans(status: str = human_review.STATUS_PENDING) -> list[Path]:
    """Return only plans whose canonical public state permits review."""
    result = []
    for plan_file in discover_plan_files():
        try:
            plan = human_review.read_json(plan_file)
        except Exception:
            continue
        if plan.get("review_status") == status and is_actionable_review(plan):
            result.append(plan_file)
    return result


def review_relative_url(content_id: str) -> str:
    """Return the relative, safely encoded deep link for a content review."""
    return f"?{urlencode({'content_id': content_id})}"


def plan_content_id(plan: Mapping[str, object]) -> str | None:
    """Return the immutable content identity for a content-job review plan."""
    content_job = plan.get("content_job")

    if isinstance(content_job, Mapping):
        content_id = _clean_text(content_job.get("content_id"))
        if content_id:
            return content_id

    # Current automation-generated plans identify the content itself with
    # batch_id while their task_id carries the full deterministic batch name.
    if _is_content_job_plan(plan):
        content_id = _clean_text(plan.get("batch_id"))
        if content_id:
            return content_id

    return None


def find_plan_by_content_id(plans: list[Path], content_id: str) -> Path | None:
    """Find a pending plan by its immutable content-job identity."""
    for plan_file in plans:
        try:
            plan = human_review.read_json(plan_file)
        except Exception:
            continue
        if plan_content_id(plan) == content_id:
            return plan_file
    return None


def filter_plans_for_content_id(plans: list[Path], content_id: str | None) -> list[Path]:
    """Preserve normal selection unless a content-job deep link was supplied."""
    if content_id is None:
        return plans
    selected_plan = find_plan_by_content_id(plans, content_id)
    return [selected_plan] if selected_plan is not None else []


def alternative_authorized_elsewhere(
    plan: Mapping[str, object],
    segment_id: str,
    asset_uid: str,
) -> str | None:
    """Use the canonical Review authorization rule for an alternative."""
    return human_review.authorized_asset_location(
        plan,
        asset_uid,
        exclude_segment_id=segment_id,
    )


def alternative_review_previewable(alternative: Mapping[str, object]) -> bool:
    """Read the persisted canonical decision, with a legacy-plan fallback."""
    decision = alternative.get("review_previewable")
    if isinstance(decision, bool):
        return decision
    return human_review.review_previewable(alternative.get("preview"))


def non_previewable_primary_segments(plan: Mapping[str, object]) -> list[str]:
    """Return PRIMARY segments that cannot be inspected in Human Review.

    This deliberately delegates the decision to Human Review's canonical
    preview helper; the UI must not infer it from provider-specific fields.
    """
    return [
        str(segment.get("segment_id") or "")
        for segment in plan.get("segments") or []
        if isinstance(segment, Mapping)
        and isinstance(segment.get("selected_asset"), Mapping)
        and not human_review.review_previewable(segment["selected_asset"].get("preview"))
    ]


def query_content_id() -> str | None:
    """Read the optional Streamlit deep-link parameter across supported APIs."""
    params = getattr(st, "query_params", None)
    if params is not None:
        value = params.get("content_id")
    else:
        legacy_params = getattr(st, "experimental_get_query_params", lambda: {})()
        value = legacy_params.get("content_id")
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def show_asset(asset: dict, key: str) -> None:
    preview = human_review.resolve_candidate_preview(asset)
    if preview:
        st.image(preview, use_container_width=True)
    else:
        st.warning("NO PREVIEW AVAILABLE")
    st.caption(f"{asset.get('provider') or asset.get('source')} · {asset.get('asset_uid')}")


def show_flip_checkbox(plan_file: Path, segment_id: str, asset: dict, key: str) -> None:
    uid = str(asset.get("asset_uid") or "")
    current = human_review.asset_flip_horizontal(asset)
    enabled = st.checkbox(
        "Flip horizontal",
        value=current,
        key=f"flip-{key}",
    )
    if enabled != current and uid:
        try:
            human_review.set_asset_flip_horizontal(
                plan_file,
                segment_id,
                uid,
                enabled,
            )
        except ValueError as exc:
            st.error(str(exc))
        else:
            st.rerun()


def _asset_duration(asset: dict) -> float:
    metadata = asset.get("metadata") if isinstance(asset.get("metadata"), dict) else {}
    return float(metadata.get("duration") or 0)


def _asset_metadata(asset: dict) -> dict:
    return asset.get("metadata") if isinstance(asset.get("metadata"), dict) else {}


def _asset_ratio(metadata: dict) -> str:
    try:
        width = int(metadata.get("width"))
        height = int(metadata.get("height"))
    except (TypeError, ValueError):
        return ""
    if width <= 0 or height <= 0:
        return ""
    from math import gcd
    divisor = gcd(width, height)
    return f"{width // divisor}x{height // divisor}"


def show_asset_metadata(asset: dict) -> None:
    """Render concise, optional editorial metadata supplied by Asset Hub."""
    metadata = _asset_metadata(asset)
    duration = _asset_duration(asset)
    ratio = _asset_ratio(metadata)
    technical = []
    if duration > 0:
        technical.append(f"Duration: {duration:.1f}s")
    if ratio:
        technical.append(ratio)
    if technical:
        st.caption(" · ".join(technical))

    for label, key in (("Topic", "primary_topic"), ("Theme", "primary_theme")):
        value = str(metadata.get(key) or "").strip()
        if value:
            st.caption(f"{label}: {value}")

    people = []
    count = metadata.get("people_count")
    if count not in (None, ""):
        people.append(str(count))
    for key in ("visual_presentation", "person_visibility"):
        value = str(metadata.get(key) or "").strip()
        if value:
            people.append(value)
    if people:
        st.caption(f"People: {' · '.join(people)}")

    for label, key in (("Visual", "visual_description"), ("Action", "action_description")):
        value = str(metadata.get(key) or "").strip()
        if value:
            st.caption(f"{label}: {value}")


def _reorder_backup(plan_file: Path, segment_id: str, backups: list[dict], index: int, delta: int) -> None:
    target = index + delta
    if target < 0 or target >= len(backups):
        return
    ordered = list(backups)
    ordered[index], ordered[target] = ordered[target], ordered[index]
    human_review.reorder_segment_backups(
        plan_file,
        segment_id,
        [str(item.get("asset_uid") or "") for item in ordered],
    )


def script_preview(text: object, limit: int = 420) -> str:
    value = " ".join(str(text or "").split())
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 3)].rstrip() + "..."


def main() -> None:
    st.set_page_config(page_title="Human Review", layout="wide")
    st.title("Human Review")

    content_id = query_content_id()
    if content_id is not None:
        diagnostic_plans = []
        for path in discover_plan_files():
            try:
                candidate = human_review.read_json(path)
            except Exception:
                continue
            if candidate.get("review_status") == human_review.STATUS_PENDING:
                diagnostic_plans.append(path)
        plans = filter_plans_for_content_id(diagnostic_plans, content_id)
        if not plans:
            st.error(f"No pending review was found for content_id={content_id!r}.")
            return
        selected_plan = human_review.read_json(plans[0])
        public_state, safe_error_message = review_public_state(selected_plan)
        if public_state == "ERROR":
            st.error("Review is unavailable because preparation failed.")
            if safe_error_message:
                st.caption(safe_error_message)
            return
        if public_state != "HUMAN_REVIEW_READY":
            st.info("Review is still being prepared and is not ready yet.")
            return
    else:
        plans = discover_plans()
        if not plans:
            st.info("No pending review jobs.")
            return

    labels = []
    for path in plans:
        plan = human_review.read_json(path)
        labels.append(f"{plan.get('batch_id')} / {plan.get('stem')}")
    selected_index = st.sidebar.radio("Pending jobs", range(len(plans)), format_func=lambda idx: labels[idx])
    plan_file = plans[selected_index]
    plan = human_review.normalize_plan_editorial_fields(
        human_review.read_json(plan_file)
    )

    st.subheader(f"{plan.get('batch_id')} / {plan.get('stem')}")
    st.caption(str(plan_file))
    for warning in plan.get("warnings") or []:
        st.warning(f"{warning.get('code')}: {warning.get('message')}")

    flip_all, flip_none = st.columns(2)

    with flip_all:
        if st.button("Flip todos"):
            human_review.set_all_visible_flip_horizontal(plan_file, True)
            st.rerun()

    with flip_none:
        if st.button("Sin flip todos"):
            human_review.set_all_visible_flip_horizontal(plan_file, False)
            st.rerun()

    coverage = human_review.coverage_summary(plan)

    st.markdown("### Approved visual coverage")

    c1, c2, c3, c4 = st.columns(4)

    c1.metric(
        "Audio",
        f"{coverage['required_duration']:.1f}s",
    )
    c2.metric(
        "PRIMARY",
        f"{coverage['primary_duration']:.1f}s",
    )
    c3.metric(
        "BACKUPS",
        f"{coverage['backup_duration']:.1f}s",
    )
    c4.metric(
        "Approved",
        f"{coverage['approved_duration']:.1f}s",
    )

    if coverage["deficit"] > 0.01:
        st.warning(
            "INSUFFICIENT VISUAL COVERAGE · "
            f"{coverage['deficit']:.1f}s missing"
        )
    else:
        st.success(
            "Visual coverage is sufficient · "
            f"{coverage['coverage_ratio'] * 100:.1f}%"
        )

    segments = plan.get("segments") or []

    timeline = human_review.render_timeline_from_plan(
        plan
    )

    pieces_by_segment = {}

    for piece in timeline.pieces:
        pieces_by_segment.setdefault(
            str(piece["segment_id"]),
            [],
        ).append(piece)

    shortfall_by_segment = {
        str(item["segment_id"]): float(
            item["shortfall"]
        )
        for item in timeline.segment_shortfalls
    }

    for segment_index, segment in enumerate(segments):
        segment_id = str(
            segment.get("segment_id")
            or ""
        )

        st.divider()
        st.markdown(
            f"#### {segment_id}"
        )

        st.write(
            script_preview(
                segment.get("script_text")
                or ""
            )
        )

        target = float(
            segment.get("duration")
            or 0
        )

        if (
            segment_index
            == len(segments) - 1
        ):
            target += 0.10

        scene_pieces = (
            pieces_by_segment.get(
                segment_id,
                [],
            )
        )

        primary_piece = next(
            (
                piece
                for piece in scene_pieces
                if piece["role"]
                == "PRIMARY"
            ),
            None,
        )

        backup_pieces = [
            piece
            for piece in scene_pieces
            if piece["role"]
            == "BACKUP"
        ]

        primary_output = (
            float(
                primary_piece[
                    "output_duration"
                ]
            )
            if primary_piece
            else 0.0
        )

        primary_source = (
            float(
                primary_piece[
                    "source_duration"
                ]
            )
            if primary_piece
            else 0.0
        )

        primary_speed = (
            float(
                primary_piece[
                    "playback_speed"
                ]
            )
            if primary_piece
            else 1.0
        )

        backup_output = sum(
            float(
                piece["output_duration"]
            )
            for piece in backup_pieces
        )

        scene_gap = float(
            shortfall_by_segment.get(
                segment_id,
                0.0,
            )
        )

        segment_metrics = segment.get("coverage") if isinstance(segment.get("coverage"), dict) else {}
        target_duration = float(segment_metrics.get("target_duration") or target)
        covered_duration = float(segment_metrics.get("covered_duration") or (target - scene_gap))
        missing_duration = float(segment_metrics.get("missing_duration") or scene_gap)

        metric_cols = st.columns(5)
        metric_cols[0].metric("TARGET", f"{target_duration:.2f}s")
        metric_cols[1].metric("PRIMARY", f"{primary_output:.2f}s")
        metric_cols[2].metric("BACKUPS", f"{backup_output:.2f}s")
        metric_cols[3].metric("COVERED", f"{covered_duration:.2f}s")
        metric_cols[4].metric("MISSING", f"{missing_duration:.2f}s")

        if missing_duration > 0.01:
            st.warning(f"NEEDS BACKUP · {missing_duration:.2f}s missing")
        else:
            st.success("COVERED")

        if missing_duration > 0.01:
            st.warning(
                f"Scene coverage "
                f"{covered_duration:.2f}"
                f"/{target_duration:.2f}s · "
                f"MISSING {missing_duration:.2f}s"
            )
        else:
            st.success(
                f"Scene coverage "
                f"{target_duration:.2f}/{target_duration:.2f}s"
            )

        if primary_speed < 0.999:
            st.caption(
                f"AUTO SLOWDOWN "
                f"{primary_speed:.3f}x · "
                f"{primary_source:.2f}s source "
                f"→ {primary_output:.2f}s timeline"
            )
        else:
            st.caption(
                f"PRIMARY {primary_output:.2f}s"
                f" · BACKUPS {backup_output:.2f}s"
                f" · TARGET {target:.2f}s"
            )

        selected = (
            segment.get("selected_asset")
            or {}
        )

        alternatives = [
            item
            for item in (
                segment.get("alternatives")
                or []
            )
            if isinstance(item, dict)
        ][:3]

        backups = [
            item
            for item in (
                segment.get("backup_assets")
                or []
            )
            if isinstance(item, dict)
        ]

        backup_uids = {
            str(
                item.get("asset_uid")
                or ""
            )
            for item in backups
        }

        cols = st.columns(
            1 + len(alternatives)
        )

        with cols[0]:
            st.markdown(
                "**PRIMARY ✓**"
            )

            if selected.get("asset_uid"):
                show_asset(
                    selected,
                    f"{segment_id}-selected",
                )

                show_flip_checkbox(
                    plan_file,
                    segment_id,
                    selected,
                    f"{segment_id}-selected",
                )

                show_asset_metadata(selected)
                ranking_v2 = selected.get("ranking_v2")
                if isinstance(ranking_v2, dict):
                    score = ranking_v2.get("score")
                    reasons = ", ".join(ranking_v2.get("reason_codes") or [])
                    penalties = ", ".join(ranking_v2.get("penalty_codes") or [])
                    st.caption(f"Score {score:.2f}" if isinstance(score, (int, float)) else "Score n/a")
                    if reasons:
                        st.caption("+ " + reasons)
                    if penalties:
                        st.caption("- " + penalties)
            else:
                st.warning("REVIEW REQUIRED")
                st.caption("0 visible PRIMARY candidates")

        for index, alternative in enumerate(
            alternatives,
            1,
        ):
            with cols[index]:
                uid = str(
                    alternative.get(
                        "asset_uid"
                    )
                    or ""
                )

                duration = _asset_duration(alternative)

                is_backup = (
                    uid in backup_uids
                )

                if is_backup:
                    st.markdown(
                        f"**SUGGESTION {index}"
                        " · BACKUP ✓**"
                    )
                else:
                    st.markdown(
                        f"**SUGGESTION {index}**"
                    )

                show_asset(
                    alternative,
                    (
                        f"{segment_id}"
                        f"-alt-{index}"
                    ),
                )

                show_flip_checkbox(
                    plan_file,
                    segment_id,
                    alternative,
                    (
                        f"{segment_id}"
                        f"-alt-{index}"
                    ),
                )

                if scene_gap > 0.01:
                    potential = min(
                        duration,
                        max(
                            scene_gap,
                            human_review.MIN_BACKUP_OUTPUT_SECONDS,
                        ),
                    )
                else:
                    potential = 0.0

                st.caption(
                    f"source {duration:.2f}s"
                    + (
                        f" · can close "
                        f"{scene_gap:.2f}s gap"
                        if (
                            scene_gap > 0.01
                            and duration
                            >= human_review.MIN_BACKUP_OUTPUT_SECONDS
                        )
                        else ""
                    )
                )
                show_asset_metadata(alternative)

                authorized_elsewhere = alternative_authorized_elsewhere(
                    plan,
                    segment_id,
                    uid,
                )
                # This is the canonical decision persisted while the review
                # plan was prepared.  A retained scarcity candidate may be
                # useful provenance, but cannot be promoted blind.
                if not alternative_review_previewable(alternative):
                    st.caption("NO PREVIEW AVAILABLE")
                elif authorized_elsewhere:
                    st.caption(f"USED IN {authorized_elsewhere}")
                elif st.button(
                    "MAKE PRIMARY",
                    key=(
                        f"replace-"
                        f"{segment_id}-"
                        f"{index}"
                    ),
                ):
                    try:
                        human_review.replace_segment_asset(
                            plan_file,
                            segment_id,
                            uid,
                        )
                    except ValueError as exc:
                        st.error(str(exc))
                    else:
                        st.rerun()

                if is_backup:
                    if st.button(
                        "REMOVE BACKUP",
                        key=(
                            f"backup-remove-"
                            f"{segment_id}-"
                            f"{index}"
                        ),
                    ):
                        human_review.set_segment_backup(
                            plan_file,
                            segment_id,
                            uid,
                            False,
                        )
                        st.rerun()

                elif duration >= human_review.MIN_BACKUP_OUTPUT_SECONDS:
                    if st.button(
                        "Add as BACKUP",
                        key=(
                            f"backup-add-"
                            f"{segment_id}-"
                            f"{index}"
                        ),
                    ):
                        try:
                            human_review.set_segment_backup(
                                plan_file,
                                segment_id,
                                uid,
                                True,
                            )
                        except ValueError as exc:
                            st.error(str(exc))
                        else:
                            st.rerun()

                else:
                    st.caption(
                        "Too short for backup use"
                    )

        if backups:
            st.markdown("**BACKUPS timeline order**")
            for backup_index, backup in enumerate(backups):
                uid = str(backup.get("asset_uid") or "")
                row = st.columns([0.9, 2.7, 1.1, 1.1, 1.5, 1.5])
                row[0].write(f"{backup_index + 1}")
                row[1].caption(f"{uid} · {_asset_duration(backup):.2f}s")
                if row[2].button("UP", key=f"backup-up-{segment_id}-{uid}", disabled=backup_index == 0):
                    try:
                        _reorder_backup(plan_file, segment_id, backups, backup_index, -1)
                    except ValueError as exc:
                        st.error(str(exc))
                    else:
                        st.rerun()
                if row[3].button("DOWN", key=f"backup-down-{segment_id}-{uid}", disabled=backup_index == len(backups) - 1):
                    try:
                        _reorder_backup(plan_file, segment_id, backups, backup_index, 1)
                    except ValueError as exc:
                        st.error(str(exc))
                    else:
                        st.rerun()
                if row[4].button("PROMOTE", key=f"backup-promote-{segment_id}-{uid}"):
                    try:
                        human_review.promote_segment_backup(plan_file, segment_id, uid)
                    except ValueError as exc:
                        st.error(str(exc))
                    else:
                        st.rerun()
                if row[5].button("REMOVE", key=f"backup-list-remove-{segment_id}-{uid}"):
                    try:
                        human_review.set_segment_backup(plan_file, segment_id, uid, False)
                    except ValueError as exc:
                        st.error(str(exc))
                    else:
                        st.rerun()

    st.divider()

    coverage = human_review.coverage_summary(
        human_review.read_json(plan_file)
    )

    allow_short = False

    if coverage["deficit"] > 0.01:
        allow_short = st.checkbox(
            "Approve despite insufficient visual coverage "
            "(renderer may need emergency looping)",
            value=False,
        )

    approve, reject = st.columns(2)
    unavailable_primary_segments = non_previewable_primary_segments(plan)

    with approve:
        if unavailable_primary_segments:
            st.caption("NO PREVIEW AVAILABLE")
        elif st.button(
            "APPROVE JOB",
            type="primary",
        ):
            enqueue_nightly = should_enqueue_nightly(plan)
            try:
                human_review.approve_plan(
                    plan_file,
                    project_root=PROJECT_ROOT,
                    allow_insufficient_coverage=allow_short,
                    enqueue_nightly=enqueue_nightly,
                )
            except ValueError as exc:
                st.error(str(exc))
            else:
                if enqueue_nightly:
                    st.success("Approved and queued for Night Runner.")
                else:
                    st.success("Approved. Awaiting production scheduling.")
                st.rerun()

    with reject:
        if st.button("REJECT JOB"):
            human_review.reject_plan(plan_file)
            st.warning("Rejected.")
            st.rerun()


if __name__ == "__main__":
    main()
