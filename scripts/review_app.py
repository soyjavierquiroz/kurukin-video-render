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
    plan = human_review.read_json(plan_file)

    st.subheader(f"{plan.get('batch_id')} / {plan.get('stem')}")
    st.caption(str(plan_file))
    for warning in plan.get("warnings") or []:
        st.warning(f"{warning.get('code')}: {warning.get('message')}")

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

        if scene_gap > 0.01:
            st.warning(
                f"Scene coverage "
                f"{target - scene_gap:.2f}"
                f"/{target:.2f}s · "
                f"MISSING {scene_gap:.2f}s"
            )
        else:
            st.success(
                f"Scene coverage "
                f"{target:.2f}/{target:.2f}s"
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

            show_asset(
                selected,
                f"{segment_id}-selected",
            )

            metadata = (
                selected.get("metadata")
                if isinstance(
                    selected.get(
                        "metadata"
                    ),
                    dict,
                )
                else {}
            )

            st.caption(
                "source duration "
                f"{float(metadata.get('duration') or 0):.2f}s"
            )

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

                metadata = (
                    alternative.get(
                        "metadata"
                    )
                    if isinstance(
                        alternative.get(
                            "metadata"
                        ),
                        dict,
                    )
                    else {}
                )

                duration = float(
                    metadata.get("duration")
                    or 0
                )

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

                elif (
                    scene_gap > 0.01
                    and duration
                    >= human_review.MIN_BACKUP_OUTPUT_SECONDS
                ):
                    if st.button(
                        "ADD BACKUP",
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

                elif scene_gap <= 0.01:
                    st.caption(
                        "Covered by PRIMARY / slowdown"
                    )

                else:
                    st.caption(
                        "Too short for backup use"
                    )


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
