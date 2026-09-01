#!/usr/bin/env python3
"""Container-side MoneyPrinterTurbo work for batch production."""

from __future__ import annotations

import argparse
import json
import re
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


_FFMPEG_SECRET = re.compile(
    r"(?i)((?:authorization|api[_-]?key|token|secret|password))"
    r"\s*(?:=|:)\s*[^\s,;]+"
)


def _safe_ffmpeg_stderr_tail(stderr: str, *, limit: int = 2000) -> str:
    """Return useful ffmpeg diagnostics without propagating credentials."""
    return _FFMPEG_SECRET.sub(r"\1=<redacted>", str(stderr or ""))[-limit:]


def _task_local_custom_audio(manifest: dict) -> str:
    """Materialize the trusted batch audio inside the MPT-owned task directory.

    MPT v1.3.5 deliberately rejects arbitrary server paths supplied as custom
    audio.  The batch manifest's canonical audio is host-controlled, so copy it
    to a fixed task-local name before passing it through the regular MPT API.
    """
    source = Path(str(manifest["audio_file"])).resolve()
    task_dir = Path(str(manifest["task_dir"])).resolve()
    if not source.is_file() or source.stat().st_size <= 0:
        raise RuntimeError(f"canonical batch audio is missing or empty: {source}")
    task_dir.mkdir(parents=True, exist_ok=True)
    destination = task_dir / f"custom-audio{source.suffix.lower() or '.mp3'}"
    if source != destination:
        shutil.copy2(source, destination)
    return destination.as_posix()



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
    source_policy = str(manifest.get("source_policy") or "").strip()

    if source_policy == "title-exclusive" and not title:
        raise ValueError("source_policy=title-exclusive requires material_title")

    if source_policy != "title-exclusive" and not title:
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
        source_start = max(0.0, float(piece.get("source_start") or 0.0))
        freeze_seconds = max(0.0, float(piece.get("freeze_seconds") or 0.0))

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

        if freeze_seconds > 0:
            # The freeze timeline piece nominally starts at the final 0.04s
            # of the source.  At 24 fps that interval can contain no frame:
            # trim then emits an empty stream and tpad cannot clone it.  Take
            # the final real decoded frame before source_start instead, reset
            # its PTS, then clone it for the approved freeze duration.
            filters = [
                f"trim=start=0:end={source_start:.6f}",
                "reverse",
                "select=eq(n\\,0)",
                "setpts=PTS-STARTPTS",
                # select produces a variable-frame-rate one-frame stream;
                # give tpad a concrete cadence so clone frames are emitted.
                "fps=24",
                f"tpad=stop_mode=clone:stop_duration={freeze_seconds:.6f}",
            ]
            print(
                f"timeline {segment_id} FREEZE asset={uid} duration={freeze_seconds:.3f}s"
            )
        else:
            # Slow playback by stretching video PTS.
            # Example: speed 0.90 -> setpts=(PTS-STARTPTS)/0.90
            filters = [
                f"trim=start={source_start:.6f}:duration={source_duration:.6f}",
                f"setpts=(PTS-STARTPTS)/{playback_speed:.6f}",
            ]
        if role == "extend":
            print(f"timeline timeline-tail EXTEND asset={uid} duration={output_duration:.3f}s")
        elif role == "loop":
            print(f"timeline timeline-tail LOOP asset={uid} duration={output_duration:.3f}s")
        if flip_horizontal:
            filters.append("hflip")
        # Make the output duration a property of the encoded MP4 rather than
        # relying on validator arithmetic or encoder frame rounding.
        filters.append(f"trim=duration={output_duration:.6f}")
        filters.append("setpts=PTS-STARTPTS")
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
                f"{_safe_ffmpeg_stderr_tail(completed.stderr)}"
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


