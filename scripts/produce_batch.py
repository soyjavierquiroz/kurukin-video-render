#!/usr/bin/env python3
"""One-command host runner for MoneyPrinterTurbo batch production."""

from __future__ import annotations

import argparse
import hashlib
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
from scripts.production_registry import ProductionRegistry, identity as production_identity, sha256_file

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
CANONICAL_ALIGNMENT_SOURCE = "MP3"
SUBTITLE_WAV_REQUIRED = False
REGISTRY_NAME = "production_registry.sqlite3"
SUBTITLE_RECIPE_VERSION = "semantic-cues-v5"
HYPERFRAMES_RECIPE_VERSION = "hyperframes-editorial-gold-v2"
PRODUCTION_RECIPE_VERSION = f"production-v4:{SUBTITLE_RECIPE_VERSION}:{HYPERFRAMES_RECIPE_VERSION}"
SUBTITLE_STAGE_NAME = "subtitle-stage.json"
HYPERFRAMES_STAGE_NAME = "hyperframes-stage.json"
MASTER_STAGE_NAME = "master-stage.json"
MASTER_RECIPE_VERSION = "approved-timeline-v1"
STYLED_MASTER_STAGE_NAME = "styled-master-stage.json"


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
        self.batch_id = batch_id
        self.task_id = f"batch-{batch_id}-{sanitize_id(stem)}"


def production_recipe_for(visual_style: str, preset: str, position: str) -> str:
    """Global identity recipe: include every final-rendering policy input."""
    return (
        f"{PRODUCTION_RECIPE_VERSION}:style={visual_style}@{visual_style_version(visual_style)}"
        f":preset={preset}:position={position}"
    )


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


def valid_mp4(path: Path) -> bool:
    """Cheap gate for reusable video artifacts.

    Do not treat a non-empty file as a render result: ffprobe must be able to
    read it, it must contain video, and it must have a positive duration.
    """
    if not valid_file(path):
        return False
    try:
        info = ffprobe_media(path)
    except (subprocess.CalledProcessError, json.JSONDecodeError, OSError, ValueError):
        return False
    return bool(info["has_video"] and info["duration"] and info["duration"] > 0)


def production_registry() -> ProductionRegistry:
    return ProductionRegistry(HOST_ROOT / "storage" / REGISTRY_NAME)


def final_duration(path: Path) -> float:
    """Read duration for registry metadata after the normal MP4 validation."""
    try:
        return float(ffprobe_media(path).get("duration") or 0)
    except (subprocess.CalledProcessError, json.JSONDecodeError, OSError, ValueError):
        # This only accommodates validator-mocked unit tests.  Production's
        # valid_mp4 already requires ffprobe and a positive duration.
        return 0.0


def same_file_content(left: Path, right: Path) -> bool:
    try:
        if os.path.samefile(left, right):
            return True
        return left.stat().st_size == right.stat().st_size and sha256_file(left) == sha256_file(right)
    except OSError:
        return False


def _host_path(value: str) -> Path:
    path = Path(value)
    try:
        return HOST_ROOT / path.relative_to(CONTAINER_ROOT)
    except ValueError:
        return path


