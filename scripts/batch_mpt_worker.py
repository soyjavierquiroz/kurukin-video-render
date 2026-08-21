#!/usr/bin/env python3
"""Container-side MoneyPrinterTurbo work for batch production."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path
import sys


def _read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("manifest must be a JSON object")
    return data


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")



def _material_source_policy(manifest: dict) -> dict:
    from app.custom.material_source_policy import (
        AssetHubCatalogPolicy,
        AssetHubIncludePolicy,
        MaterialProviderPolicy,
        MaterialSourcePolicy,
        PROVIDER_ASSET_HUB,
        open_sources_policy,
    )

    title = str(manifest.get("material_title") or "").strip()

    if not title:
        return open_sources_policy().to_dict()

    return MaterialSourcePolicy(
        providers=MaterialProviderPolicy((PROVIDER_ASSET_HUB,)),
        asset_hub=AssetHubCatalogPolicy(
            include=AssetHubIncludePolicy(
                titles=(title,),
            )
        ),
    ).to_dict()



def _stage_human_review_timeline(
    *,
    plan: dict,
    selection: object,
    acquisition: object,
    task_id: str,
) -> tuple[list[object], Path]:
    """
    Stage exact Human Review timeline clips.

    Every staged MP4 already contains its approved output duration
    and playback speed. MPT can therefore keep using its ordinary
    sequential local-material renderer.
    """

    from app.custom import human_review
    from app.models.schema import MaterialInfo

    timeline = (
        human_review.render_timeline_from_plan(
            plan
        )
    )

    if timeline.shortfall > 0.01:
        details = ", ".join(
            f"{item['segment_id']}="
            f"{item['shortfall']:.2f}s"
            for item in timeline.segment_shortfalls
        )

        raise RuntimeError(
            "cannot stage Human Review timeline; "
            f"shortfall remains: {details}"
        )

    decisions = list(
        getattr(
            selection,
            "decisions",
            (),
        )
        or ()
    )

    materials = list(
        getattr(
            acquisition,
            "materials",
            (),
        )
        or ()
    )

    if len(decisions) != len(materials):
        raise RuntimeError(
            "Human Review acquisition "
            "decision/material count mismatch"
        )

    material_by_uid = {}

    for decision, info in zip(
        decisions,
        materials,
    ):
        uid = str(
            getattr(
                decision.candidate,
                "canonical_id",
                "",
            )
            or ""
        )

        if not uid:
            raise RuntimeError(
                "Human Review acquisition has "
                "material without asset_uid"
            )

        material_by_uid[uid] = info

    timeline_dir = Path(
        "/MoneyPrinterTurbo/storage/local_videos/"
        "human-review"
    ) / task_id

    timeline_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    staged = []

    for index, piece in enumerate(
        timeline.pieces,
        1,
    ):
        uid = str(
            piece["asset_uid"]
        )

        segment_id = str(
            piece["segment_id"]
        )

        role = str(
            piece["role"]
        ).lower()

        info = material_by_uid.get(uid)

        if info is None:
            raise RuntimeError(
                f"materialized asset missing: {uid}"
            )

        source = Path(
            str(info.url)
        )

        if not source.is_file():
            raise RuntimeError(
                f"materialized file missing: {source}"
            )

        source_duration = float(
            piece["source_duration"]
        )

        output_duration = float(
            piece["output_duration"]
        )

        playback_speed = float(
            piece["playback_speed"]
        )

        flip_horizontal = bool(
            piece.get(
                "flip_horizontal",
                human_review.asset_flip_horizontal(
                    piece.get("asset")
                ),
            )
        )

        if (
            source_duration <= 0
            or output_duration <= 0
            or playback_speed <= 0
        ):
            raise RuntimeError(
                f"invalid timeline durations for {uid}"
            )

        output = timeline_dir / (
            f"{index:03d}_"
            f"{segment_id}_"
            f"{role}_"
            f"{uid}.mp4"
        )

        # Slow playback by stretching video PTS.
        #
        # Example:
        #   speed 0.90
        #   setpts=(PTS-STARTPTS)/0.90
        filters = [
            (
                f"trim=start=0:"
                f"duration={source_duration:.6f}"
            ),
            (
                "setpts="
                f"(PTS-STARTPTS)/{playback_speed:.6f}"
            ),
        ]
        if flip_horizontal:
            filters.append("hflip")
        vf = ",".join(filters)

        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            source.as_posix(),
            "-map",
            "0:v:0",
            "-an",
            "-sn",
            "-dn",
            "-vf",
            vf,
            "-t",
            f"{output_duration:.6f}",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            output.as_posix(),
        ]

        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        if completed.returncode != 0:
            raise RuntimeError(
                "failed to stage Human Review timeline "
                f"clip {segment_id}/{uid}: "
                f"{completed.stderr[-2000:]}"
            )

        if (
            not output.is_file()
            or output.stat().st_size <= 0
        ):
            raise RuntimeError(
                f"timeline clip missing: {output}"
            )

        staged.append(
            MaterialInfo(
                provider="local",
                url=output.as_posix(),
                duration=max(
                    1,
                    int(
                        output_duration
                        + 0.999
                    ),
                ),
                source_info={
                    "human_review_timeline": True,
                    "segment_id": segment_id,
                    "role": role,
                    "asset_id": uid,
                    "source_duration": (
                        source_duration
                    ),
                    "approved_duration": (
                        output_duration
                    ),
                    "playback_speed": (
                        playback_speed
                    ),
                    "flip_horizontal": (
                        flip_horizontal
                    ),
                },
            )
        )

        print(
            "  timeline "
            f"{segment_id} "
            f"{role.upper()} "
            f"{uid} "
            f"source={source_duration:.3f}s "
            f"output={output_duration:.3f}s "
            f"speed={playback_speed:.3f}x "
            f"flip={str(flip_horizontal).lower()}"
        )

    print(
        "human review staged timeline: "
        f"pieces={len(staged)} "
        f"duration="
        f"{timeline.total_output_duration:.3f}s"
    )

    return staged, timeline_dir


def run_master(manifest: dict) -> dict:
    from app.custom import human_review
    from app.custom.material_acquisition import acquire_selected_materials
    from app.models.schema import VideoParams
    from app.services import task

    production_plan_path = str(manifest.get("production_plan_path") or "")
    params = VideoParams(
        video_subject=manifest["stem"],
        video_script=manifest["script"],
        video_aspect="9:16",
        video_concat_mode="sequential",
        video_clip_duration=5,
        match_materials_to_script=True,
        video_count=1,
        video_source="pexels",
        material_source_policy=_material_source_policy(manifest),
        editorial_profile=manifest.get("editorial_profile") or {},
        custom_audio_file=manifest["audio_file"],
        voice_name="",
        voice_volume=1.0,
        voice_rate=1.0,
        bgm_type="",
        bgm_file="",
        bgm_volume=0,
        subtitle_enabled=False,
        subtitle_correction_enabled=False,
        subtitle_optimization_enabled=False,
    )
    if production_plan_path:
        plan = human_review.read_json(Path(production_plan_path))
        if plan.get("review_status") != human_review.STATUS_APPROVED:
            raise RuntimeError("production plan must be approved before render")
        selection = human_review.selection_result_from_plan(plan)
        print(
            "human review render selection: "
            f"primary={getattr(selection, 'primary_count', 0)} "
            f"backups={getattr(selection, 'backup_count', 0)} "
            f"total={len(getattr(selection, 'decisions', ()))}"
        )
        acquisition = acquire_selected_materials(
            selection_result=selection,
            task_id=manifest["task_id"],
        )

        staged_materials, timeline_dir = (
            _stage_human_review_timeline(
                plan=plan,
                selection=selection,
                acquisition=acquisition,
                task_id=manifest["task_id"],
            )
        )

        params.video_source = "local"
        params.video_materials = staged_materials
        params.material_source_policy = None
        params.video_terms = []
    timeline_dir = locals().get("timeline_dir")

    try:
        result = task.start(
            manifest["task_id"],
            params,
            stop_at="video",
        )
    finally:
        if timeline_dir is not None:
            shutil.rmtree(
                timeline_dir,
                ignore_errors=True,
            )

    final_path = Path(manifest["task_dir"]) / "final-1.mp4"
    if not final_path.is_file() or final_path.stat().st_size <= 0:
        raise RuntimeError(f"MPT did not produce {final_path}")
    return {"ok": True, "result": result, "master": final_path.as_posix()}


def run_review(manifest: dict) -> dict:
    from app.models.schema import VideoParams
    from app.services import task

    params = VideoParams(
        video_subject=manifest["stem"],
        video_script=manifest["script"],
        video_aspect="9:16",
        video_concat_mode="sequential",
        video_clip_duration=5,
        match_materials_to_script=True,
        video_count=1,
        video_source="pexels",
        material_source_policy=_material_source_policy(manifest),
        editorial_profile=manifest.get("editorial_profile") or {},
        custom_audio_file=manifest["audio_file"],
        voice_name="",
        voice_volume=1.0,
        voice_rate=1.0,
        bgm_type="",
        bgm_file="",
        bgm_volume=0,
        subtitle_enabled=False,
        subtitle_correction_enabled=False,
        subtitle_optimization_enabled=False,
    )
    object.__setattr__(
        params,
        "human_review",
        {
            "batch_id": manifest["batch_id"],
            "stem": manifest["stem"],
            "production_plan_path": manifest["production_plan_path"],
            "audio_path": manifest.get("host_audio_file") or manifest["audio_file"],
            "script_path": manifest.get("host_text_file") or manifest["text_file"],
            "visual_style": manifest.get("visual_style", "none"),
            "editorial_profile": manifest.get("editorial_profile") or {},
        },
    )
    result = task.start(manifest["task_id"], params, stop_at="review")
    plan_path = Path(manifest["production_plan_path"])
    if not plan_path.is_file() or plan_path.stat().st_size <= 0:
        raise RuntimeError(f"review plan was not created: {plan_path}")
    return {"ok": True, "result": result, "production_plan_path": plan_path.as_posix()}


def run_subtitles(manifest: dict) -> dict:
    from app.services import subtitle

    subtitle.model_size = "medium"
    subtitle.device = "cpu"
    subtitle.compute_type = "int8"

    subtitle_file = Path(manifest["task_dir"]) / "subtitle.srt"
    raw_audio = manifest["subtitle_audio_file"]
    subtitle.create(raw_audio, subtitle_file.as_posix())
    if not subtitle_file.is_file() or subtitle_file.stat().st_size <= 0:
        raise RuntimeError("Whisper subtitle generation failed: subtitle.srt missing or empty")
    report = subtitle.correct(subtitle_file.as_posix(), manifest["script"])
    _write_json(Path(manifest["task_dir"]) / "subtitle-worker-result.json", report)
    return {"ok": True, "subtitle": subtitle_file.as_posix(), "report": report}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run MPT batch worker stages")
    parser.add_argument("manifest")
    parser.add_argument("--stage", choices=("master", "subtitles", "review"), required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest = _read_json(Path(args.manifest))
        if args.stage == "master":
            result = run_master(manifest)
        elif args.stage == "review":
            result = run_review(manifest)
        else:
            result = run_subtitles(manifest)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
