#!/usr/bin/env python3
"""Minimal Streamlit UI for human-in-the-loop production plans."""

from __future__ import annotations

from pathlib import Path
import sys

import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.custom import human_review


def discover_plans(status: str = human_review.STATUS_PENDING) -> list[Path]:
    root = human_review.review_root(PROJECT_ROOT)
    plans = sorted(root.glob("*/*/production-plan.json"))
    result = []
    for plan_file in plans:
        try:
            plan = human_review.read_json(plan_file)
        except Exception:
            continue
        if plan.get("review_status") == status:
            result.append(plan_file)
    return result


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

                if st.button(
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

    with approve:
        if st.button(
            "APPROVE JOB",
            type="primary",
        ):
            try:
                human_review.approve_plan(
                    plan_file,
                    project_root=PROJECT_ROOT,
                    allow_insufficient_coverage=allow_short,
                )
            except ValueError as exc:
                st.error(str(exc))
            else:
                st.success(
                    "Approved and queued for Night Runner."
                )
                st.rerun()

    with reject:
        if st.button("REJECT JOB"):
            human_review.reject_plan(plan_file)
            st.warning("Rejected.")
            st.rerun()


if __name__ == "__main__":
    main()