def backfill_completed(*, wanted_fingerprint: str | None = None, emit: bool = False) -> None:
    """Safely index outputs only when a completed report and frozen inputs prove identity."""
    registry = production_registry()
    outputs_root = HOST_ROOT / "storage" / "batch_outputs"
    if not outputs_root.exists():
        return
    for final_mp4 in sorted(outputs_root.glob("*/*.mp4")):
        if not valid_mp4(final_mp4):
            if emit:
                print(f"INVALID {final_mp4.relative_to(HOST_ROOT).as_posix()}")
            continue
        report = read_json(final_mp4.parent / REPORT_NAME)
        entries = report.get("jobs") if isinstance(report.get("jobs"), dict) else {}
        entry = next((item for title, item in entries.items()
                      if isinstance(item, dict) and item.get("status") == "completed"
                      and _host_path(str(item.get("batch_final") or final_mp4.parent / f"{title}.mp4")) == final_mp4), None)
        if not entry:
            if emit:
                print(f"SKIPPED UNVERIFIABLE {final_mp4.relative_to(HOST_ROOT).as_posix()}")
            continue
        plan_path = _host_path(str(entry.get("production_plan_path") or ""))
        plan = read_json(plan_path)
        audio = _host_path(str(plan.get("audio_path") or ""))
        script = _host_path(str(plan.get("script_path") or ""))
        title = str(plan.get("stem") or final_mp4.stem)
        material_title = str(plan.get("material_title") or "")
        if not valid_file(audio) or not script.is_file():
            if emit:
                print(f"SKIPPED UNVERIFIABLE {final_mp4.relative_to(HOST_ROOT).as_posix()}")
            continue
        # Legacy reports have no recipe provenance.  They are intentionally not
        # backfilled: a retry must rebuild subtitle/final stages under today's
        # recipe rather than globally freezing an old-quality artifact.
        recipe_version = str(entry.get("production_recipe_version") or "")
        if not recipe_version:
            if emit:
                print(f"SKIPPED STALE RECIPE {final_mp4.relative_to(HOST_ROOT).as_posix()}")
            continue
        record = production_identity(title, audio, script, material_title, recipe_version)
        if wanted_fingerprint and record["production_fingerprint"] != wanted_fingerprint:
            continue
        already = registry.find_valid(record["production_fingerprint"], valid_mp4)
        inserted = False
        if already is None:
            inserted = registry.upsert(record, final_mp4, str(report.get("batch_id") or final_mp4.parent.name), final_duration(final_mp4))
        if emit:
            action = "REGISTERED" if inserted else "ALREADY REGISTERED"
            print(f"{action} {title}" + (f" {final_mp4.relative_to(HOST_ROOT).as_posix()}" if inserted else ""))


def valid_srt(path: Path) -> bool:
    """Return whether *path* contains at least one parseable SRT cue."""
    if not valid_file(path):
        return False
    try:
        from app.custom.subtitle_optimizer import parse_srt

        return bool(parse_srt(path.read_text(encoding="utf-8-sig")))
    except (OSError, UnicodeError):
        return False