def _validate_approved_materialization(selection: object, acquisition: object) -> None:
    """Fail before staging/render if materialization drifted from frozen UIDs."""
    expected = {
        str(getattr(decision.candidate, "canonical_id", "") or "")
        for decision in (getattr(selection, "decisions", ()) or ())
    }
    expected.discard("")
    materialized = {
        str((getattr(info, "source_info", {}) or {}).get("asset_id") or "")
        for info in (getattr(acquisition, "materials", ()) or ())
    }
    materialized.discard("")
    if materialized != expected:
        unexpected = sorted(materialized - expected)
        missing = sorted(expected - materialized)
        details = []
        if unexpected:
            details.append("unapproved asset_uids=" + ", ".join(unexpected))
        if missing:
            details.append("missing approved asset_uids=" + ", ".join(missing))
        raise RuntimeError(
            "approved renderer manifest integrity failed: " + "; ".join(details)
        )


def run_master(manifest: dict) -> dict:
    from app.custom import human_review
    from app.custom.material_acquisition import acquire_selected_materials
    from app.models.schema import VideoParams
    from app.services import task

    production_plan_path = str(manifest.get("production_plan_path") or "")
    from app.custom.mpt_defaults import mpt_video_params, resolve_effective_mpt_settings
    mpt_settings = resolve_effective_mpt_settings(manifest.get("effective_mpt_settings"))
    custom_audio_file = _task_local_custom_audio(manifest)
    params = VideoParams(
        video_subject=manifest["stem"],
        video_script=manifest["script"],
        **mpt_video_params(mpt_settings),
        video_concat_mode="sequential",
        match_materials_to_script=True,
        video_count=1,
        video_source="pexels",
        material_source_policy=_material_source_policy(manifest),
        editorial_profile=manifest.get("editorial_profile") or {},
        custom_audio_file=custom_audio_file,
        voice_name="",
        voice_volume=1.0,
        voice_rate=1.0,
        subtitle_enabled=False,
        subtitle_correction_enabled=False,
        subtitle_optimization_enabled=False,
        video_terms=manifest.get("video_terms") or None,
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
            approved_plan=plan,
        )
        _validate_approved_materialization(selection, acquisition)

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

    from app.custom.mpt_defaults import mpt_video_params, resolve_effective_mpt_settings
    mpt_settings = resolve_effective_mpt_settings(manifest.get("effective_mpt_settings"))
    custom_audio_file = _task_local_custom_audio(manifest)
    params = VideoParams(
        video_subject=manifest["stem"],
        video_script=manifest["script"],
        **mpt_video_params(mpt_settings),
        video_concat_mode="sequential",
        match_materials_to_script=True,
        video_count=1,
        video_source="pexels",
        material_source_policy=_material_source_policy(manifest),
        editorial_profile=manifest.get("editorial_profile") or {},
        custom_audio_file=custom_audio_file,
        voice_name="",
        voice_volume=1.0,
        voice_rate=1.0,
        subtitle_enabled=False,
        subtitle_correction_enabled=False,
        subtitle_optimization_enabled=False,
        # Blank/missing deliberately remains None so native generate_terms()
        # follows its established automatic branch.
        video_terms=manifest.get("video_terms") or None,
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
            "material_title": manifest.get("material_title") or "",
            "content_title": manifest["stem"],
            "source_policy": manifest.get("source_policy") or "",
            "mpt_defaults": manifest.get("mpt_defaults"),
            "effective_mpt_settings": mpt_settings,
            "video_terms_source": "operator" if manifest.get("video_terms") else "generated",
            "video_terms_raw": manifest.get("video_terms") if manifest.get("video_terms") else None,
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
    # The approved batch contract aligns directly against its canonical MP3.
    # Do not require a master-derived WAV; faster-whisper reads MP3 via PyAV.
    raw_audio = str(manifest["audio_file"])
    audio_path = Path(raw_audio)
    if not audio_path.is_file() or audio_path.stat().st_size <= 0:
        raise RuntimeError(f"canonical MP3 audio missing for subtitles: {raw_audio}")
    try:
        subtitle.create(raw_audio, subtitle_file.as_posix())
    except Exception as exc:
        raise RuntimeError(f"Whisper could not decode canonical MP3 {raw_audio}: {exc}") from exc
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
