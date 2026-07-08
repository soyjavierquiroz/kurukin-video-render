"""Safe renderer-core helpers for Kurukin A-roll/B-roll MVP."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import subprocess
from typing import Any, Callable, Literal
from urllib.parse import unquote, urlparse

from app.custom.aroll_broll_mode import FREQUENCY_INTERVAL_SECONDS


RENDERER_LAYOUT_ALTERNATING_FULLSCREEN = "alternating_fullscreen"
DEFAULT_ASPECT_RATIO = "9:16"
DEFAULT_WIDTH = 720
DEFAULT_HEIGHT = 1280
DEFAULT_FPS = 30
DEFAULT_VIDEO_CODEC = "libx264"
DEFAULT_AUDIO_CODEC = "aac"
MAX_INPUT_VIDEO_SECONDS = 60 * 60

SUPPORTED_VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm"}
SUPPORTED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
SUPPORTED_MEDIA_EXTENSIONS = SUPPORTED_VIDEO_EXTENSIONS | SUPPORTED_IMAGE_EXTENSIONS

LOCAL_AROLL_ROOTS = ("storage/local_videos", "storage/local_assets")
LOCAL_BROLL_ROOTS = ("storage/local_videos", "storage/local_assets", "storage/local_images")
DEFAULT_ASSET_HUB_JOB_ASSETS_DIR = Path("/data/job-assets")
FFPROBE_TIMEOUT_SECONDS = 30
FFMPEG_TIMEOUT_SECONDS = 60 * 60


class ArollBrollRendererError(ValueError):
    """Expected A-roll/B-roll renderer validation error."""


@dataclass(frozen=True)
class ArollBrollAsset:
    path: Path
    kind: Literal["video", "image"]
    label: str | None = None


@dataclass(frozen=True)
class ArollBrollRenderPlan:
    a_roll_path: Path
    b_roll_assets: list[ArollBrollAsset]
    output_path: Path
    timeline: list[dict[str, Any]]
    aroll_duration_seconds: float | None = None
    width: int = DEFAULT_WIDTH
    height: int = DEFAULT_HEIGHT
    fps: int = DEFAULT_FPS
    audio_policy: str = "a_roll_original"
    layout_preset: str = RENDERER_LAYOUT_ALTERNATING_FULLSCREEN


@dataclass(frozen=True)
class ArollBrollCommand:
    command: list[str]
    cwd: str | None
    output_path: str
    warnings: list[str] = field(default_factory=list)


Runner = Callable[..., Any]


def _project_root(project_root: str | Path | None = None) -> Path:
    return Path(project_root or Path(__file__).resolve().parents[2]).resolve(
        strict=False
    )


def _has_path_traversal(value: str | Path) -> bool:
    text = str(value)
    if "\\" in text:
        return True
    return ".." in Path(text).parts


def _resolved_roots(roots: list[str | Path] | tuple[str | Path, ...]) -> list[Path]:
    return [Path(root).resolve(strict=False) for root in roots]


def is_path_under(path: str | Path, root: str | Path) -> bool:
    """Return True when path resolves under root, following existing symlinks."""

    try:
        Path(path).resolve(strict=False).relative_to(Path(root).resolve(strict=False))
        return True
    except ValueError:
        return False


def resolve_safe_path(
    path: str | Path,
    allowed_roots: list[str | Path] | tuple[str | Path, ...],
) -> Path:
    """Resolve an existing file while keeping it inside one of allowed_roots."""

    if not allowed_roots:
        raise ArollBrollRendererError("allowed_roots is required")
    if _has_path_traversal(path):
        raise ArollBrollRendererError("path cannot use traversal or backslashes")

    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate

    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ArollBrollRendererError(f"path does not exist: {candidate}") from exc

    roots = _resolved_roots(allowed_roots)
    if not any(is_path_under(resolved, root) for root in roots):
        allowed = ", ".join(root.as_posix() for root in roots)
        raise ArollBrollRendererError(f"path must stay under allowed roots: {allowed}")
    if not resolved.is_file():
        raise ArollBrollRendererError(f"path is not a file: {resolved}")
    return resolved


def _resolve_project_file(
    path: str | Path,
    *,
    project_root: str | Path | None,
    roots: tuple[str, ...],
) -> Path:
    root = _project_root(project_root)
    requested = Path(path)
    candidate = requested if requested.is_absolute() else root / requested
    allowed_roots = [root / item for item in roots]
    return resolve_safe_path(candidate, allowed_roots)


def detect_asset_kind(path: str | Path) -> Literal["video", "image"]:
    suffix = Path(path).suffix.lower()
    if suffix in SUPPORTED_VIDEO_EXTENSIONS:
        return "video"
    if suffix in SUPPORTED_IMAGE_EXTENSIONS:
        return "image"
    allowed = ", ".join(sorted(SUPPORTED_MEDIA_EXTENSIONS))
    raise ArollBrollRendererError(f"unsupported media extension; allowed: {allowed}")


def validate_aroll_path(
    path: str | Path,
    project_root: str | Path | None = None,
) -> Path:
    resolved = _resolve_project_file(
        path,
        project_root=project_root,
        roots=LOCAL_AROLL_ROOTS,
    )
    if detect_asset_kind(resolved) != "video":
        raise ArollBrollRendererError("A-roll must be a supported video file")
    return resolved


def validate_broll_path(
    path: str | Path,
    project_root: str | Path | None = None,
    *,
    asset_hub_root: str | Path = DEFAULT_ASSET_HUB_JOB_ASSETS_DIR,
) -> ArollBrollAsset:
    root = _project_root(project_root)
    requested = Path(path)
    candidate = requested if requested.is_absolute() else root / requested
    allowed_roots: list[Path] = [root / item for item in LOCAL_BROLL_ROOTS]
    allowed_roots.append(Path(asset_hub_root))
    resolved = resolve_safe_path(candidate, allowed_roots)
    return ArollBrollAsset(path=resolved, kind=detect_asset_kind(resolved))


def build_ffprobe_duration_command(path: str | Path) -> list[str]:
    return [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]


def parse_ffprobe_duration(stdout: str) -> float:
    value = str(stdout or "").strip().splitlines()[0] if str(stdout or "").strip() else ""
    try:
        duration = float(value)
    except (TypeError, ValueError) as exc:
        raise ArollBrollRendererError("ffprobe duration output is invalid") from exc
    if duration <= 0:
        raise ArollBrollRendererError("ffprobe duration must be greater than zero")
    return duration


def _runner_result_fields(result: Any) -> tuple[int, str, str]:
    if isinstance(result, dict):
        return (
            int(result.get("returncode", 0)),
            str(result.get("stdout", "")),
            str(result.get("stderr", "")),
        )
    return (
        int(getattr(result, "returncode", 0)),
        str(getattr(result, "stdout", "")),
        str(getattr(result, "stderr", "")),
    )


def get_media_duration_seconds(path: str | Path, runner: Runner | None = None) -> float:
    command = build_ffprobe_duration_command(path)
    if runner is None:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=FFPROBE_TIMEOUT_SECONDS,
        )
    else:
        completed = runner(command, cwd=None, timeout=FFPROBE_TIMEOUT_SECONDS)

    returncode, stdout, stderr = _runner_result_fields(completed)
    if returncode != 0:
        details = stderr.strip() or stdout.strip()
        suffix = f": {details}" if details else ""
        raise ArollBrollRendererError(f"ffprobe failed{suffix}")
    return parse_ffprobe_duration(stdout)


def _is_probable_media_path(value: str) -> bool:
    parsed = urlparse(value)
    if parsed.scheme and parsed.scheme not in {"file"}:
        return False
    path_value = unquote(parsed.path) if parsed.scheme == "file" else value
    return Path(path_value).suffix.lower() in SUPPORTED_MEDIA_EXTENSIONS


def _path_from_manifest_value(value: str) -> Path | None:
    parsed = urlparse(value)
    if parsed.scheme and parsed.scheme != "file":
        return None
    if parsed.scheme == "file":
        return Path(unquote(parsed.path))
    return Path(value)


def _iter_manifest_candidates(value: Any, inherited_kind: str | None = None):
    path_fields = {"path", "local_path", "file_path", "uri", "source_path", "resolved_path"}
    kind_fields = {"type", "kind", "media_type"}
    label_fields = {"label", "name", "filename", "title"}

    if isinstance(value, dict):
        local_kind = inherited_kind
        for key in kind_fields:
            item = value.get(key)
            if isinstance(item, str) and item.lower() in {"video", "image"}:
                local_kind = item.lower()
                break
        label = next(
            (
                str(value[key]).strip()
                for key in label_fields
                if isinstance(value.get(key), str) and str(value[key]).strip()
            ),
            None,
        )
        for key in path_fields:
            item = value.get(key)
            if isinstance(item, str) and _is_probable_media_path(item):
                yield item, local_kind, label
        for item in value.values():
            yield from _iter_manifest_candidates(item, local_kind)
        return

    if isinstance(value, list):
        for item in value:
            yield from _iter_manifest_candidates(item, inherited_kind)
        return

    if isinstance(value, str) and _is_probable_media_path(value):
        yield value, inherited_kind, None


def _manifest_candidate_paths(raw_value: str, manifest_path: Path) -> list[Path]:
    parsed = _path_from_manifest_value(raw_value)
    if parsed is None:
        return []
    if parsed.is_absolute():
        return [parsed]
    manifest_dir = manifest_path.parent
    bundle_root = manifest_path.parent.parent
    return [manifest_dir / parsed, bundle_root / parsed]


def _safe_existing_manifest_asset(
    raw_value: str,
    *,
    manifest_path: Path,
    project_root: str | Path | None,
    asset_hub_root: Path,
) -> tuple[Path | None, str | None]:
    for candidate in _manifest_candidate_paths(raw_value, manifest_path):
        resolved = candidate.resolve(strict=False)
        allowed_roots: list[Path] = [asset_hub_root]
        if project_root is not None:
            root = _project_root(project_root)
            allowed_roots.extend(root / item for item in LOCAL_BROLL_ROOTS)
        if not any(is_path_under(resolved, root) for root in allowed_roots):
            continue
        if not resolved.exists():
            continue
        try:
            return resolve_safe_path(resolved, allowed_roots), None
        except ArollBrollRendererError as exc:
            return None, str(exc)
    return None, f"manifest asset not found or outside allowed roots: {raw_value}"


def extract_broll_assets_from_manifest(
    manifest_path: str | Path,
    project_root: str | Path | None = None,
    *,
    allowed_root: str | Path | None = None,
) -> dict[str, Any]:
    """Read a renderer manifest and extract local B-roll assets without API calls."""

    warnings: list[str] = []
    asset_hub_root = Path(allowed_root or DEFAULT_ASSET_HUB_JOB_ASSETS_DIR).resolve(
        strict=False
    )
    requested = Path(manifest_path)
    candidate = requested if requested.is_absolute() else asset_hub_root / requested
    if _has_path_traversal(candidate):
        raise ArollBrollRendererError("manifest_path cannot use path traversal")
    resolved_manifest = candidate.resolve(strict=False)
    if not is_path_under(resolved_manifest, asset_hub_root):
        raise ArollBrollRendererError("manifest_path must stay under /data/job-assets")
    if resolved_manifest.suffix.lower() != ".json":
        raise ArollBrollRendererError("manifest_path must point to a .json file")
    if not resolved_manifest.exists():
        return {
            "assets": [],
            "warnings": [f"manifest file not found: {resolved_manifest.as_posix()}"],
        }
    if not resolved_manifest.is_file():
        raise ArollBrollRendererError("manifest_path is not a file")

    with resolved_manifest.open("r", encoding="utf-8") as handle:
        manifest = json.load(handle)

    assets: list[ArollBrollAsset] = []
    seen: set[str] = set()
    for raw_value, manifest_kind, label in _iter_manifest_candidates(manifest):
        resolved_asset, warning = _safe_existing_manifest_asset(
            raw_value,
            manifest_path=resolved_manifest,
            project_root=project_root,
            asset_hub_root=asset_hub_root,
        )
        if warning:
            warnings.append(warning)
            continue
        if resolved_asset is None:
            continue
        key = resolved_asset.as_posix()
        if key in seen:
            continue
        kind = detect_asset_kind(resolved_asset)
        if manifest_kind in {"video", "image"} and manifest_kind != kind:
            warnings.append(
                f"manifest kind {manifest_kind} does not match extension for {key}"
            )
        assets.append(ArollBrollAsset(path=resolved_asset, kind=kind, label=label))
        seen.add(key)

    return {"assets": assets, "warnings": warnings}


def build_alternating_fullscreen_timeline(
    aroll_duration_seconds: float,
    broll_assets: list[ArollBrollAsset],
    clip_seconds: int | float,
    frequency: str,
) -> list[dict[str, Any]]:
    try:
        total_duration = max(0.0, float(aroll_duration_seconds))
    except (TypeError, ValueError):
        total_duration = 0.0
    try:
        clip_duration = max(0.0, float(clip_seconds))
    except (TypeError, ValueError):
        clip_duration = 0.0

    if total_duration <= 0:
        return []
    if not broll_assets or clip_duration <= 0:
        return [
            {
                "start": 0.0,
                "end": round(total_duration, 3),
                "visual": "a_roll",
                "layout": RENDERER_LAYOUT_ALTERNATING_FULLSCREEN,
            }
        ]

    interval = FREQUENCY_INTERVAL_SECONDS.get(frequency, FREQUENCY_INTERVAL_SECONDS["medium"])
    timeline: list[dict[str, Any]] = []
    cursor = 0.0
    broll_segment_count = 0
    next_broll_start = min(interval / 2.0, total_duration)

    while cursor < total_duration:
        if next_broll_start >= total_duration:
            timeline.append(
                {
                    "start": round(cursor, 3),
                    "end": round(total_duration, 3),
                    "visual": "a_roll",
                    "layout": RENDERER_LAYOUT_ALTERNATING_FULLSCREEN,
                }
            )
            break

        broll_start = max(cursor, min(next_broll_start, total_duration))
        if cursor < broll_start:
            timeline.append(
                {
                    "start": round(cursor, 3),
                    "end": round(broll_start, 3),
                    "visual": "a_roll",
                    "layout": RENDERER_LAYOUT_ALTERNATING_FULLSCREEN,
                }
            )

        broll_end = min(broll_start + clip_duration, total_duration)
        if broll_end <= broll_start:
            break
        broll_index = broll_segment_count % len(broll_assets)
        timeline.append(
            {
                "start": round(broll_start, 3),
                "end": round(broll_end, 3),
                "visual": "b_roll",
                "broll_index": broll_index,
                "broll_path": broll_assets[broll_index].path.as_posix(),
                "broll_kind": broll_assets[broll_index].kind,
                "layout": RENDERER_LAYOUT_ALTERNATING_FULLSCREEN,
            }
        )
        broll_segment_count += 1
        cursor = broll_end
        next_broll_start = cursor + interval

    return [item for item in timeline if item["end"] > item["start"]]


def _format_seconds(value: float) -> str:
    text = f"{float(value):.3f}".rstrip("0").rstrip(".")
    return text or "0"


def _scale_crop_filter(width: int, height: int, fps: int) -> str:
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},setsar=1,fps={fps},format=yuv420p"
    )


def _timeline_duration_seconds(timeline: list[dict[str, Any]]) -> float:
    try:
        return max(float(item["end"]) for item in timeline)
    except (KeyError, TypeError, ValueError) as exc:
        raise ArollBrollRendererError("timeline end values must be numeric") from exc


def _target_duration_seconds(plan: ArollBrollRenderPlan) -> float:
    if plan.aroll_duration_seconds is None:
        duration = _timeline_duration_seconds(plan.timeline)
    else:
        try:
            duration = float(plan.aroll_duration_seconds)
        except (TypeError, ValueError) as exc:
            raise ArollBrollRendererError(
                "A-roll duration must be numeric"
            ) from exc
    if duration <= 0:
        raise ArollBrollRendererError("A-roll duration must be greater than zero")
    return duration


def _clamp_timeline_to_duration(
    timeline: list[dict[str, Any]],
    duration_seconds: float,
) -> list[dict[str, Any]]:
    clamped: list[dict[str, Any]] = []
    for item in timeline:
        try:
            start = max(0.0, float(item["start"]))
            end = min(float(item["end"]), duration_seconds)
        except (KeyError, TypeError, ValueError) as exc:
            raise ArollBrollRendererError(
                "timeline start and end values must be numeric"
            ) from exc
        if end <= start:
            continue
        segment = dict(item)
        segment["start"] = start
        segment["end"] = end
        clamped.append(segment)
    return clamped


def build_alternating_fullscreen_ffmpeg_command(
    plan: ArollBrollRenderPlan,
) -> list[str]:
    if plan.layout_preset != RENDERER_LAYOUT_ALTERNATING_FULLSCREEN:
        raise ArollBrollRendererError("only alternating_fullscreen is supported")
    if plan.width <= 0 or plan.height <= 0 or plan.fps <= 0:
        raise ArollBrollRendererError("width, height, and fps must be positive")
    if not plan.timeline:
        raise ArollBrollRendererError("timeline is required")

    total_duration = _target_duration_seconds(plan)
    timeline = _clamp_timeline_to_duration(plan.timeline, total_duration)
    if not timeline:
        raise ArollBrollRendererError("timeline has no renderable segments")

    command = ["ffmpeg", "-y", "-i", plan.a_roll_path.as_posix()]
    for asset in plan.b_roll_assets:
        if asset.kind == "image":
            command.extend(["-loop", "1", "-framerate", str(plan.fps), "-i", asset.path.as_posix()])
        else:
            command.extend(["-stream_loop", "-1", "-i", asset.path.as_posix()])

    scale_crop = _scale_crop_filter(plan.width, plan.height, plan.fps)
    filters: list[str] = []
    segment_labels: list[str] = []
    for index, segment in enumerate(timeline):
        start = float(segment["start"])
        end = float(segment["end"])
        duration = end - start
        if duration <= 0:
            continue
        label = f"v{index}"
        if segment.get("visual") == "a_roll":
            filters.append(
                f"[0:v]trim=start={_format_seconds(start)}:end={_format_seconds(end)},"
                f"setpts=PTS-STARTPTS,{scale_crop}[{label}]"
            )
        elif segment.get("visual") == "b_roll":
            if not plan.b_roll_assets:
                raise ArollBrollRendererError("timeline references B-roll without assets")
            broll_index = int(segment.get("broll_index", 0)) % len(plan.b_roll_assets)
            input_index = broll_index + 1
            asset = plan.b_roll_assets[broll_index]
            if asset.kind == "image":
                trim_filter = f"trim=duration={_format_seconds(duration)}"
            else:
                trim_filter = f"trim=start=0:duration={_format_seconds(duration)}"
            filters.append(
                f"[{input_index}:v]{trim_filter},setpts=PTS-STARTPTS,"
                f"{scale_crop}[{label}]"
            )
        else:
            raise ArollBrollRendererError("timeline visual must be a_roll or b_roll")
        segment_labels.append(f"[{label}]")

    if not segment_labels:
        raise ArollBrollRendererError("timeline has no renderable segments")
    if len(segment_labels) == 1:
        filters.append(f"{segment_labels[0]}null[vout]")
    else:
        filters.append(
            f"{''.join(segment_labels)}concat=n={len(segment_labels)}:v=1:a=0[vout]"
        )

    command.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[vout]",
            "-map",
            "0:a?",
            "-t",
            _format_seconds(total_duration),
            "-c:v",
            DEFAULT_VIDEO_CODEC,
            "-c:a",
            DEFAULT_AUDIO_CODEC,
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            plan.output_path.as_posix(),
        ]
    )
    return command


def build_aroll_broll_command(plan: ArollBrollRenderPlan) -> ArollBrollCommand:
    return ArollBrollCommand(
        command=build_alternating_fullscreen_ffmpeg_command(plan),
        cwd=None,
        output_path=plan.output_path.as_posix(),
        warnings=[],
    )


def _config_section(config: dict[str, Any], key: str) -> dict[str, Any]:
    value = config.get(key)
    if not isinstance(value, dict):
        raise ArollBrollRendererError(f"aroll_broll.{key} must be an object")
    return value


def _local_asset_path(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("path", "local_path", "file_path", "source_path", "resolved_path"):
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                return item.strip()
    return ""


def _broll_assets_from_config(
    b_roll: dict[str, Any],
    *,
    project_root: str | Path | None,
) -> list[ArollBrollAsset]:
    assets: list[ArollBrollAsset] = []
    raw_assets = b_roll.get("assets")
    if isinstance(raw_assets, list):
        for item in raw_assets:
            asset_path = _local_asset_path(item)
            if not asset_path:
                raise ArollBrollRendererError("b_roll.assets entries must include a path")
            assets.append(validate_broll_path(asset_path, project_root=project_root))
        return assets

    raw_paths = b_roll.get("paths")
    if isinstance(raw_paths, list):
        for item in raw_paths:
            asset_path = _local_asset_path(item)
            if not asset_path:
                raise ArollBrollRendererError("b_roll.paths entries must include a path")
            assets.append(validate_broll_path(asset_path, project_root=project_root))
        return assets

    single_path = _local_asset_path(b_roll.get("path"))
    if single_path:
        return [validate_broll_path(single_path, project_root=project_root)]

    manifest_path = b_roll.get("manifest_path")
    if isinstance(manifest_path, str) and manifest_path.strip():
        extracted = extract_broll_assets_from_manifest(
            manifest_path.strip(),
            project_root=project_root,
        )
        return list(extracted.get("assets") or [])

    return []


def _positive_duration(value: Any) -> float | None:
    if value is None:
        return None
    try:
        duration = float(value)
    except (TypeError, ValueError) as exc:
        raise ArollBrollRendererError("A-roll duration must be numeric") from exc
    if duration <= 0:
        raise ArollBrollRendererError("A-roll duration must be greater than zero")
    if duration > MAX_INPUT_VIDEO_SECONDS:
        raise ArollBrollRendererError("A-roll duration exceeds maximum supported length")
    return duration


def build_aroll_broll_plan_from_job(
    job: dict[str, Any],
    *,
    project_root: str | Path | None = None,
    task_id: str | None = None,
    duration_runner: Runner | None = None,
    aroll_duration_seconds: float | None = None,
) -> ArollBrollRenderPlan:
    """Build a direct renderer plan from a guarded pending job without API calls."""

    if not isinstance(job, dict):
        raise ArollBrollRendererError("job must be a JSON object")
    if job.get("render_mode") != "aroll_broll":
        raise ArollBrollRendererError("render_mode must be aroll_broll")

    config = job.get("aroll_broll")
    if not isinstance(config, dict):
        raise ArollBrollRendererError("aroll_broll must be an object")

    a_roll = _config_section(config, "a_roll")
    b_roll = _config_section(config, "b_roll")
    layout = _config_section(config, "layout")

    layout_preset = str(layout.get("preset") or RENDERER_LAYOUT_ALTERNATING_FULLSCREEN)
    if layout_preset != RENDERER_LAYOUT_ALTERNATING_FULLSCREEN:
        raise ArollBrollRendererError("only alternating_fullscreen is supported")

    a_roll_path = _local_asset_path(a_roll.get("path"))
    if not a_roll_path:
        raise ArollBrollRendererError("a_roll.path is required")
    resolved_aroll_path = validate_aroll_path(a_roll_path, project_root=project_root)

    b_roll_assets = _broll_assets_from_config(b_roll, project_root=project_root)
    if not b_roll_assets:
        raise ArollBrollRendererError("at least one B-roll asset is required")

    clean_task_id = str(task_id or job.get("task_id") or job.get("job_id") or "").strip()
    output_path = build_aroll_broll_output_path(clean_task_id, project_root=project_root)

    duration = (
        _positive_duration(aroll_duration_seconds)
        or _positive_duration(a_roll.get("duration_seconds"))
        or get_media_duration_seconds(resolved_aroll_path, runner=duration_runner)
    )
    timeline = build_alternating_fullscreen_timeline(
        duration,
        b_roll_assets,
        b_roll.get("clip_seconds", 4),
        str(b_roll.get("frequency") or "medium"),
    )
    return ArollBrollRenderPlan(
        a_roll_path=resolved_aroll_path,
        b_roll_assets=b_roll_assets,
        output_path=output_path,
        timeline=timeline,
        aroll_duration_seconds=duration,
        layout_preset=layout_preset,
    )


def run_aroll_broll_render(
    plan: ArollBrollRenderPlan,
    runner: Runner | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    command = build_alternating_fullscreen_ffmpeg_command(plan)
    if dry_run:
        return {
            "ok": True,
            "command": command,
            "output_path": plan.output_path.as_posix(),
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "warnings": [],
            "dry_run": True,
        }

    Path(plan.output_path).parent.mkdir(parents=True, exist_ok=True)

    if runner is None:
        completed = subprocess.run(
            command,
            cwd=None,
            timeout=FFMPEG_TIMEOUT_SECONDS,
            check=False,
            capture_output=True,
            text=True,
        )
    else:
        completed = runner(command, cwd=None, timeout=FFMPEG_TIMEOUT_SECONDS)
    returncode, stdout, stderr = _runner_result_fields(completed)
    return {
        "ok": returncode == 0,
        "command": command,
        "output_path": plan.output_path.as_posix(),
        "returncode": returncode,
        "stdout": stdout,
        "stderr": stderr,
        "warnings": [],
        "dry_run": False,
    }


def build_aroll_broll_output_path(
    task_id: str,
    project_root: str | Path | None = None,
) -> Path:
    value = str(task_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ArollBrollRendererError("task_id must use only letters, numbers, - or _")
    root = _project_root(project_root)
    output_path = (root / "storage" / "tasks" / value / "final-1.mp4").resolve(
        strict=False
    )
    tasks_root = (root / "storage" / "tasks").resolve(strict=False)
    if not is_path_under(output_path, tasks_root):
        raise ArollBrollRendererError("output path must stay under storage/tasks")
    return output_path