def stable_fingerprint(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


_DERIVED_COVERAGE_KEYS = {"coverage", "coverage_summary", "segment_coverage"}


def _canonical_approved_plan(value: Any) -> Any:
    """Remove only non-authoritative coverage cache values before hashing."""
    if isinstance(value, dict):
        return {
            str(key): _canonical_approved_plan(item)
            for key, item in value.items()
            if str(key) not in _DERIVED_COVERAGE_KEYS
        }
    if isinstance(value, list):
        return [_canonical_approved_plan(item) for item in value]
    return value


def _frozen_asset_master_data(asset: Any) -> dict[str, Any]:
    asset = asset if isinstance(asset, dict) else {}
    metadata = asset.get("metadata") if isinstance(asset.get("metadata"), dict) else {}
    return {
        "asset_uid": str(asset.get("asset_uid") or asset.get("canonical_id") or ""),
        "flip_horizontal": human_review.asset_flip_horizontal(asset),
        "source_duration": human_review._asset_source_duration(asset),
        "orientation": str(metadata.get("orientation") or asset.get("orientation") or ""),
    }


def master_stage_fingerprint(plan: dict[str, Any]) -> str:
    """Fingerprint the frozen approved inputs to the MPT video-only render."""
    segments = [segment for segment in plan.get("segments", []) if isinstance(segment, dict)]
    approved_plan = _canonical_approved_plan(plan)
    return stable_fingerprint({
        "approved_plan_sha256": stable_fingerprint(approved_plan),
        "ordered_segment_ids": [str(segment.get("segment_id") or "") for segment in segments],
        "ordered_segment_target_durations": [
            float(segment.get("duration") or 0) for segment in segments
        ],
        "ordered_primary_assets": [_frozen_asset_master_data(segment.get("selected_asset")) for segment in segments],
        "ordered_authorized_backup_assets": [
            [_frozen_asset_master_data(asset) for asset in (segment.get("backup_assets") or [])]
            for segment in segments
        ],
        "visual_orientation": str(plan.get("aspect_ratio") or plan.get("video_aspect") or ""),
        "timeline_policy": {
            "preferred_playback_speed": human_review.PREFERRED_PLAYBACK_SPEED,
            "hard_min_playback_speed": human_review.HARD_MIN_PLAYBACK_SPEED,
            "min_backup_output_seconds": human_review.MIN_BACKUP_OUTPUT_SECONDS,
            "max_segment_freeze_seconds": human_review.MAX_SEGMENT_FREEZE_SECONDS,
            "max_timeline_autofill_seconds": human_review.MAX_TIMELINE_AUTOFILL_SECONDS,
        },
        "master_recipe_version": MASTER_RECIPE_VERSION,
    })


def styled_master_fingerprint(master: Path, visual_style: str) -> str:
    return stable_fingerprint({
        "master_sha256": sha256_file(master),
        "visual_style": visual_style,
        "visual_style_version": visual_style_version(visual_style),
    })


def subtitle_stage_fingerprint(job: Job) -> str:
    return stable_fingerprint({
        "audio_sha256": sha256_file(job.mp3),
        "script_sha256": sha256_file(job.txt),
        "subtitle_recipe_version": SUBTITLE_RECIPE_VERSION,
        "segmentation": {
            "language": "es", "policy": "punctuation-clause-natural-phrase",
            "semantic_rebalance": "connector-v1", "max_display_lines": 2,
        },
    })


def final_stage_fingerprint(master: Path, subtitle: Path, *, preset: str, position: str, visual_style: str) -> str:
    return stable_fingerprint({
        "master_sha256": sha256_file(master),
        "srt_sha256": sha256_file(subtitle),
        "subtitle_recipe_version": SUBTITLE_RECIPE_VERSION,
        "hyperframes_recipe_version": HYPERFRAMES_RECIPE_VERSION,
        "preset": preset, "position": position,
        "visual_style": visual_style, "visual_style_version": visual_style_version(visual_style),
    })


def stage_metadata_is_current(path: Path, fingerprint: str) -> bool:
    return read_json(path).get("fingerprint") == fingerprint


def write_stage_metadata(path: Path, fingerprint: str, **details: Any) -> None:
    write_json_atomic(path, {"fingerprint": fingerprint, **details})


def subtitle_validation_issues(
    subtitle: Path, audio_duration: float | None = None,
) -> tuple[list[str], list[str]]:
    """Return (fatal structural errors, non-fatal semantic style warnings)."""
    from app.custom.subtitle_optimizer import TIMING_RE, parse_srt, parse_timestamp
    from app.services.subtitle import _is_semantic_orphan, _normalize_token_for_subtitle_alignment

    try:
        content = subtitle.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return ["invalid_srt"], []

    blocks = [block for block in re.split(r"\n\s*\n", content.replace("\r\n", "\n").replace("\r", "\n").strip()) if block.strip()]
    if not blocks:
        return ["empty_srt"], []

    structural: list[str] = []
    for index, block in enumerate(blocks, start=1):
        lines = [line.rstrip() for line in block.split("\n")]
        timing_match = TIMING_RE.match(lines[1].strip()) if len(lines) >= 2 else None
        if len(lines) < 2 or not lines[0].strip().isdigit() or not timing_match:
            structural.append(f"cue_{index}_invalid_srt")
            continue
        try:
            start_value, end_value = timing_match.group(1), timing_match.group(2)
            start, end = parse_timestamp(start_value), parse_timestamp(end_value)
        except (AttributeError, ValueError):
            structural.append(f"cue_{index}_timing")
            continue
        if any(
            int(value.split(":")[1]) >= 60
            or int(value.split(":")[2].split(",")[0]) >= 60
            for value in (start_value, end_value)
        ):
            structural.append(f"cue_{index}_timing")
        if end <= start:
            structural.append(f"cue_{index}_timing")
        if len(lines) == 2:
            structural.append(f"cue_{index}_empty")
        if not " ".join(line.strip() for line in lines[2:]).strip():
            structural.append(f"cue_{index}_empty")

    if structural:
        return list(dict.fromkeys(structural)), []

    cues = parse_srt(content)
    if not cues or len(cues) != len(blocks):
        structural.append("invalid_srt")
    semantic: list[str] = []
    previous_end = -1.0
    previous_text = ""
    for index, cue in enumerate(cues, start=1):
        try:
            start, end = float(cue["start"]), float(cue["end"])
        except (KeyError, TypeError, ValueError):
            structural.append(f"cue_{index}_timing")
            continue
        text = " ".join(str(cue.get("text") or "").split())
        words = text.split()
        if not text:
            structural.append(f"cue_{index}_empty")
        if end <= start or start < previous_end:
            structural.append(f"cue_{index}_ordering")
        if audio_duration is not None and (start < 0 or end > audio_duration + 0.05):
            structural.append(f"cue_{index}_outside_audio")
        if previous_text and text.casefold() == previous_text.casefold():
            semantic.append(f"cue_{index}_duplicate")
        if words and index < len(cues):
            last = _normalize_token_for_subtitle_alignment(words[-1])
            # Punctuation closes the phrase even when normalization makes an
            # interrogative "qué?" look like the connector "que".
            if not re.search(r"[.!?！？。:]$", words[-1]) and (_is_semantic_orphan(words[-1]) or last == "no"):
                semantic.append(f"cue_{index}_dangling_{last}")
        if len(words) == 1 and not re.search(r"[.!?！？。:]$", text):
            semantic.append(f"cue_{index}_orphan")
        if text.endswith(("-", "…")):
            semantic.append(f"cue_{index}_truncated")
        previous_end, previous_text = end, text
    return list(dict.fromkeys(structural)), list(dict.fromkeys(semantic))


def subtitle_quality_issues(subtitle: Path, audio_duration: float | None = None) -> list[str]:
    """Compatibility wrapper for callers that need every subtitle issue."""
    structural, semantic = subtitle_validation_issues(subtitle, audio_duration)
    return [*structural, *semantic]


def repair_subtitle_semantics(subtitle: Path, script: str) -> dict[str, Any]:
    """Re-run deterministic canonical segmentation from the existing timing SRT."""
    from app.services import subtitle as subtitle_service
    report = subtitle_service._build_alignment_result(str(subtitle), script)
    if report["status"] == "ok":
        subtitle_service._write_aligned_subtitle(str(subtitle), report["_output_items"])
    subtitle_service._write_alignment_report(str(subtitle), report)
    return {key: value for key, value in report.items() if not key.startswith("_")}


def subtitle_semantic_gate(subtitle: Path, script: str, audio_duration: float | None) -> dict[str, Any]:
    """Attempt one deterministic style repair without weakening structural safety."""
    structural, warnings = subtitle_validation_issues(subtitle, audio_duration)
    result: dict[str, Any] = {
        "structural_issues": structural,
        "initial_warnings": warnings,
        "warnings": warnings,
        "repaired": False,
    }
    if structural or not warnings:
        return result

    result["report"] = repair_subtitle_semantics(subtitle, script)
    structural, warnings = subtitle_validation_issues(subtitle, audio_duration)
    result.update(structural_issues=structural, warnings=warnings, repaired=True)
    return result


def ensure_similar_duration(master: Path, output: Path) -> None:
    """Reject a delivery video whose duration materially differs from master."""
    master_duration = ffprobe_media(master)["duration"]
    output_duration = ffprobe_media(output)["duration"]
    if not master_duration or not output_duration:
        raise StageError(f"duration unavailable for delivery validation: {output}")
    delta = abs(master_duration - output_duration)
    if delta > VIDEO_DURATION_TOLERANCE_SECONDS:
        raise StageError(
            f"delivery duration differs from master by {delta:.3f}s "
            f"(tolerance {VIDEO_DURATION_TOLERANCE_SECONDS:.3f}s)"
        )


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
    except (subprocess.CalledProcessError, json.JSONDecodeError, OSError, ValueError) as exc:
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
    try:
        confidence = float(report.get("confidence") or 0)
    except (TypeError, ValueError):
        return False
    return (
        report.get("status") in {"ok", "custom_srt"}
        and confidence >= APPROVAL_CONFIDENCE
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


def styled_master_is_current(master: Path, styled_master: Path, visual_style: str) -> bool:
    if not (
        valid_file(styled_master)
        and stage_metadata_is_current(
            styled_master.with_name(STYLED_MASTER_STAGE_NAME),
            styled_master_fingerprint(master, visual_style),
        )
    ):
        return False
    try:
        validate_styled_master(master, styled_master)
    except StageError:
        return False
    return True


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


def link_or_copy_completed(src: Path, dst: Path) -> None:
    """Materialize a global completion without altering its source artifact."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def compose_base_command() -> list[str]:
    cmd = ["docker", "compose", "-f", "docker-compose.yml"]
    smoke = Path("/root/mpt-asset-hub-smoke.compose.yml")
    if smoke.exists():
        cmd.extend(["-f", smoke.as_posix()])
    return cmd


def running_inside_mpt_runtime() -> bool:
    """Whether this process already runs in the repository's MPT container."""
    return HOST_ROOT.resolve() == CONTAINER_ROOT.resolve()


def _stage_name_from_command(cmd: list[str]) -> str:
    try:
        return str(cmd[cmd.index("--stage") + 1])
    except (ValueError, IndexError):
        return Path(cmd[0]).name if cmd else "command"


def _safe_log_tail(log_path: Path, *, max_lines: int = 40, max_bytes: int = 8192) -> str:
    """Return useful worker diagnostics without exposing configuration/secrets."""
    try:
        with log_path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            raw = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""

    secret = re.compile(r"(?i)\b(api[_-]?key|token|password|secret)\b\s*([=:])\s*\S+")
    lines = []
    for line in raw.splitlines()[-max_lines:]:
        if "config.toml" in line.lower():
            continue
        lines.append(secret.sub(r"\1\2[REDACTED]", line))
    return "\n".join(lines).strip()


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
            stage = _stage_name_from_command(cmd)
            detail = _safe_log_tail(log_path)
            message = f"{stage} failed exit={code}"
            if detail:
                message += f"\n{detail}"
            raise StageError(message)


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
    subject_gender: str = "neutral",
    material_title: str = "",
    source_policy: str = "",
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
        "script": script,
        "visual_style": visual_style,
        "editorial_profile": {"subject_gender": subject_gender},
        "material_title": material_title.strip(),
        "source_policy": source_policy.strip(),
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
    subject_gender: str = "neutral",
    material_title: str = "",
    source_policy: str = "",
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
            subject_gender=subject_gender,
            material_title=material_title,
            source_policy=source_policy,
        ),
    )
    return manifest_path


