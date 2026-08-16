#!/usr/bin/env python3
"""One-command host runner for MoneyPrinterTurbo batch production."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any


HOST_ROOT = Path(__file__).resolve().parents[1]
if str(HOST_ROOT) not in sys.path:
    sys.path.insert(0, str(HOST_ROOT))

from app.custom import human_review

CONTAINER_ROOT = Path("/MoneyPrinterTurbo")
HYPERFRAMES_ROOT = Path("/opt/apps/hyperframes")
REPORT_NAME = "batch-report.json"
APPROVAL_CONFIDENCE = 0.90
PROCESS_TIMEOUT = 90 * 60
VISUAL_STYLE_NONE = "none"
VISUAL_STYLE_WARM_SEPIA = "warm-sepia"
VISUAL_STYLE_CHOICES = (VISUAL_STYLE_NONE, VISUAL_STYLE_WARM_SEPIA)
VISUAL_STYLE_VERSION = {
    VISUAL_STYLE_NONE: 1,
    VISUAL_STYLE_WARM_SEPIA: 2,
}
WARM_SEPIA_FILTERGRAPH = (
    "eq=saturation=0.74:contrast=1.08:brightness=-0.006,"
    "colorbalance=rs=0.020:gs=0.002:bs=-0.018:rm=0.065:gm=0.014:bm=-0.075:rh=0.032:gh=0.006:bh=-0.035,"
    "colorchannelmixer=rr=1.045:rg=0.025:rb=0.000:gr=0.012:gg=0.990:gb=0.000:br=-0.015:bg=0.018:bb=0.860"
)
VIDEO_DURATION_TOLERANCE_SECONDS = 0.35


class BatchValidationError(Exception):
    pass


class StageError(Exception):
    pass


class Job:
    def __init__(self, stem: str, mp3: Path, txt: Path, srt: Path | None, batch_id: str):
        self.stem = stem
        self.mp3 = mp3
        self.txt = txt
        self.srt = srt
        self.task_id = f"batch-{batch_id}-{sanitize_id(stem)}"


_current_process: subprocess.Popen | None = None


def sanitize_id(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", str(value or "").strip())
    safe = safe.strip("-_")
    return safe or "job"


def sanitize_batch_id(path: Path) -> str:
    return sanitize_id(path.name)


def host_to_container(path: Path) -> str:
    resolved = path.resolve()
    try:
        rel = resolved.relative_to(HOST_ROOT.resolve())
    except ValueError:
        return resolved.as_posix()
    return (CONTAINER_ROOT / rel).as_posix()


def scan_input(input_dir: Path) -> list[Job]:
    if not input_dir.is_dir():
        raise BatchValidationError(f"{input_dir} no es una carpeta")

    files = [p for p in input_dir.iterdir() if p.is_file() and not p.name.startswith(".")]
    allowed = {".mp3", ".txt", ".srt"}
    ignored = [p.name for p in files if p.suffix.lower() not in allowed]
    if ignored:
        raise BatchValidationError("archivos no permitidos: " + ", ".join(sorted(ignored)))

    mp3s = {p.stem: p for p in files if p.suffix.lower() == ".mp3"}
    txts = {p.stem: p for p in files if p.suffix.lower() == ".txt"}
    srts = {p.stem: p for p in files if p.suffix.lower() == ".srt"}

    missing = [mp3 for stem, mp3 in sorted(mp3s.items()) if stem not in txts]
    if missing:
        lines = ["ERROR:"]
        lines.extend(f"{mp3.name} no tiene {mp3.stem}.txt" for mp3 in missing)
        raise BatchValidationError("\n".join(lines))

    batch_id = sanitize_batch_id(input_dir)
    return [
        Job(stem, mp3s[stem], txts[stem], srts.get(stem), batch_id)
        for stem in sorted(mp3s)
    ]


def read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {}
    if isinstance(data, dict):
        return data
    return {}


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{time.time_ns()}")
    try:
        with tmp.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def valid_file(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


def ffprobe_media(path: Path) -> dict[str, Any]:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            "-show_format",
            path.as_posix(),
        ],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    data = json.loads(result.stdout or "{}")
    streams = data.get("streams") if isinstance(data.get("streams"), list) else []
    video_stream = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    audio_stream = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    duration = None
    format_data = data.get("format") if isinstance(data.get("format"), dict) else {}
    for value in (format_data.get("duration"), video_stream.get("duration") if video_stream else None):
        try:
            duration = float(value)
            break
        except (TypeError, ValueError):
            pass
    return {
        "has_video": video_stream is not None,
        "has_audio": audio_stream is not None,
        "width": int(video_stream.get("width") or 0) if video_stream else 0,
        "height": int(video_stream.get("height") or 0) if video_stream else 0,
        "duration": duration,
    }


def validate_styled_master(master: Path, styled_master: Path) -> None:
    if not valid_file(styled_master):
        raise StageError(f"visual style did not produce {styled_master}")

    try:
        master_info = ffprobe_media(master)
        styled_info = ffprobe_media(styled_master)
    except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        raise StageError(f"ffprobe validation failed for visual style output: {exc}") from exc

    if not styled_info["has_video"]:
        raise StageError(f"visual style output has no video: {styled_master}")
    if (styled_info["width"], styled_info["height"]) != (master_info["width"], master_info["height"]):
        raise StageError(
            "visual style changed resolution: "
            f"{master_info['width']}x{master_info['height']} -> {styled_info['width']}x{styled_info['height']}"
        )
    if master_info["duration"] is not None and styled_info["duration"] is not None:
        delta = abs(master_info["duration"] - styled_info["duration"])
        if delta > VIDEO_DURATION_TOLERANCE_SECONDS:
            raise StageError(f"visual style changed duration by {delta:.3f}s")
    if master_info["has_audio"] and not styled_info["has_audio"]:
        raise StageError("visual style output is missing audio")


def approved_report(report: dict[str, Any]) -> bool:
    return (
        report.get("status") in {"ok", "custom_srt"}
        and float(report.get("confidence") or 0) >= APPROVAL_CONFIDENCE
        and report.get("review_required") is False
    )


def visual_style_version(visual_style: str) -> int:
    try:
        return VISUAL_STYLE_VERSION[visual_style]
    except KeyError as exc:
        raise StageError(f"unsupported visual style: {visual_style}") from exc


def job_report_entry(report: dict[str, Any], job: Job) -> dict[str, Any]:
    jobs = report.get("jobs")
    if isinstance(jobs, dict):
        entry = jobs.get(job.stem)
        if isinstance(entry, dict):
            return entry
    return {}


def styled_master_is_current(report_entry: dict[str, Any], styled_master: Path, visual_style: str) -> bool:
    return (
        valid_file(styled_master)
        and report_entry.get("visual_style") == visual_style
        and report_entry.get("visual_style_version") == visual_style_version(visual_style)
        and report_entry.get("styled_master") == styled_master.as_posix()
    )


def delivery_output_is_current(report_entry: dict[str, Any], video_for_delivery: Path, visual_style: str) -> bool:
    return (
        report_entry.get("video_for_delivery") == video_for_delivery.as_posix()
        and report_entry.get("visual_style") == visual_style
        and report_entry.get("visual_style_version") == visual_style_version(visual_style)
    )


def link_or_copy(src: Path, dst: Path, *, replace: bool = False) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        if valid_file(dst) and not replace:
            return
        dst.unlink()
    try:
        os.link(src, dst)
        return
    except OSError:
        pass
    try:
        subprocess.run(["cp", "--reflink=auto", src.as_posix(), dst.as_posix()], check=True)
        return
    except Exception:
        shutil.copy2(src, dst)


def compose_base_command() -> list[str]:
    cmd = ["docker", "compose", "-f", "docker-compose.yml"]
    smoke = Path("/root/mpt-asset-hub-smoke.compose.yml")
    if smoke.exists():
        cmd.extend(["-f", smoke.as_posix()])
    return cmd


def run_logged(cmd: list[str], log_path: Path, *, cwd: Path = HOST_ROOT, timeout: int | None = None) -> None:
    global _current_process
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log:
        log.write(("\n$ " + " ".join(cmd) + "\n").encode("utf-8"))
        process = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        _current_process = process
        try:
            code = process.wait(timeout=timeout)
        except KeyboardInterrupt:
            terminate_current_process()
            raise
        except subprocess.TimeoutExpired as exc:
            terminate_current_process()
            raise StageError(f"command timed out: {' '.join(cmd)}") from exc
        finally:
            _current_process = None
        if code != 0:
            raise StageError(f"command failed with exit code {code}: {' '.join(cmd)}")


def terminate_current_process() -> None:
    process = _current_process
    if process and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def make_manifest(
    job: Job,
    task_dir: Path,
    script: str,
    *,
    production_plan_path: Path | None = None,
    visual_style: str = VISUAL_STYLE_NONE,
) -> dict[str, Any]:
    payload = {
        "batch_id": sanitize_batch_id(job.mp3.parent),
        "stem": job.stem,
        "task_id": job.task_id,
        "task_dir": host_to_container(task_dir),
        "audio_file": host_to_container(job.mp3),
        "host_audio_file": job.mp3.as_posix(),
        "text_file": host_to_container(job.txt),
        "host_text_file": job.txt.as_posix(),
        "subtitle_audio_file": host_to_container(task_dir / "subtitle-audio.wav"),
        "script": script,
        "visual_style": visual_style,
        "material_title": os.environ.get("MPT_MATERIAL_TITLE", "").strip(),
    }
    if production_plan_path is not None:
        payload["production_plan_path"] = host_to_container(production_plan_path)
    return payload


def write_manifest(
    job: Job,
    task_dir: Path,
    script: str,
    *,
    production_plan_path: Path | None = None,
    visual_style: str = VISUAL_STYLE_NONE,
) -> Path:
    manifest_path = task_dir / "batch-manifest.json"
    write_json_atomic(
        manifest_path,
        make_manifest(
            job,
            task_dir,
            script,
            production_plan_path=production_plan_path,
            visual_style=visual_style,
        ),
    )
    return manifest_path


def run_worker(manifest: Path, stage: str, log_path: Path) -> None:
    cmd = compose_base_command() + [
        "exec",
        "-T",
        "api",
        "python3",
        "scripts/batch_mpt_worker.py",
        host_to_container(manifest),
        "--stage",
        stage,
    ]
    run_logged(cmd, log_path, timeout=PROCESS_TIMEOUT)


def extract_subtitle_audio(master: Path, wav: Path, log_path: Path) -> None:
    if valid_file(wav):
        return
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        master.as_posix(),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-acodec",
        "pcm_s16le",
        wav.as_posix(),
    ]
    run_logged(cmd, log_path, timeout=PROCESS_TIMEOUT)
    if not valid_file(wav):
        raise StageError(f"ffmpeg did not produce {wav}")


def build_warm_sepia_command(master: Path, styled_master: Path, *, copy_audio: bool) -> list[str]:
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        master.as_posix(),
        "-filter:v",
        WARM_SEPIA_FILTERGRAPH,
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
    ]
    if copy_audio:
        cmd.extend(["-c:a", "copy"])
    else:
        cmd.extend(["-c:a", "aac", "-b:a", "192k"])
    cmd.extend(["-movflags", "+faststart", styled_master.as_posix()])
    return cmd


def apply_visual_style(
    master: Path,
    styled_master: Path,
    visual_style: str,
    log_path: Path,
    report_entry: dict[str, Any] | None = None,
) -> tuple[Path, str]:
    if visual_style == VISUAL_STYLE_NONE:
        return master, "skip"
    if visual_style != VISUAL_STYLE_WARM_SEPIA:
        raise StageError(f"unsupported visual style: {visual_style}")
    report_entry = report_entry or {}
    if styled_master_is_current(report_entry, styled_master, visual_style):
        validate_styled_master(master, styled_master)
        return styled_master, "skip"

    styled_master.parent.mkdir(parents=True, exist_ok=True)
    try:
        run_logged(build_warm_sepia_command(master, styled_master, copy_audio=True), log_path, timeout=PROCESS_TIMEOUT)
    except StageError:
        try:
            styled_master.unlink()
        except FileNotFoundError:
            pass
        run_logged(build_warm_sepia_command(master, styled_master, copy_audio=False), log_path, timeout=PROCESS_TIMEOUT)
    validate_styled_master(master, styled_master)
    return styled_master, "ok"


def write_custom_srt_report(task_dir: Path) -> dict[str, Any]:
    report = {"status": "custom_srt", "confidence": 1.0, "review_required": False}
    write_json_atomic(task_dir / "subtitle-alignment.json", report)
    return report


def subtitle_report(task_dir: Path) -> dict[str, Any]:
    return read_json(task_dir / "subtitle-alignment.json")


def find_hyperframes_container() -> str:
    result = subprocess.run(
        [
            "docker",
            "ps",
            "--filter",
            "name=hyperframes_hyperframes",
            "--format",
            "{{.Names}}",
        ],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    names = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not names:
        raise StageError("HyperFrames container not found")
    return names[0]


def run_hyperframes(job: Job, master: Path, srt: Path, final_subtitled: Path, log_path: Path, preset: str, position: str) -> Path:
    hf_job = job.task_id
    hf_input = HYPERFRAMES_ROOT / "input" / hf_job
    hf_video = hf_input / "master.mp4"
    hf_srt = hf_input / "subtitle.srt"
    link_or_copy(master, hf_video, replace=True)
    link_or_copy(srt, hf_srt, replace=True)

    container = find_hyperframes_container()
    run_logged(
        [
            "docker",
            "exec",
            container,
            "node",
            "scripts/build-and-render.mjs",
            hf_job,
            "--video",
            hf_video.as_posix(),
            "--srt",
            hf_srt.as_posix(),
            "--preset",
            preset,
            "--position",
            position,
            "--build-only",
        ],
        log_path,
        cwd=HYPERFRAMES_ROOT,
        timeout=PROCESS_TIMEOUT,
    )
    run_logged(
        ["docker", "exec", container, "node", "scripts/render-job.mjs", hf_job],
        log_path,
        cwd=HYPERFRAMES_ROOT,
        timeout=PROCESS_TIMEOUT,
    )
    hf_output = HYPERFRAMES_ROOT / "output" / f"{hf_job}.mp4"
    if not valid_file(hf_output):
        raise StageError(f"HyperFrames did not produce {hf_output}")
    link_or_copy(hf_output, final_subtitled, replace=True)
    return hf_output


def init_report(batch_id: str, jobs: list[Job], report_path: Path) -> dict[str, Any]:
    report = read_json(report_path)
    if report.get("batch_id") != batch_id or not isinstance(report.get("jobs"), dict):
        report = {"batch_id": batch_id, "jobs": {}}
    for job in jobs:
        report["jobs"].setdefault(job.stem, {"task_id": job.task_id, "status": "pending"})
        report["jobs"][job.stem]["task_id"] = job.task_id
    return report


def update_job(report: dict[str, Any], report_path: Path, job: Job, **fields: Any) -> None:
    entry = report.setdefault("jobs", {}).setdefault(job.stem, {"task_id": job.task_id})
    entry.update(fields)
    entry["task_id"] = job.task_id
    write_json_atomic(report_path, report)


def process_job(
    job: Job,
    *,
    index: int,
    total: int,
    batch_output_dir: Path,
    report: dict[str, Any],
    report_path: Path,
    preset: str,
    position: str,
    visual_style: str = VISUAL_STYLE_NONE,
    human_review_mode: bool = False,
) -> str:
    task_dir = HOST_ROOT / "storage" / "tasks" / job.task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    logs_dir = batch_output_dir / "logs"
    master = task_dir / "final-1.mp4"
    styled_master = task_dir / "final-styled-warm-sepia.mp4"
    subtitle = task_dir / "subtitle.srt"
    alignment = task_dir / "subtitle-alignment.json"
    final_subtitled = task_dir / "final-subtitled.mp4"
    batch_final = batch_output_dir / f"{job.stem}.mp4"
    script = job.txt.read_text(encoding="utf-8")
    batch_id = sanitize_batch_id(job.mp3.parent)
    review_plan_path = human_review.plan_path(batch_id, job.stem, HOST_ROOT)
    existing_review_plan = read_json(review_plan_path)
    approved_review_plan = existing_review_plan.get("review_status") == human_review.STATUS_APPROVED
    manifest = write_manifest(
        job,
        task_dir,
        script,
        production_plan_path=review_plan_path if (human_review_mode or approved_review_plan) else None,
        visual_style=visual_style,
    )
    existing_report_entry = dict(job_report_entry(report, job))
    current_visual_style_version = visual_style_version(visual_style)

    print(f"[{index}/{total}] {job.stem}")
    if human_review_mode:
        existing_plan = existing_review_plan
        if existing_plan.get("review_status") == human_review.STATUS_APPROVED:
            print("  REVIEW PLAN SKIP approved")
            update_job(
                report,
                report_path,
                job,
                status=human_review.STATUS_APPROVED,
                production_plan_path=review_plan_path.as_posix(),
            )
            return human_review.STATUS_APPROVED
        if existing_plan.get("review_status") == human_review.STATUS_PENDING:
            print("  PREPARE     SKIP")
            print("  REVIEW PLAN SKIP")
        else:
            update_job(report, report_path, job, status="prepare_review")
            run_worker(manifest, "review", logs_dir / f"{job.stem}-review.log")
            print("  PREPARE     OK")
            print("  REVIEW PLAN OK")
        print("  STATUS      PENDING_REVIEW")
        update_job(
            report,
            report_path,
            job,
            status=human_review.STATUS_PENDING,
            production_plan_path=review_plan_path.as_posix(),
        )
        return human_review.STATUS_PENDING

    update_job(report, report_path, job, status="master")
    if valid_file(master):
        print("  MASTER      SKIP")
    else:
        run_worker(manifest, "master", logs_dir / f"{job.stem}-mpt.log")
        print("  MASTER      OK")

    update_job(
        report,
        report_path,
        job,
        status="visual_style",
        master=master.as_posix(),
        visual_style=visual_style,
        visual_style_version=current_visual_style_version,
        styled_master=styled_master.as_posix() if visual_style == VISUAL_STYLE_WARM_SEPIA else None,
    )
    video_for_delivery, visual_style_status = apply_visual_style(
        master,
        styled_master,
        visual_style,
        logs_dir / f"{job.stem}-visual-style.log",
        existing_report_entry,
    )
    if visual_style == VISUAL_STYLE_NONE:
        print("  VISUAL STYLE SKIP none")
    elif visual_style_status == "skip":
        print(f"  VISUAL STYLE SKIP {visual_style}")
    else:
        print(f"  VISUAL STYLE OK {visual_style}")

    update_job(
        report,
        report_path,
        job,
        status="subtitles",
        master=master.as_posix(),
        video_for_delivery=video_for_delivery.as_posix(),
        visual_style=visual_style,
        visual_style_version=current_visual_style_version,
        styled_master=styled_master.as_posix() if visual_style == VISUAL_STYLE_WARM_SEPIA else None,
    )

    if valid_file(subtitle) and approved_report(subtitle_report(task_dir)):
        report_data = subtitle_report(task_dir)
        print(f"  SUBTITLES   SKIP confidence={float(report_data.get('confidence', 0)):.3f}")
    elif job.srt:
        shutil.copy2(job.srt, subtitle)
        report_data = write_custom_srt_report(task_dir)
        print("  SUBTITLES   OK confidence=1.000 custom_srt")
    else:
        extract_subtitle_audio(video_for_delivery, task_dir / "subtitle-audio.wav", logs_dir / f"{job.stem}-whisper.log")
        run_worker(manifest, "subtitles", logs_dir / f"{job.stem}-whisper.log")
        report_data = subtitle_report(task_dir)
        confidence = float(report_data.get("confidence") or 0)
        if approved_report(report_data):
            print(f"  SUBTITLES   OK confidence={confidence:.3f}")
        else:
            print(f"  SUBTITLES   REVIEW confidence={confidence:.3f}")
            update_job(
                report,
                report_path,
                job,
                status="review_required",
                subtitle_status=report_data.get("status"),
                confidence=confidence,
            )
            print("  HYPERFRAMES SKIP")
            return "review_required"

    update_job(
        report,
        report_path,
        job,
        status="hyperframes",
        subtitle_status=report_data.get("status"),
        confidence=float(report_data.get("confidence") or 0),
        video_for_delivery=video_for_delivery.as_posix(),
        visual_style=visual_style,
        visual_style_version=current_visual_style_version,
        styled_master=styled_master.as_posix() if visual_style == VISUAL_STYLE_WARM_SEPIA else None,
    )

    final_subtitled_current = valid_file(final_subtitled) and delivery_output_is_current(
        existing_report_entry,
        video_for_delivery,
        visual_style,
    )
    if final_subtitled_current:
        print("  HYPERFRAMES SKIP")
    else:
        run_hyperframes(
            job,
            video_for_delivery,
            subtitle,
            final_subtitled,
            logs_dir / f"{job.stem}-hyperframes.log",
            preset,
            position,
        )
        print("  HYPERFRAMES OK")

    link_or_copy(final_subtitled, batch_final, replace=not final_subtitled_current)
    print(f"  FINAL       {batch_final.relative_to(HOST_ROOT).as_posix()}")
    update_job(
        report,
        report_path,
        job,
        status="completed",
        final=final_subtitled.as_posix(),
        batch_final=batch_final.as_posix(),
        video_for_delivery=video_for_delivery.as_posix(),
        visual_style=visual_style,
        visual_style_version=current_visual_style_version,
        styled_master=styled_master.as_posix() if visual_style == VISUAL_STYLE_WARM_SEPIA else None,
    )
    return "completed"


def dry_run(input_dir: Path, jobs: list[Job], visual_style: str = VISUAL_STYLE_NONE) -> int:
    print(f"Batch: {sanitize_batch_id(input_dir)}")
    print(f"Jobs: {len(jobs)}")
    print(f"visual_style={visual_style}")
    for job in jobs:
        print(f"- {job.stem}")
        print(f"  task_id: {job.task_id}")
        print(f"  mp3: {job.mp3.name}")
        print(f"  txt: {job.txt.name}")
        print(f"  custom_srt: {'yes' if job.srt else 'no'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Produce an MPT batch from MP3/TXT pairs")
    parser.add_argument("input_dir")
    parser.add_argument("--preset", default="editorial-gold")
    parser.add_argument("--position", default="bottom")
    parser.add_argument("--visual-style", choices=VISUAL_STYLE_CHOICES, default=VISUAL_STYLE_NONE)
    parser.add_argument("--human-review", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    input_dir = Path(args.input_dir).resolve()
    try:
        jobs = scan_input(input_dir)
    except BatchValidationError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.dry_run:
        return dry_run(input_dir, jobs, args.visual_style)

    batch_id = sanitize_batch_id(input_dir)
    batch_output_dir = HOST_ROOT / "storage" / "batch_outputs" / batch_id
    batch_output_dir.mkdir(parents=True, exist_ok=True)
    report_path = batch_output_dir / REPORT_NAME
    report = init_report(batch_id, jobs, report_path)
    write_json_atomic(report_path, report)

    print(f"Batch: {batch_id}")
    print(f"Jobs: {len(jobs)}")
    counts = {"completed": 0, "review_required": 0, human_review.STATUS_PENDING: 0, human_review.STATUS_APPROVED: 0, "failed": 0}
    interrupted = False

    try:
        for index, job in enumerate(jobs, start=1):
            try:
                status = process_job(
                    job,
                    index=index,
                    total=len(jobs),
                    batch_output_dir=batch_output_dir,
                    report=report,
                    report_path=report_path,
                    preset=args.preset,
                    position=args.position,
                    visual_style=args.visual_style,
                    human_review_mode=args.human_review,
                )
                counts[status] = counts.get(status, 0) + 1
            except Exception as exc:
                counts["failed"] += 1
                print(f"  FAILED      {type(exc).__name__}: {exc}")
                update_job(
                    report,
                    report_path,
                    job,
                    status="failed",
                    error=f"{type(exc).__name__}: {exc}",
                )
    except KeyboardInterrupt:
        interrupted = True
        terminate_current_process()
        write_json_atomic(report_path, report)

    if interrupted:
        print("Batch interrumpido; vuelva a ejecutar el mismo comando para continuar.")
        return 3

    print("SUMMARY")
    print(f"completed: {counts['completed']}")
    print(f"review_required: {counts['review_required']}")
    if args.human_review:
        print(f"pending_review: {counts[human_review.STATUS_PENDING]}")
        print(f"approved: {counts[human_review.STATUS_APPROVED]}")
    print(f"failed: {counts['failed']}")
    if counts["failed"]:
        return 3
    if counts["review_required"]:
        return 2
    return 0


def process_approved_review_plan(
    production_plan_path: str | Path,
    *,
    preset: str = "karaoke",
    position: str = "bottom",
    visual_style: str | None = None,
) -> str:
    plan_file = Path(production_plan_path)
    if plan_file.is_absolute():
        try:
            relative_plan = plan_file.relative_to("/MoneyPrinterTurbo")
        except ValueError:
            pass
        else:
            plan_file = HOST_ROOT / relative_plan

    plan = human_review.read_json(plan_file)
    if plan.get("review_status") != human_review.STATUS_APPROVED:
        raise StageError(f"production plan is not approved: {plan_file}")

    audio = Path(str(plan.get("audio_path") or ""))
    script_path = Path(str(plan.get("script_path") or ""))
    if not valid_file(audio):
        raise StageError(f"approved audio missing: {audio}")
    if not script_path.is_file():
        raise StageError(f"approved script missing: {script_path}")

    batch_id = str(plan.get("batch_id") or sanitize_batch_id(script_path.parent))
    stem = str(plan.get("stem") or script_path.stem)
    job = Job(stem, audio, script_path, script_path.with_suffix(".srt") if script_path.with_suffix(".srt").is_file() else None, batch_id)
    job.task_id = str(plan.get("task_id") or job.task_id)
    batch_output_dir = HOST_ROOT / "storage" / "batch_outputs" / batch_id
    batch_output_dir.mkdir(parents=True, exist_ok=True)
    report_path = batch_output_dir / REPORT_NAME
    report = init_report(batch_id, [job], report_path)
    write_json_atomic(report_path, report)
    return process_job(
        job,
        index=1,
        total=1,
        batch_output_dir=batch_output_dir,
        report=report,
        report_path=report_path,
        preset=preset,
        position=position,
        visual_style=visual_style or str(plan.get("visual_style") or VISUAL_STYLE_NONE),
        human_review_mode=False,
    )


if __name__ == "__main__":
    sys.exit(main())