def run_worker(manifest: Path, stage: str, log_path: Path) -> None:
    if running_inside_mpt_runtime():
        cmd = [
            sys.executable,
            "scripts/batch_mpt_worker.py",
            host_to_container(manifest),
            "--stage",
            stage,
        ]
        run_logged(cmd, log_path, timeout=PROCESS_TIMEOUT)
        return

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
) -> tuple[Path, str]:
    if visual_style == VISUAL_STYLE_NONE:
        return master, "skip"
    if visual_style != VISUAL_STYLE_WARM_SEPIA:
        raise StageError(f"unsupported visual style: {visual_style}")
    if styled_master_is_current(master, styled_master, visual_style):
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
    write_stage_metadata(
        styled_master.with_name(STYLED_MASTER_STAGE_NAME),
        styled_master_fingerprint(master, visual_style),
        master=master.as_posix(), visual_style=visual_style,
        visual_style_version=visual_style_version(visual_style),
    )
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
    subject_gender: str = "neutral",
    material_title: str = "",
    source_policy: str = "",
) -> str:
    task_dir = HOST_ROOT / "storage" / "tasks" / job.task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    logs_dir = batch_output_dir / "logs"
    master = task_dir / "final-1.mp4"
    master_stage = task_dir / MASTER_STAGE_NAME
    styled_master = task_dir / "final-styled-warm-sepia.mp4"
    subtitle = task_dir / "subtitle.srt"
    alignment = task_dir / "subtitle-alignment.json"
    subtitle_stage = task_dir / SUBTITLE_STAGE_NAME
    hyperframes_stage = task_dir / HYPERFRAMES_STAGE_NAME
    final_subtitled = task_dir / "final-subtitled.mp4"
    batch_final = batch_output_dir / f"{job.stem}.mp4"
    script = job.txt.read_text(encoding="utf-8")
    batch_id = job.batch_id
    review_plan_path = human_review.plan_path(batch_id, job.stem, HOST_ROOT)
    existing_review_plan = read_json(review_plan_path)
    approved_review_plan = existing_review_plan.get("review_status") == human_review.STATUS_APPROVED
    current_visual_style_version = visual_style_version(visual_style)

    print(f"[{index}/{total}] {job.stem}")
    if human_review_mode:
        manifest = write_manifest(
            job, task_dir, script, production_plan_path=review_plan_path,
            visual_style=visual_style, subject_gender=subject_gender,
            material_title=material_title, source_policy=source_policy,
        )
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

    # This comes after the review gate (which must still prepare/validate its
    # approved plan) but before manifest creation, MPT, subtitles, or HF work.
    current_production_recipe = production_recipe_for(visual_style, preset, position)
    identity_record = production_identity(job.stem, job.mp3, job.txt, material_title, current_production_recipe)
    registry = production_registry()
    existing_completed = registry.find_valid(identity_record["production_fingerprint"], valid_mp4)
    if existing_completed is None:
        # Old dated batches predate the registry.  Their frozen plan must prove
        # the same audio/script fingerprint; filenames alone are never enough.
        backfill_completed(wanted_fingerprint=identity_record["production_fingerprint"])
        existing_completed = registry.find_valid(identity_record["production_fingerprint"], valid_mp4)
    # Same-batch artifacts retain the established stage-by-stage resume path.
    # The registry is for cross-batch reuse, not a replacement for it.
    if existing_completed is not None and str(existing_completed["batch_id"]) != batch_id:
        source = Path(existing_completed["final_mp4_path"])
        if not (valid_mp4(batch_final) and same_file_content(source, batch_final)):
            link_or_copy_completed(source, batch_final)
        print(f"  GLOBAL COMPLETED SKIP {job.stem}")
        print(f"  SOURCE      {source.relative_to(HOST_ROOT).as_posix() if source.is_relative_to(HOST_ROOT) else source}")
        print(f"  FINAL       {batch_final.relative_to(HOST_ROOT).as_posix()}")
        update_job(
            report, report_path, job, status="completed", batch_final=batch_final.as_posix(),
            production_fingerprint=identity_record["production_fingerprint"],
            global_source=source.as_posix(),
        )
        return "completed"
    print(f"  GLOBAL COMPLETED MISS {job.stem}")

    manifest = write_manifest(
        job,
        task_dir,
        script,
        production_plan_path=review_plan_path if approved_review_plan else None,
        visual_style=visual_style,
        subject_gender=subject_gender,
        material_title=material_title,
        source_policy=source_policy,
    )

    current_master_fingerprint = (
        master_stage_fingerprint(existing_review_plan)
        if approved_review_plan else None
    )
    update_job(report, report_path, job, status="master")
    master_current = valid_mp4(master) and (
        current_master_fingerprint is None
        or stage_metadata_is_current(master_stage, current_master_fingerprint)
    )
    if master_current:
        print("  MASTER      SKIP")
    else:
        if valid_mp4(master) and current_master_fingerprint is not None:
            print("  MASTER      STALE")
        print("  MASTER      REBUILD")
        run_worker(manifest, "master", logs_dir / f"{job.stem}-mpt.log")
        if not valid_mp4(master):
            raise StageError(f"master did not produce a valid video: {master}")
        if current_master_fingerprint is not None:
            write_stage_metadata(
                master_stage, current_master_fingerprint,
                master=master.as_posix(), master_recipe_version=MASTER_RECIPE_VERSION,
            )
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
    styled_current_before = (
        visual_style == VISUAL_STYLE_WARM_SEPIA
        and styled_master_is_current(master, styled_master, visual_style)
    )
    video_for_delivery, visual_style_status = apply_visual_style(
        master,
        styled_master,
        visual_style,
        logs_dir / f"{job.stem}-visual-style.log",
    )
    if visual_style == VISUAL_STYLE_NONE:
        print("  VISUAL STYLE SKIP none")
    elif visual_style_status == "skip":
        print(f"  VISUAL STYLE SKIP {visual_style}")
    else:
        if not styled_current_before:
            print("  VISUAL STYLE STALE")
        print("  VISUAL STYLE REBUILD")
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

    current_subtitle_fingerprint = subtitle_stage_fingerprint(job)
    subtitle_current = (
        valid_srt(subtitle)
        and approved_report(subtitle_report(task_dir))
        and stage_metadata_is_current(subtitle_stage, current_subtitle_fingerprint)
    )
    if subtitle_current:
        report_data = subtitle_report(task_dir)
        print(f"  SUBTITLES   SKIP confidence={float(report_data.get('confidence', 0)):.3f}")
    else:
        if valid_srt(subtitle) or valid_file(subtitle_stage):
            print("  SUBTITLES STALE")
        print("  SUBTITLES REBUILD")
    alignment_confidence: float | None = None
    alignment_suffix = ""
    if not subtitle_current and job.srt:
        shutil.copy2(job.srt, subtitle)
        report_data = write_custom_srt_report(task_dir)
        if not valid_srt(subtitle):
            raise StageError(f"custom subtitle is not a valid SRT: {job.srt}")
        alignment_confidence = 1.0
        alignment_suffix = " custom_srt"
    elif not subtitle_current:
        run_worker(manifest, "subtitles", logs_dir / f"{job.stem}-whisper.log")
        report_data = subtitle_report(task_dir)
        confidence = float(report_data.get("confidence") or 0)
        if valid_srt(subtitle) and approved_report(report_data):
            alignment_confidence = confidence
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

    if not subtitle_current:
        try:
            audio_duration = ffprobe_media(job.mp3).get("duration")
        except (subprocess.CalledProcessError, json.JSONDecodeError, OSError, ValueError):
            audio_duration = None
        gate = subtitle_semantic_gate(subtitle, script, audio_duration)
        structural_issues = gate["structural_issues"]
        semantic_warnings = gate["warnings"]
        if structural_issues:
            raise StageError("subtitle structural validation failed: " + ",".join(structural_issues))
        print(f"  SUBTITLE ALIGNMENT OK confidence={alignment_confidence:.3f}{alignment_suffix}")
        if gate["initial_warnings"]:
            print(f"  SUBTITLE SEMANTIC REPAIR {','.join(gate['initial_warnings'])}")
        if gate.get("report"):
            report_data = gate["report"]
        from app.custom.subtitle_optimizer import parse_srt
        cue_count = len(parse_srt(subtitle.read_text(encoding="utf-8-sig")))
        if semantic_warnings:
            for warning in semantic_warnings:
                print(f"  SUBTITLE SEMANTIC WARNING {warning}")
            print(f"  SUBTITLE SEMANTIC ACCEPTED WITH WARNINGS cues={cue_count}")
        else:
            print(f"  SUBTITLE SEMANTIC OK cues={cue_count}")
        print(f"  SUBTITLES   OK confidence={alignment_confidence:.3f}{alignment_suffix}")
        write_stage_metadata(
            subtitle_stage, current_subtitle_fingerprint,
            recipe_version=SUBTITLE_RECIPE_VERSION,
            audio_sha256=sha256_file(job.mp3), script_sha256=sha256_file(job.txt),
            alignment_status=report_data.get("status"),
        )

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

    current_final_fingerprint = final_stage_fingerprint(
        video_for_delivery, subtitle, preset=preset, position=position, visual_style=visual_style,
    )
    final_subtitled_current = valid_mp4(final_subtitled) and stage_metadata_is_current(
        hyperframes_stage, current_final_fingerprint
    )
    if valid_mp4(final_subtitled) and not final_subtitled_current:
        print("  HYPERFRAMES STALE")
    if final_subtitled_current:
        try:
            ensure_similar_duration(video_for_delivery, final_subtitled)
        except (StageError, subprocess.CalledProcessError, json.JSONDecodeError):
            final_subtitled_current = False
    if final_subtitled_current:
        print("  HYPERFRAMES SKIP")
    else:
        print("  HYPERFRAMES REBUILD")
        run_hyperframes(
            job,
            video_for_delivery,
            subtitle,
            final_subtitled,
            logs_dir / f"{job.stem}-hyperframes.log",
            preset,
            position,
        )
        if not valid_mp4(final_subtitled):
            raise StageError(f"HyperFrames did not produce a valid video: {final_subtitled}")
        ensure_similar_duration(video_for_delivery, final_subtitled)
        print("  HYPERFRAMES OK")
        write_stage_metadata(
            hyperframes_stage, current_final_fingerprint,
            recipe_version=HYPERFRAMES_RECIPE_VERSION,
            subtitle_recipe_version=SUBTITLE_RECIPE_VERSION,
            master_sha256=sha256_file(video_for_delivery), srt_sha256=sha256_file(subtitle),
            preset=preset, position=position, visual_style=visual_style,
        )

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
        production_recipe_version=current_production_recipe,
    )
    # Registration is deliberately last: neither failed nor partial stages can
    # create a reusable completed entry.
    if not valid_mp4(batch_final):
        raise StageError(f"final batch output did not validate: {batch_final}")
    registry.upsert(identity_record, batch_final, batch_id, final_duration(batch_final))
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
    parser.add_argument("input_dir", nargs="?")
    parser.add_argument("--approved-plan", help="frozen approved production-plan.json to produce")
    parser.add_argument(
        "--refresh-stale-subtitles-batch",
        metavar="BATCH_ID",
        help="repair stale subtitle/final stages for approved jobs while reusing valid masters",
    )
    parser.add_argument(
        "--production",
        action="store_true",
        help="run the approved-plan production pipeline (master, subtitles, HyperFrames)",
    )
    parser.add_argument("--preset", default="editorial-gold")
    parser.add_argument("--position", default="bottom")
    parser.add_argument("--visual-style", choices=VISUAL_STYLE_CHOICES, default=VISUAL_STYLE_NONE)
    parser.add_argument("--human-review", action="store_true")
    parser.add_argument(
        "--subject-gender",
        choices=("feminine", "masculine", "mixed", "neutral"),
        default="neutral",
    )
    parser.add_argument("--material-title", default=os.environ.get("MPT_MATERIAL_TITLE", "").strip())
    parser.add_argument(
        "--source-policy",
        choices=("", "open", "title-exclusive"),
        default=os.environ.get("MPT_SOURCE_POLICY", "").strip(),
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--reindex-completed",
        action="store_true",
        help="safely register verifiable completed batch outputs; never renders",
    )
    return parser


def refresh_stale_subtitles_batch(batch_id: str, *, preset: str, position: str) -> int:
    """Retry only approved jobs; normal stage provenance preserves valid masters."""
    input_dir = HOST_ROOT / "storage" / "batch_inputs" / batch_id
    jobs = scan_input(input_dir)
    failures = 0
    for job in jobs:
        plan = human_review.plan_path(job.batch_id, job.stem, HOST_ROOT)
        if read_json(plan).get("review_status") != human_review.STATUS_APPROVED:
            continue
        print(f"CURRENT {job.stem}")
        try:
            process_approved_review_plan(plan, preset=preset, position=position)
        except Exception as exc:
            failures += 1
            print(f"FAILED {job.stem} {type(exc).__name__}: {exc}")
    return 3 if failures else 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.refresh_stale_subtitles_batch:
        if args.input_dir or args.approved_plan or args.production or args.human_review or args.dry_run or args.reindex_completed:
            print("--refresh-stale-subtitles-batch cannot be combined with other production options", file=sys.stderr)
            return 1
        try:
            return refresh_stale_subtitles_batch(
                args.refresh_stale_subtitles_batch, preset=args.preset, position=args.position,
            )
        except (BatchValidationError, OSError) as exc:
            print(f"REFRESH FAILED {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
    if args.reindex_completed:
        if args.input_dir or args.approved_plan or args.production or args.human_review or args.dry_run:
            print("--reindex-completed cannot be combined with production options", file=sys.stderr)
            return 1
        backfill_completed(emit=True)
        return 0
    if args.approved_plan or args.production:
        if not args.production or not args.approved_plan or args.input_dir:
            print("--production requires --approved-plan and does not accept input_dir", file=sys.stderr)
            return 1
        try:
            status = process_approved_review_plan(
                args.approved_plan,
                preset=args.preset,
                position=args.position,
            )
        except Exception as exc:
            print(f"PRODUCTION FAILED {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        return 0 if status == "completed" else 2

    if not args.input_dir:
        print("input_dir is required unless --production --approved-plan is used", file=sys.stderr)
        return 1
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
                    subject_gender=args.subject_gender,
                    material_title=args.material_title,
                    source_policy=args.source_policy,
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

    integrity = human_review.validate_approved_plan_integrity(plan)
    if not integrity["ok"]:
        raise StageError(
            "approved production plan integrity failed:\n- "
            + "\n- ".join(integrity["errors"])
        )

    batch_id = str(plan.get("batch_id") or sanitize_batch_id(script_path.parent))
    stem = str(plan.get("stem") or script_path.stem)
    # Approved production plans are frozen.  Alignment comes from their
    # canonical MP3, never from an incidental sidecar next to the script.
    job = Job(stem, audio, script_path, None, batch_id)
    job.task_id = str(plan.get("task_id") or job.task_id)
    batch_output_dir = HOST_ROOT / "storage" / "batch_outputs" / batch_id
    batch_output_dir.mkdir(parents=True, exist_ok=True)
    report_path = batch_output_dir / REPORT_NAME
    report = init_report(batch_id, [job], report_path)
    write_json_atomic(report_path, report)
    plan_visual_style = str(plan.get("visual_style") or VISUAL_STYLE_NONE)
    if visual_style is not None and visual_style != plan_visual_style:
        raise StageError(
            "approved plan visual_style is frozen: "
            f"{plan_visual_style!r} (requested {visual_style!r})"
        )
    return process_job(
        job,
        index=1,
        total=1,
        batch_output_dir=batch_output_dir,
        report=report,
        report_path=report_path,
        preset=preset,
        position=position,
        visual_style=plan_visual_style,
        human_review_mode=False,
        material_title=str(plan.get("material_title") or ""),
    )


if __name__ == "__main__":
    sys.exit(main())
