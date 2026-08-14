import itertools
import io
import hashlib
import math
import os
import random
import gc
import subprocess
import sys
import tempfile
import unicodedata
from contextlib import ExitStack, redirect_stdout
from dataclasses import dataclass
from functools import lru_cache
from typing import List
from loguru import logger
import numpy as np
from moviepy import (
    AudioFileClip,
    ColorClip,
    CompositeAudioClip,
    CompositeVideoClip,
    ImageClip,
    TextClip,
    VideoFileClip,
    afx,
    vfx,
)
from moviepy.video.tools.subtitles import SubtitlesClip
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from app.config import config
from app.custom import asset_hub_manifest
from app.models import const
from app.models.schema import (
    MaterialInfo,
    VideoAspect,
    VideoConcatMode,
    VideoParams,
    VideoTransitionMode,
)
from app.services import bgm as bgm_service
from app.services.utils import video_effects
from app.utils import file_security, utils

class SubClippedVideoClip:
    def __init__(
        self,
        file_path,
        start_time=None,
        end_time=None,
        width=None,
        height=None,
        duration=None,
        source_file_path=None,
    ):
        self.file_path = file_path
        self.start_time = start_time
        self.end_time = end_time
        self.width = width
        self.height = height
        self.source_file_path = source_file_path or file_path
        if duration is None:
            self.duration = end_time - start_time
        else:
            self.duration = duration

    def __str__(self):
        return f"SubClippedVideoClip(file_path={self.file_path}, start_time={self.start_time}, end_time={self.end_time}, duration={self.duration}, width={self.width}, height={self.height})"


audio_codec = "aac"
# Docker 里的 ffmpeg/AAC 组合在默认配置下更容易出现音频质量波动，
# 这里显式抬高音频码率，避免成片阶段因为默认值过低而引入明显失真。
audio_bitrate = "192k"
fps = 30
# FFmpeg 按帧率拼接/转码时，最终时长可能比 MoviePy 读到的理论时长短几十毫秒。
# 这里给视频素材多留一个很小的安全余量，避免音频末尾因为帧舍入出现黑屏、
# 卡顿或最后一小段旁白没有画面的情况。
_VIDEO_DURATION_SAFETY_MARGIN = 0.1
_MIN_MATERIAL_DIMENSION = 480
# 消息类应用和部分编码器会把画面尺寸向下取整，例如 WhatsApp 会把 9:16 的
# 素材压成 478x850，比 480 少两个像素。直接按 480 硬卡会让这类素材全部被
# 丢弃，最终以 "no valid materials found" 整体失败。这里留一个很小的容差，
# 既能放行仅仅因为取整而略低于阈值的素材，也仍然能挡住真正的低清素材。
_MIN_DIMENSION_TOLERANCE = 10
_DEFAULT_VIDEO_CODEC = "libx264"
_SUPPORTED_VIDEO_CODECS = (
    "libx264",
    "h264_nvenc",
    "h264_amf",
    "h264_qsv",
    "h264_mf",
    "h264_videotoolbox",
)
_runtime_disabled_video_codecs = set()
_IMAGE_MOTION_PRESETS = {
    "none",
    "slow_zoom_in",
    "slow_zoom_out",
    "pan_left",
    "pan_right",
    "pan_up",
    "pan_down",
    "subtle_pulse",
    "handheld_soft",
}
_IMAGE_MOTION_ALIASES = {
    "zoom_in": "slow_zoom_in",
    "zoom_out": "slow_zoom_out",
    "ken_burns": "slow_zoom_in",
    "pulse": "subtle_pulse",
    "handheld": "handheld_soft",
}
_IMAGE_MOTION_DEFAULT_INTENSITY = 0.06
_IMAGE_MOTION_MAX_INTENSITY = 0.20
_VIDEO_EDGE_FEATHER_ENABLED = True
_VIDEO_EDGE_FEATHER_REFERENCE_HEIGHT = 1920
_VIDEO_EDGE_FEATHER_VISIBLE_RATIO = 0.12
_VIDEO_EDGE_FEATHER_MAX_ALPHA = 1.0
_VIDEO_EDGE_FEATHER_HOLD_REFERENCE_SIZE = 12
_VIDEO_EDGE_FEATHER_BLUR_REFERENCE_RADIUS = 3
_VIDEO_TRANSITION_DURATION = 0.18


@dataclass(frozen=True)
class _ScaledContentGeometry:
    canvas_width: int
    canvas_height: int
    scaled_width: int
    scaled_height: int
    content_left: int
    content_top: int
    content_right: int
    content_bottom: int

    @property
    def canvas_size(self) -> tuple[int, int]:
        return (self.canvas_width, self.canvas_height)

    @property
    def content_size(self) -> tuple[int, int]:
        return (self.scaled_width, self.scaled_height)


def _normalize_video_resolution(video_resolution: str = "") -> str:
    raw_value = "" if video_resolution is None else str(video_resolution)
    normalized = raw_value.strip().lower()
    aliases = {
        "": "standard_1080p",
        "standard": "standard_1080p",
        "standard_1080p": "standard_1080p",
        "1080p": "standard_1080p",
        "draft": "draft_720p",
        "draft_720p": "draft_720p",
        "720p": "draft_720p",
        "premium": "premium_2k",
        "premium_2k": "premium_2k",
        "2k": "premium_2k",
        "1440p": "premium_2k",
    }
    try:
        return aliases[normalized]
    except KeyError as exc:
        raise ValueError(
            f"Unsupported video_resolution {raw_value!r}. Expected 720p, 1080p, or 2k."
        ) from exc


def resolve_video_size(video_aspect: str, video_resolution: str = "") -> tuple[int, int]:
    profile = _normalize_video_resolution(video_resolution)
    aspect = VideoAspect(video_aspect)
    sizes = {
        VideoAspect.portrait: {
            "draft_720p": (720, 1280),
            "standard_1080p": (1080, 1920),
            "premium_2k": (1440, 2560),
        },
        VideoAspect.landscape: {
            "draft_720p": (1280, 720),
            "standard_1080p": (1920, 1080),
            "premium_2k": (2560, 1440),
        },
    }
    if aspect in sizes:
        return sizes[aspect][profile]
    if profile == "standard_1080p":
        return aspect.to_resolution()
    raise ValueError("only 9:16 and 16:9 support non-standard video resolutions")


def _has_vertical_letterbox(
    *,
    canvas_size: tuple[int, int],
    content_size: tuple[int, int],
) -> bool:
    canvas_width, canvas_height = canvas_size
    content_width, content_height = content_size
    if min(canvas_width, canvas_height, content_width, content_height) <= 0:
        return False
    return content_width >= canvas_width and content_height < canvas_height


def _scaled_content_geometry(
    *,
    source_size: tuple[int, int],
    canvas_size: tuple[int, int],
) -> _ScaledContentGeometry:
    source_width, source_height = source_size
    canvas_width, canvas_height = canvas_size
    if min(source_width, source_height, canvas_width, canvas_height) <= 0:
        return _ScaledContentGeometry(
            canvas_width=canvas_width,
            canvas_height=canvas_height,
            scaled_width=max(1, canvas_width),
            scaled_height=max(1, canvas_height),
            content_left=0,
            content_top=0,
            content_right=max(1, canvas_width),
            content_bottom=max(1, canvas_height),
        )

    source_ratio = source_width / source_height
    canvas_ratio = canvas_width / canvas_height
    if math.isclose(source_ratio, canvas_ratio, rel_tol=0.0, abs_tol=1e-6):
        scaled_width, scaled_height = canvas_width, canvas_height
    elif source_ratio > canvas_ratio:
        scale_factor = canvas_width / source_width
        scaled_width = canvas_width
        scaled_height = max(1, int(source_height * scale_factor))
    else:
        scale_factor = canvas_height / source_height
        scaled_width = max(1, int(source_width * scale_factor))
        scaled_height = canvas_height

    content_left = max(0, (canvas_width - scaled_width) // 2)
    content_top = max(0, (canvas_height - scaled_height) // 2)
    return _ScaledContentGeometry(
        canvas_width=canvas_width,
        canvas_height=canvas_height,
        scaled_width=scaled_width,
        scaled_height=scaled_height,
        content_left=content_left,
        content_top=content_top,
        content_right=content_left + scaled_width,
        content_bottom=content_top + scaled_height,
    )


def _geometry_has_vertical_letterbox(geometry: _ScaledContentGeometry) -> bool:
    return _has_vertical_letterbox(
        canvas_size=geometry.canvas_size,
        content_size=geometry.content_size,
    )


def _video_edge_feather_size(content_height: int) -> int:
    if content_height <= 0:
        return 0
    return max(
        1,
        int(round(content_height * _VIDEO_EDGE_FEATHER_VISIBLE_RATIO)),
    )


def _video_edge_feather_hold_size(canvas_height: int) -> int:
    if canvas_height <= 0:
        return 0
    return max(
        1,
        int(round(canvas_height * _VIDEO_EDGE_FEATHER_HOLD_REFERENCE_SIZE / _VIDEO_EDGE_FEATHER_REFERENCE_HEIGHT)),
    )


def _video_edge_feather_blur_radius(canvas_height: int) -> float:
    if canvas_height <= 0:
        return 0.0
    return max(
        0.0,
        canvas_height * _VIDEO_EDGE_FEATHER_BLUR_REFERENCE_RADIUS / _VIDEO_EDGE_FEATHER_REFERENCE_HEIGHT,
    )


def _video_edge_feather_overlay_path(
    *,
    geometry: _ScaledContentGeometry,
) -> str:
    canvas_width, canvas_height = geometry.canvas_size
    feather_total = min(
        _video_edge_feather_size(geometry.scaled_height),
        max(1, geometry.scaled_height // 2),
    )
    hold = min(
        _video_edge_feather_hold_size(canvas_height),
        max(1, feather_total),
    )
    feather = max(1, feather_total - hold)
    max_alpha = max(0, min(255, int(round(255 * _VIDEO_EDGE_FEATHER_MAX_ALPHA))))
    blur_radius = _video_edge_feather_blur_radius(canvas_height)
    key = (
        f"{canvas_width}x{canvas_height}-{geometry.scaled_width}x{geometry.scaled_height}-"
        f"{geometry.content_top}-{geometry.content_bottom}-{feather}-{hold}-"
        f"{max_alpha}-{blur_radius:.2f}"
    )
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
    overlay_dir = utils.storage_dir("temp/video_overlays", create=True)
    overlay_path = os.path.join(overlay_dir, f"edge-feather-{digest}.png")
    if os.path.exists(overlay_path):
        return overlay_path

    alpha = np.zeros((canvas_height, canvas_width), dtype=np.uint8)
    top_content_start = geometry.content_top
    top_hold_end = min(canvas_height, top_content_start + hold)
    top_feather_end = min(canvas_height, top_hold_end + feather)
    alpha[:top_hold_end, :] = max_alpha
    if top_feather_end > top_hold_end:
        progress = np.linspace(0, 1, top_feather_end - top_hold_end)
        ramp = (max_alpha * (1 - progress) ** 3).astype(np.uint8)[:, None]
        alpha[top_hold_end:top_feather_end, :] = np.maximum(
            alpha[top_hold_end:top_feather_end, :],
            ramp,
        )

    bottom_content_end = geometry.content_bottom
    bottom_hold_start = max(0, bottom_content_end - hold)
    bottom_feather_start = max(0, bottom_hold_start - feather)
    if bottom_hold_start > bottom_feather_start:
        progress = np.linspace(0, 1, bottom_hold_start - bottom_feather_start)
        ramp = (max_alpha * progress ** 3).astype(np.uint8)[:, None]
        alpha[bottom_feather_start:bottom_hold_start, :] = np.maximum(
            alpha[bottom_feather_start:bottom_hold_start, :],
            ramp,
        )
    alpha[bottom_hold_start:, :] = max_alpha

    rgba = np.zeros((canvas_height, canvas_width, 4), dtype=np.uint8)
    rgba[:, :, 3] = alpha
    image = Image.fromarray(rgba)
    if blur_radius > 0:
        image = image.filter(ImageFilter.GaussianBlur(radius=blur_radius))
    image.save(overlay_path)
    return overlay_path


def _apply_vertical_letterbox_feather(clip, *, geometry: _ScaledContentGeometry):
    if not _VIDEO_EDGE_FEATHER_ENABLED:
        return clip
    if not _geometry_has_vertical_letterbox(geometry):
        return clip
    overlay_path = _video_edge_feather_overlay_path(geometry=geometry)
    overlay = ImageClip(overlay_path, transparent=True).with_duration(clip.duration)
    return CompositeVideoClip([clip, overlay], size=geometry.canvas_size).with_duration(clip.duration)


def _apply_dip_to_black_transition(clip, *, fade_in: bool, fade_out: bool):
    duration = float(getattr(clip, "duration", 0) or 0)
    transition_duration = min(_VIDEO_TRANSITION_DURATION, max(0.0, duration / 2))
    if transition_duration <= 0:
        return clip
    effects = []
    if fade_in:
        effects.append(vfx.FadeIn(transition_duration, initial_color=[0, 0, 0]))
    if fade_out:
        effects.append(vfx.FadeOut(transition_duration, final_color=[0, 0, 0]))
    if not effects or not hasattr(clip, "with_effects"):
        return clip
    return clip.with_effects(effects)


def _dip_to_black_flags(
    *,
    clip_index: int,
    clip_count: int,
    selected_count: int,
    current_duration: float,
    clip_duration: float,
    required_duration: float,
) -> tuple[bool, bool]:
    if clip_count <= 1:
        return False, False
    fade_in = selected_count > 0
    fade_out = (
        clip_index < clip_count - 1
        and current_duration + clip_duration < required_duration
    )
    return fade_in, fade_out


def normalize_image_motion_preset(value: str) -> str:
    raw_value = "" if value is None else str(value)
    normalized = raw_value.strip().lower()
    if not normalized:
        return "none"
    normalized = _IMAGE_MOTION_ALIASES.get(normalized, normalized)
    if normalized not in _IMAGE_MOTION_PRESETS:
        raise ValueError(f"Unsupported image motion preset {raw_value!r}.")
    return normalized


def clamp_image_motion_intensity(value: float | int | None) -> float:
    if value in (None, ""):
        return _IMAGE_MOTION_DEFAULT_INTENSITY
    if isinstance(value, bool):
        raise ValueError("image motion intensity must be a number between 0.0 and 0.20")
    try:
        intensity = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "image motion intensity must be a number between 0.0 and 0.20"
        ) from exc
    if not math.isfinite(intensity):
        raise ValueError("image motion intensity must be a finite number")
    if intensity < 0.0 or intensity > _IMAGE_MOTION_MAX_INTENSITY:
        raise ValueError("image motion intensity must be between 0.0 and 0.20")
    return intensity


def is_image_file(path_or_name: str) -> bool:
    extension = os.path.splitext(str(path_or_name or ""))[1].lower().lstrip(".")
    return extension in {"jpg", "jpeg", "png"}


def _progress(current_time: float, duration: float) -> float:
    return min(max(current_time / max(duration, 0.001), 0.0), 1.0)


def _cover_image_size(image_path: str, target_size: tuple[int, int]) -> tuple[int, int]:
    target_width, target_height = target_size
    if not hasattr(Image, "open"):
        return target_size
    with Image.open(image_path) as image:
        image_width, image_height = image.size
    if image_width <= 0 or image_height <= 0:
        raise ValueError(f"invalid image size for {image_path}")
    cover_scale = max(target_width / image_width, target_height / image_height)
    return (
        max(target_width, int(math.ceil(image_width * cover_scale))),
        max(target_height, int(math.ceil(image_height * cover_scale))),
    )


def create_image_motion_clip(
    image_path: str,
    duration: float,
    target_size: tuple[int, int],
    motion_preset: str = "none",
    intensity: float = _IMAGE_MOTION_DEFAULT_INTENSITY,
):
    preset = normalize_image_motion_preset(motion_preset)
    motion_intensity = clamp_image_motion_intensity(intensity)
    target_width, target_height = target_size
    if target_width <= 0 or target_height <= 0:
        raise ValueError("target_size must contain positive width and height")
    clip_duration = float(duration)
    if clip_duration <= 0:
        raise ValueError("image motion duration must be greater than 0")

    base_width, base_height = _cover_image_size(image_path, target_size)
    motion_pad = 1.0 + max(motion_intensity, 0.01)

    if preset in {"pan_left", "pan_right", "pan_up", "pan_down", "handheld_soft"}:
        base_width = int(math.ceil(base_width * motion_pad))
        base_height = int(math.ceil(base_height * motion_pad))

    def scale_at(current_time: float) -> float:
        progress = _progress(current_time, clip_duration)
        if preset == "slow_zoom_in":
            return 1.0 + (motion_intensity * progress)
        if preset == "slow_zoom_out":
            return 1.0 + (motion_intensity * (1.0 - progress))
        if preset == "subtle_pulse":
            return 1.0 + (motion_intensity * 0.5 * math.sin(math.pi * progress) ** 2)
        if preset == "handheld_soft":
            return 1.0 + min(motion_intensity * 0.25, 0.02)
        return 1.0

    def position_at(current_time: float):
        progress = _progress(current_time, clip_duration)
        scale = scale_at(current_time)
        width = base_width * scale
        height = base_height * scale
        overflow_x = max(width - target_width, 0.0)
        overflow_y = max(height - target_height, 0.0)
        center_x = -overflow_x / 2.0
        center_y = -overflow_y / 2.0

        if preset == "pan_left":
            return (-overflow_x * progress, center_y)
        if preset == "pan_right":
            return (-overflow_x * (1.0 - progress), center_y)
        if preset == "pan_up":
            return (center_x, -overflow_y * progress)
        if preset == "pan_down":
            return (center_x, -overflow_y * (1.0 - progress))
        if preset == "handheld_soft":
            amplitude_x = min(target_width * motion_intensity * 0.10, 12.0)
            amplitude_y = min(target_height * motion_intensity * 0.08, 12.0)
            return (
                center_x + math.sin(current_time * 2.1) * amplitude_x,
                center_y + math.cos(current_time * 1.7) * amplitude_y,
            )
        return (center_x, center_y)

    image_clip = ImageClip(image_path)
    if not hasattr(image_clip, "with_duration"):
        image_clip.size = target_size
        image_clip.duration = clip_duration
        return image_clip

    image_clip = image_clip.with_duration(clip_duration).resized(
        new_size=(base_width, base_height)
    )
    if preset in {"slow_zoom_in", "slow_zoom_out", "subtle_pulse", "handheld_soft"}:
        image_clip = image_clip.resized(scale_at)
    image_clip = image_clip.with_position(position_at)
    return CompositeVideoClip([image_clip], size=target_size).with_duration(clip_duration)


def _get_required_video_duration(audio_duration: float) -> float:
    """
    返回视频素材拼接的目标时长。

    使用场景：合成视频时需要素材时长覆盖旁白音频。只做到“刚好等于”
    音频时长时，FFmpeg 可能因为帧率舍入让最终视频略短，因此统一加一个
    轻量余量。函数独立出来，便于测试和后续按实际反馈调整余量大小。
    """
    return max(0.0, float(audio_duration) + _VIDEO_DURATION_SAFETY_MARGIN)


def is_material_resolution_acceptable(width: int, height: int) -> bool:
    """
    判断素材分辨率是否足够用于合成。

    标称最小值是 480x480，但允许比它低 `_MIN_DIMENSION_TOLERANCE` 个像素，
    以兼容编码器/消息应用向下取整导致的尺寸（例如 WhatsApp 的 478x850）。
    """
    min_dimension = _MIN_MATERIAL_DIMENSION - _MIN_DIMENSION_TOLERANCE
    return width >= min_dimension and height >= min_dimension


def _prioritize_unique_source_clips(
    subclipped_items: List[SubClippedVideoClip],
    concat_mode: VideoConcatMode,
) -> List[SubClippedVideoClip]:
    """
    优先让每个源素材只出现一次，降低成片里同一素材反复出现的概率。

    线上素材经常会遇到“一个长视频被切成多个短片段”的情况。旧逻辑在
    random 模式下直接打乱所有短片段，导致同一个源视频的多个切片可能
    分布在开头和中间，用户会感知为素材重复。本函数只调整片段顺序：
    先放每个源文件里最长的一个片段，剩余片段作为兜底；当素材总时长不足时，
    仍然允许后续片段补齐音频长度，避免破坏视频生成成功率。优先选择最长
    片段是为了避免随机选中视频尾部的零碎短片段，导致明明有足够素材却过早复用。
    """
    if not subclipped_items:
        return []

    concat_mode_value = getattr(concat_mode, "value", concat_mode)
    if concat_mode_value != VideoConcatMode.random.value:
        return subclipped_items

    grouped_items: dict[str, list[SubClippedVideoClip]] = {}
    for item in subclipped_items:
        grouped_items.setdefault(item.source_file_path, []).append(item)

    primary_items = []
    overflow_items = []
    for items in grouped_items.values():
        primary_item = max(items, key=lambda item: item.duration)
        primary_items.append(primary_item)
        overflow_items.extend(item for item in items if item is not primary_item)

    random.shuffle(primary_items)
    random.shuffle(overflow_items)
    logger.info(
        "prioritized unique video materials, "
        f"sources: {len(grouped_items)}, "
        f"primary clips: {len(primary_items)}, "
        f"fallback clips: {len(overflow_items)}"
    )
    return primary_items + overflow_items


def get_ffmpeg_binary():
    """
    兼容历史上直接从 video 服务读取 FFmpeg 路径的调用方。

    真正的解析逻辑已经抽到 `app.utils.utils.get_ffmpeg_binary()`，视频、语音
    和后续新增链路都应复用同一套优先级；这里保留薄包装，避免外部脚本或
    旧测试直接导入 `app.services.video.get_ffmpeg_binary` 时出现 AttributeError。
    """
    return utils.get_ffmpeg_binary()


def _get_configured_video_codec() -> str:
    """
    读取用户配置的视频编码器。

    该配置面向高级用户，用于尝试启用 NVENC/AMF/QSV/VideoToolbox 等硬件
    编码。这里刻意只允许固定白名单，避免开放任意 FFmpeg 参数后，用户填错
    参数导致输出格式不可控，甚至让生成任务在后续阶段才失败。
    """
    configured_codec = str(
        config.app.get("video_codec", _DEFAULT_VIDEO_CODEC) or _DEFAULT_VIDEO_CODEC
    ).strip()
    if configured_codec not in _SUPPORTED_VIDEO_CODECS:
        logger.warning(
            f"unsupported video codec configured: {configured_codec}, "
            f"fallback to {_DEFAULT_VIDEO_CODEC}"
        )
        return _DEFAULT_VIDEO_CODEC
    return configured_codec


@lru_cache(maxsize=16)
def _ffmpeg_encoder_exists(ffmpeg_binary: str, codec: str) -> bool:
    """
    检查当前 FFmpeg 是否声明支持指定编码器。

    这只能证明 FFmpeg 编译时包含该 encoder，不能证明当前机器硬件和驱动
    一定可用。因此实际编码失败时仍会再回退到 libx264。
    """
    try:
        result = subprocess.run(
            [ffmpeg_binary, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning(
            "failed to inspect ffmpeg encoders, "
            f"fallback to {_DEFAULT_VIDEO_CODEC}: {str(exc)}"
        )
        return False

    if result.returncode != 0:
        logger.warning(
            "failed to inspect ffmpeg encoders, "
            f"fallback to {_DEFAULT_VIDEO_CODEC}: {(result.stderr or result.stdout or '').strip()}"
        )
        return False
    return codec in result.stdout


def _get_effective_video_codec(preferred_codec: str | None = None) -> str:
    """
    返回本次实际使用的视频编码器。

    用户选择硬件编码器时，先做 FFmpeg encoder 列表检测；如果本进程里已经
    实际编码失败过，也直接回退，避免一个任务里每个片段都重复失败。
    """
    selected_codec = preferred_codec or _get_configured_video_codec()
    if selected_codec == _DEFAULT_VIDEO_CODEC:
        return _DEFAULT_VIDEO_CODEC

    if selected_codec in _runtime_disabled_video_codecs:
        logger.warning(
            f"video codec {selected_codec} was disabled after a runtime failure, "
            f"fallback to {_DEFAULT_VIDEO_CODEC}"
        )
        return _DEFAULT_VIDEO_CODEC

    ffmpeg_binary = utils.get_ffmpeg_binary()
    if not _ffmpeg_encoder_exists(ffmpeg_binary, selected_codec):
        logger.warning(
            f"ffmpeg encoder {selected_codec} is not available, "
            f"fallback to {_DEFAULT_VIDEO_CODEC}"
        )
        return _DEFAULT_VIDEO_CODEC

    return selected_codec


def _disable_runtime_video_codec(codec: str, reason: str):
    if codec == _DEFAULT_VIDEO_CODEC:
        return
    _runtime_disabled_video_codecs.add(codec)
    logger.warning(
        f"video codec {codec} failed, fallback to {_DEFAULT_VIDEO_CODEC}. "
        f"reason: {reason}"
    )


def _get_temp_audio_dir(output_dir: str) -> str:
    """
    Return the directory to use for MoviePy's temporary audio file.

    On Windows, Windows Defender can lock files written to the task output
    directory while scanning them, causing MoviePy to fail with a
    PermissionError (WinError 32) on the TEMP_MPY_wvf_snd temp file and
    leaving the final MP4 at 0 bytes.  Using the system temp directory
    sidesteps the scan without changing behaviour on other platforms.

    On Linux/macOS/Docker the output directory is returned unchanged so
    existing behaviour is preserved.
    """
    if sys.platform == "win32":
        return tempfile.gettempdir()
    return output_dir


def _fallback_write_videofile(clip, output_file: str, failed_codec: str, reason: str, **kwargs):
    """
    硬件编码失败后用 libx264 重试，只有重试成功才禁用该硬件编码器。

    Windows 上 FFmpeg 失败原因比较复杂：可能是显卡/驱动不支持，也可能是输出
    文件被占用、目录权限、杀软拦截等通用 IO 问题。只有 libx264 能成功写出时，
    才能判断原始失败大概率来自硬件编码器本身，避免误伤后续任务。
    """
    clip.write_videofile(output_file, codec=_DEFAULT_VIDEO_CODEC, **kwargs)
    _disable_runtime_video_codec(failed_codec, reason)
    return _DEFAULT_VIDEO_CODEC


def _write_videofile_with_codec_fallback(clip, output_file: str, codec: str, **kwargs):
    """
    使用指定编码器写出视频，失败时自动用 libx264 重试一次。

    硬件编码器是否可用不仅取决于 FFmpeg，还取决于显卡、驱动和当前运行环境。
    生成任务不能因为高级编码器不可用而整体失败，所以这里把回退集中处理。
    """
    effective_codec = _get_effective_video_codec(codec)
    try:
        clip.write_videofile(output_file, codec=effective_codec, **kwargs)
        return effective_codec
    except Exception as exc:
        if effective_codec == _DEFAULT_VIDEO_CODEC:
            raise
        return _fallback_write_videofile(
            clip,
            output_file,
            failed_codec=effective_codec,
            reason=str(exc),
            **kwargs,
        )


def _run_ffmpeg_video_command_with_codec_fallback(build_command):
    effective_codec = _get_effective_video_codec()

    def run(codec: str):
        command = build_command(codec)
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            error_message = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(error_message or "ffmpeg video polish failed")
        return codec

    try:
        try:
            return run(effective_codec)
        except Exception as exc:
            if effective_codec == _DEFAULT_VIDEO_CODEC:
                raise
            result_codec = run(_DEFAULT_VIDEO_CODEC)
            _disable_runtime_video_codec(effective_codec, str(exc))
            return result_codec
    except Exception:
        raise


def _ffmpeg_seconds(value: float) -> str:
    return f"{max(0.0, float(value)):.3f}"


def _write_polished_clip_with_ffmpeg(
    *,
    source_file: str,
    output_file: str,
    source_start: float,
    source_duration: float,
    used_duration: float,
    clip_speed: float,
    geometry: _ScaledContentGeometry,
    feather_applied: bool,
    fade_in: bool,
    fade_out: bool,
    threads: int,
):
    overlay_path = (
        _video_edge_feather_overlay_path(geometry=geometry)
        if feather_applied
        else ""
    )

    def build_filter() -> str:
        speed = max(0.01, float(clip_speed))
        setpts = f"setpts=(PTS-STARTPTS)/{speed:.6f}"
        base_chain = (
            f"[0:v]{setpts},"
            f"scale={geometry.scaled_width}:{geometry.scaled_height}:flags=lanczos,"
            f"pad={geometry.canvas_width}:{geometry.canvas_height}:"
            f"{geometry.content_left}:{geometry.content_top}:black,"
            f"setsar=1[base]"
        )
        if overlay_path:
            overlay_chain = "[base][1:v]overlay=0:0:format=auto[polished]"
            input_label = "[polished]"
        else:
            overlay_chain = ""
            input_label = "[base]"

        effect_filters = []
        if fade_in:
            effect_filters.append(
                f"fade=t=in:st=0:d={_ffmpeg_seconds(_VIDEO_TRANSITION_DURATION)}:color=black"
            )
        if fade_out:
            fade_start = max(0.0, used_duration - _VIDEO_TRANSITION_DURATION)
            effect_filters.append(
                f"fade=t=out:st={_ffmpeg_seconds(fade_start)}:"
                f"d={_ffmpeg_seconds(_VIDEO_TRANSITION_DURATION)}:color=black"
            )
        effect_filters.extend([f"fps={fps}", "format=yuv420p"])
        effect_chain = f"{input_label}{','.join(effect_filters)}[v]"
        return ";".join(part for part in (base_chain, overlay_chain, effect_chain) if part)

    def build_command(codec: str) -> list[str]:
        command = [
            utils.get_ffmpeg_binary(),
            "-y",
            "-ss",
            _ffmpeg_seconds(source_start),
            "-t",
            _ffmpeg_seconds(source_duration),
            "-i",
            source_file,
        ]
        if overlay_path:
            command.extend([
                "-loop",
                "1",
                "-t",
                _ffmpeg_seconds(used_duration),
                "-i",
                overlay_path,
            ])
        command.extend([
            "-filter_complex",
            build_filter(),
            "-map",
            "[v]",
            "-an",
            "-t",
            _ffmpeg_seconds(used_duration),
            "-c:v",
            codec,
            "-threads",
            str(threads or 2),
            "-pix_fmt",
            "yuv420p",
            output_file,
        ])
        return command

    return _run_ffmpeg_video_command_with_codec_fallback(build_command)


def _escape_ffmpeg_concat_path(file_path: str) -> str:
    # concat demuxer 使用单引号包裹路径，路径中的单引号需要先转义。
    return file_path.replace("'", "'\\''")


def _format_ffmpeg_concat_path(file_path: str) -> str:
    """
    生成 concat demuxer 文件列表中的路径。

    FFmpeg 官方文档要求 concat list 中的特殊字符和空格需要转义；Windows
    绝对路径里的反斜杠也容易被解析成转义字符。这里统一转成正斜杠形式，
    让 `C:\\Users\\...` 变成 `C:/Users/...`，再处理单引号，兼容 macOS/Linux。
    """
    absolute_path = os.path.abspath(file_path)
    return _escape_ffmpeg_concat_path(absolute_path.replace("\\", "/"))


def concat_video_clips_with_ffmpeg(
    clip_files: List[str],
    output_file: str,
    threads: int,
    output_dir: str,
    max_duration: float | None = None,
):
    concat_list_file = os.path.join(output_dir, "ffmpeg-concat-list.txt")
    with open(concat_list_file, "w", encoding="utf-8") as fp:
        for clip_file in clip_files:
            fp.write(f"file '{_format_ffmpeg_concat_path(clip_file)}'\n")

    def build_command(codec: str) -> list[str]:
        command = [
            utils.get_ffmpeg_binary(),
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            concat_list_file,
            "-c:v",
            codec,
            "-threads",
            str(threads or 2),
            "-pix_fmt",
            "yuv420p",
        ]
        if max_duration is not None and max_duration > 0:
            command.extend(["-t", f"{max_duration:.3f}"])
        command.append(output_file)
        return command

    def run_concat(codec: str):
        command = build_command(codec)
        # 使用 ffmpeg 只做一次串联与编码，避免 MoviePy 逐段合并时反复重编码，
        # 从而降低画质劣化与颜色偏移风险。
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            error_message = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(error_message or "ffmpeg concat failed")
        return codec

    try:
        effective_codec = _get_effective_video_codec()
        try:
            return run_concat(effective_codec)
        except Exception as exc:
            if effective_codec == _DEFAULT_VIDEO_CODEC:
                raise
            result_codec = run_concat(_DEFAULT_VIDEO_CODEC)
            _disable_runtime_video_codec(effective_codec, str(exc))
            return result_codec
    finally:
        delete_files(concat_list_file)


def _sanitize_image_file(image_path: str) -> str:
    # 某些本地图片虽然能被 Pillow 打开，但会因为损坏的 EXIF/eXIf 元数据导致
    # ImageClip 在解析阶段直接抛异常。这里重新导出一份“干净图片”，把坏元数据剥离掉。
    image_root, _ = os.path.splitext(image_path)
    sanitized_path = f"{image_root}.sanitized.png"

    with Image.open(image_path) as image:
        image.load()
        # 统一导出为 PNG，避免 JPEG/PNG 不同元数据路径继续把坏块带过去。
        cleaned_image = Image.new(image.mode, image.size)
        cleaned_image.putdata(list(image.getdata()))
        cleaned_image.save(sanitized_path)

    return sanitized_path


def _open_image_clip_with_fallback(image_path: str):
    # 优先直接打开原始图片；如果因为损坏元数据失败，再尝试生成无元数据副本。
    try:
        return ImageClip(image_path), image_path
    except Exception as exc:
        logger.warning(
            f"failed to open image directly, trying sanitized copy: {image_path}, error: {str(exc)}"
        )
        sanitized_path = _sanitize_image_file(image_path)
        return ImageClip(sanitized_path), sanitized_path


def _open_video_clip_quietly(video_path: str, audio: bool = False) -> VideoFileClip:
    """
    安静地打开视频文件，避免 MoviePy 2.1.x 把 ffmpeg 探测信息直接打印到 stdout。

    背景：
    当前依赖版本的 `FFMPEG_VideoReader` 内部存在 `print(self.infos)` 和
    `print(ffmpeg command)`，读取无音轨的中间视频时会输出
    `audio_found: False`。这只是输入素材 metadata，不代表最终成片没有音频，
    但会误导 WebUI/终端用户以为生成失败。

    实现：
    1. 只在打开 VideoFileClip 的短窗口内重定向 stdout；
    2. 默认 `audio=False`，因为项目视频素材阶段不需要保留素材原声，
       最终音频会在 `generate_video()` 阶段统一挂载；
    3. 如果依赖库确实输出了内容，降级为 debug 日志，便于必要时排查。
    """
    captured_stdout = io.StringIO()
    with redirect_stdout(captured_stdout):
        clip = VideoFileClip(video_path, audio=audio)

    moviepy_stdout = captured_stdout.getvalue().strip()
    if moviepy_stdout:
        logger.debug(
            "suppressed MoviePy video reader stdout for "
            f"{video_path}, chars: {len(moviepy_stdout)}"
        )

    return clip


def _sanitize_video_file_for_moviepy(video_path: str) -> str:
    sanitized_dir = utils.storage_dir("temp/sanitized_videos", create=True)
    descriptor, sanitized_path = tempfile.mkstemp(
        prefix="moviepy-sanitized-",
        suffix=".mp4",
        dir=sanitized_dir,
    )
    os.close(descriptor)

    sanitized_realpath = os.path.realpath(sanitized_path)
    sanitized_root = os.path.realpath(sanitized_dir)
    if not sanitized_realpath.startswith(sanitized_root + os.sep):
        raise ValueError(f"sanitized video path escaped temp directory: {sanitized_path}")
    if sanitized_realpath.startswith(os.path.realpath("/data/job-assets") + os.sep):
        raise ValueError(f"sanitized video path must not be under /data/job-assets: {sanitized_path}")

    command = [
        utils.get_ffmpeg_binary(),
        "-y",
        "-i",
        video_path,
        "-map",
        "0:v:0",
        "-an",
        "-sn",
        "-dn",
        "-map_chapters",
        "-1",
        "-map_metadata",
        "-1",
        "-c:v",
        "copy",
        "-movflags",
        "+faststart",
        sanitized_path,
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        delete_files(sanitized_path)
        error_message = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(error_message or "ffmpeg sanitized remux failed")

    return sanitized_path


def _open_video_clip_with_sanitized_fallback(
    video_path: str,
    audio: bool = False,
) -> tuple[VideoFileClip, str]:
    try:
        return _open_video_clip_quietly(video_path, audio=audio), video_path
    except Exception as first_exc:
        logger.warning(
            "failed to open video directly, trying sanitized remux: "
            f"{video_path}, error: {str(first_exc)}"
        )
        sanitized_path = _sanitize_video_file_for_moviepy(video_path)
        return _open_video_clip_quietly(sanitized_path, audio=audio), sanitized_path


def close_clip(clip):
    if clip is None:
        return
        
    try:
        # close main resources
        if hasattr(clip, 'reader') and clip.reader is not None:
            clip.reader.close()
            
        # close audio resources
        if hasattr(clip, 'audio') and clip.audio is not None:
            if hasattr(clip.audio, 'reader') and clip.audio.reader is not None:
                clip.audio.reader.close()
            del clip.audio
            
        # close mask resources
        if hasattr(clip, 'mask') and clip.mask is not None:
            if hasattr(clip.mask, 'reader') and clip.mask.reader is not None:
                clip.mask.reader.close()
            del clip.mask
            
        # handle child clips in composite clips
        if hasattr(clip, 'clips') and clip.clips:
            for child_clip in clip.clips:
                if child_clip is not clip:  # avoid possible circular references
                    close_clip(child_clip)
            
        # clear clip list
        if hasattr(clip, 'clips'):
            clip.clips = []
            
    except Exception as e:
        logger.error(f"failed to close clip: {str(e)}")
    
    del clip
    gc.collect()

def delete_files(files: List[str] | str):
    if isinstance(files, str):
        files = [files]

    # 循环补足视频时，同一个临时片段路径会在 FFmpeg 拼接列表中出现多次。
    # 拼接必须保留重复项，但清理只能删除一次；这里按原顺序统一去重，让所有
    # 调用方都获得幂等行为，也避免首次删除成功后连续输出 FileNotFoundError。
    unique_files = dict.fromkeys(file for file in files if file)
    for file in unique_files:
        try:
            os.remove(file)
        except FileNotFoundError:
            # 清理动作允许文件已经不存在，例如 FFmpeg 失败路径或并发清理已经
            # 回收文件；这不是需要用户处理的问题，不应污染生成日志。
            continue
        except OSError as e:
            # 权限、只读文件系统或磁盘异常会留下真实临时文件，保留 warning
            # 便于根据具体路径和系统错误定位环境问题。
            logger.warning(f"failed to delete temporary file {file}: {str(e)}")


def get_bgm_file(bgm_type: str = "random", bgm_file: str = ""):
    if not bgm_type:
        return ""

    if bgm_file:
        try:
            resolved_bgm_file = bgm_service.resolve_bgm_file(bgm_file)
        except ValueError as exc:
            # API 请求里的 bgm_file 来自用户输入，只允许解析到用户 BGM 或内置
            # 歌曲目录，阻止 MoviePy 读取配置、密钥等任意服务器文件。
            logger.warning(
                f"reject unsafe bgm file: {bgm_file}, error: {str(exc)}"
            )
            return ""
        return resolved_bgm_file

    if bgm_type == "random":
        files = bgm_service.list_bgm_files()
        # 当背景音乐目录为空时，直接回退为“不使用 BGM”，避免 random.choice([]) 抛异常。
        if not files:
            logger.warning("no background music files found")
            return ""
        return random.choice(files)

    return ""


def combine_videos(
    combined_video_path: str,
    video_paths: List[str],
    audio_file: str,
    video_aspect: VideoAspect = VideoAspect.portrait,
    video_concat_mode: VideoConcatMode = VideoConcatMode.random,
    video_transition_mode: VideoTransitionMode = None,
    max_clip_duration: int = 5,
    threads: int = 2,
    clip_speed: float = 1.0,
    video_resolution: str = "",
) -> str:
    audio_clip = AudioFileClip(audio_file)
    try:
        # 这里只需要读取旁白音频时长来决定素材视频拼接长度；后续不会再使用
        # audio_clip。读取完成后立即关闭，避免早退或异常路径泄漏文件句柄。
        audio_duration = audio_clip.duration
    finally:
        close_clip(audio_clip)
    logger.info(f"audio duration: {audio_duration} seconds")
    logger.info(f"maximum clip duration: {max_clip_duration} seconds")
    required_video_duration = _get_required_video_duration(audio_duration)
    logger.info(
        f"required video duration: {required_video_duration:.2f} seconds "
        f"(audio duration + {_VIDEO_DURATION_SAFETY_MARGIN:.2f}s safety margin)"
    )

    # 兼容 API 直接调用时未传转场模式的情况，避免后续访问 .value 时崩溃。
    transition_value = getattr(video_transition_mode, "value", video_transition_mode)
    normalized_clip_speed = utils.normalize_clip_speed(clip_speed)
    if normalized_clip_speed != 1.0:
        # 只记录一次最终生效值，既方便定位 API 越界参数被归一化的问题，
        # 也避免在逐片段热路径中重复输出相同日志。
        logger.info(f"clip playback speed: {normalized_clip_speed:.2f}x")
    # max_clip_duration 约束的是成片里的最终播放时长，而不是源视频读取时长。
    # MoviePy 以 0.5 倍速播放 1.5 秒源画面会得到 3 秒片段，以 2 倍速播放
    # 6 秒源画面同样会得到 3 秒片段。因此切片前必须按速度反推源时长；如果
    # 仍固定读取 3 秒再慢放、裁剪，下一段却从源视频第 3 秒开始，会跳过中间
    # 1.5 秒画面。该计算同时保证不同速度下的源时间线连续且无重叠。
    source_clip_duration = max_clip_duration * normalized_clip_speed
    output_dir = os.path.dirname(combined_video_path)

    aspect = VideoAspect(video_aspect)
    video_width, video_height = resolve_video_size(video_aspect, video_resolution)

    processed_clips = []
    subclipped_items = []
    video_duration = 0
    for video_path in video_paths:
        clip = _open_video_clip_quietly(video_path)
        clip_duration = clip.duration
        clip_w, clip_h = clip.size
        close_clip(clip)
        
        start_time = 0

        while start_time < clip_duration:
            end_time = min(start_time + source_clip_duration, clip_duration)

            # 保留所有有效分段。
            # 这样既不会丢掉“整段视频本身就短于 max_clip_duration”的素材，
            # 也不会吞掉长视频最后剩下的一小段尾部内容。
            if end_time > start_time:
                subclipped_items.append(
                    SubClippedVideoClip(
                        file_path=video_path,
                        start_time=start_time,
                        end_time=end_time,
                        width=clip_w,
                        height=clip_h,
                        source_file_path=video_path,
                    )
                )

            start_time = end_time
            if video_concat_mode.value == VideoConcatMode.sequential.value:
                break

    subclipped_items = _prioritize_unique_source_clips(
        subclipped_items=subclipped_items,
        concat_mode=video_concat_mode,
    )
        
    logger.debug(f"total subclipped items: {len(subclipped_items)}")
    
    # Add downloaded clips over and over until the duration of the audio (max_duration) has been reached
    for i, subclipped_item in enumerate(subclipped_items):
        if video_duration >= required_video_duration:
            break

        remaining_duration = max(0.0, required_video_duration - video_duration)
        source_available_duration = max(
            0.0,
            float(subclipped_item.end_time or 0) - float(subclipped_item.start_time or 0),
        )
        used_duration = min(
            float(max_clip_duration),
            remaining_duration,
            source_available_duration / normalized_clip_speed,
        )
        if used_duration <= 0:
            continue
        source_used_duration = used_duration * normalized_clip_speed
        geometry = _scaled_content_geometry(
            source_size=(int(subclipped_item.width or 0), int(subclipped_item.height or 0)),
            canvas_size=(video_width, video_height),
        )
        feather_applied = (
            _VIDEO_EDGE_FEATHER_ENABLED
            and _geometry_has_vertical_letterbox(geometry)
        )
        fade_in, fade_out = _dip_to_black_flags(
            clip_index=i,
            clip_count=len(subclipped_items),
            selected_count=len(processed_clips),
            current_duration=video_duration,
            clip_duration=used_duration,
            required_duration=required_video_duration,
        )
        transition_is_legacy = transition_value not in (None, VideoTransitionMode.none.value)
        backend = "moviepy" if transition_is_legacy else "ffmpeg"
        logger.debug(
            "video polish: "
            f"source={source_available_duration:.2f} "
            f"used={used_duration:.2f} "
            f"remaining={remaining_duration:.2f} "
            f"bounds={geometry.content_top}:{geometry.content_bottom} "
            f"feather={str(feather_applied).lower()} "
            f"fade_in={str(fade_in).lower()} "
            f"fade_out={str(fade_out).lower()} "
            f"backend={backend}"
        )
        
        try:
            # wirte clip to temp file
            clip_file = f"{output_dir}/temp-clip-{i+1}.mp4"
            if not transition_is_legacy:
                _write_polished_clip_with_ffmpeg(
                    source_file=subclipped_item.file_path,
                    output_file=clip_file,
                    source_start=float(subclipped_item.start_time or 0),
                    source_duration=source_used_duration,
                    used_duration=used_duration,
                    clip_speed=normalized_clip_speed,
                    geometry=geometry,
                    feather_applied=feather_applied,
                    fade_in=fade_in,
                    fade_out=fade_out,
                    threads=threads,
                )
                clip_duration_saved = used_duration
            else:
                clip = _open_video_clip_quietly(subclipped_item.file_path).subclipped(
                    subclipped_item.start_time,
                    float(subclipped_item.start_time or 0) + source_used_duration,
                )
                # 播放速度属于素材本身属性，应在转场前应用。这样 Fade/Slide 等一秒转场
                # 不会跟随素材速度变成 0.5 秒或 2 秒；后续最大时长裁剪继续作为
                # 浮点误差或异常素材时长的安全兜底，保证最终片段不突破配置上限。
                if normalized_clip_speed != 1.0:
                    clip = clip.with_speed_scaled(normalized_clip_speed)
                if clip.duration > used_duration:
                    clip = clip.subclipped(0, used_duration)
                # Not all videos are same size, so we need to resize them
                if geometry.content_size == geometry.canvas_size:
                    if clip.size != [video_width, video_height] and clip.size != (video_width, video_height):
                        clip = clip.resized(new_size=(video_width, video_height))
                else:
                    background = ColorClip(size=(video_width, video_height), color=(0, 0, 0)).with_duration(clip.duration)
                    clip_resized = clip.resized(new_size=geometry.content_size).with_position(
                        (geometry.content_left, geometry.content_top)
                    )
                    clip = CompositeVideoClip([background, clip_resized], size=(video_width, video_height)).with_duration(clip.duration)
                    clip = _apply_vertical_letterbox_feather(clip, geometry=geometry)

                shuffle_side = random.choice(["left", "right", "top", "bottom"])
                if transition_value == VideoTransitionMode.fade_in.value:
                    clip = video_effects.fadein_transition(clip, 1)
                elif transition_value == VideoTransitionMode.fade_out.value:
                    clip = video_effects.fadeout_transition(clip, 1)
                elif transition_value == VideoTransitionMode.slide_in.value:
                    clip = video_effects.slidein_transition(clip, 1, shuffle_side)
                elif transition_value == VideoTransitionMode.slide_out.value:
                    clip = video_effects.slideout_transition(clip, 1, shuffle_side)
                elif transition_value == VideoTransitionMode.zoom_in.value:
                    clip = video_effects.zoomin_transition(clip, 1)
                elif transition_value == VideoTransitionMode.zoom_out.value:
                    clip = video_effects.zoomout_transition(clip, 1)
                elif transition_value == VideoTransitionMode.shuffle.value:
                    transition_funcs = [
                        lambda c: video_effects.fadein_transition(c, 1),
                        lambda c: video_effects.fadeout_transition(c, 1),
                        lambda c: video_effects.slidein_transition(c, 1, shuffle_side),
                        lambda c: video_effects.slideout_transition(c, 1, shuffle_side),
                        lambda c: video_effects.zoomin_transition(c, 1),
                        lambda c: video_effects.zoomout_transition(c, 1),
                    ]
                    shuffle_transition = random.choice(transition_funcs)
                    clip = shuffle_transition(clip)

                if clip.duration > used_duration:
                    clip = clip.subclipped(0, used_duration)
                clip = _apply_dip_to_black_transition(
                    clip,
                    fade_in=fade_in,
                    fade_out=fade_out,
                )
                _write_videofile_with_codec_fallback(
                    clip,
                    clip_file,
                    codec=_get_configured_video_codec(),
                    logger=None,
                    fps=fps,
                )

                # Store clip duration before closing
                clip_duration_saved = clip.duration
                close_clip(clip)

            processed_clips.append(
                SubClippedVideoClip(
                    file_path=clip_file,
                    duration=clip_duration_saved,
                    width=geometry.canvas_width,
                    height=geometry.canvas_height,
                    source_file_path=subclipped_item.source_file_path,
                )
            )
            video_duration += clip_duration_saved
            
        except Exception as e:
            logger.error(f"failed to process clip: {str(e)}")
    
    # loop processed clips until the video duration covers the audio duration and the small safety margin.
    if video_duration < required_video_duration:
        logger.warning(
            f"video duration ({video_duration:.2f}s) is shorter than required duration "
            f"({required_video_duration:.2f}s), looping clips to match audio length."
        )
        base_clips = processed_clips.copy()
        for clip in itertools.cycle(base_clips):
            if video_duration >= required_video_duration:
                break
            processed_clips.append(clip)
            video_duration += clip.duration
        logger.info(
            f"video duration: {video_duration:.2f}s, audio duration: {audio_duration:.2f}s, "
            f"required duration: {required_video_duration:.2f}s, "
            f"looped {len(processed_clips)-len(base_clips)} clips"
        )
     
    # merge video clips progressively, avoid loading all videos at once to avoid memory overflow
    logger.info("starting clip merging process")
    if not processed_clips:
        logger.warning("no clips available for merging")
        return combined_video_path
    
    clip_files = [clip.file_path for clip in processed_clips]
    logger.info(f"concatenating {len(clip_files)} clips with ffmpeg")
    concat_video_clips_with_ffmpeg(
        clip_files=clip_files,
        output_file=combined_video_path,
        threads=threads,
        output_dir=output_dir,
        max_duration=audio_duration,
    )
    
    # clean temp files
    delete_files(clip_files)
            
    logger.info("video combining completed")
    return combined_video_path


def wrap_text(text, max_width, font="Arial", fontsize=60):
    # 字幕换行必须在真正创建 TextClip 前完成，否则 MoviePy 只会按原始文本
    # 计算渲染区域。这里用 PIL 按当前字体和字号测量宽度，确保每一行都尽量
    # 控制在视频可用宽度内，避免大字号或中文长句直接溢出画面。
    font = ImageFont.truetype(font, fontsize)
    max_width = int(max_width)

    def get_text_size(inner_text):
        inner_text = inner_text.strip()
        if not inner_text:
            return 0, fontsize
        left, top, right, bottom = font.getbbox(inner_text)
        return right - left, bottom - top

    width, height = get_text_size(text)
    if width <= max_width:
        return text, height

    def split_long_token(token):
        # 当一个 token 本身就超宽时（常见于中文无空格长句，或英文超长单词），
        # 退化为字符级拆分。关键点是：检测到 candidate 超宽时，先提交上一个
        # 仍然合法的 current，再把当前字符放入下一行，不能把超宽字符塞回上一行。
        lines = []
        current = ""
        for char in token:
            candidate = f"{current}{char}"
            candidate_width, _ = get_text_size(candidate)
            if candidate_width <= max_width or not current:
                current = candidate
                continue
            lines.append(current)
            current = char
        if current:
            lines.append(current)
        return lines

    lines = []
    current = ""
    words = text.split(" ")
    for word in words:
        candidate = f"{current} {word}".strip() if current else word
        candidate_width, _ = get_text_size(candidate)
        if candidate_width <= max_width:
            current = candidate
            continue

        if current:
            lines.append(current)

        word_width, _ = get_text_size(word)
        if word_width <= max_width:
            current = word
        else:
            lines.extend(split_long_token(word))
            current = ""

    if current:
        lines.append(current)

    line_start_punctuation = "，。！？；：、,.!?;:)]}）】》」』”’"
    for index in range(1, len(lines)):
        # 中文长句按字符拆分时，最后一个句号、逗号等闭合标点可能被单独
        # 放到下一行，导致字幕背景被异常撑高，视觉上像一个小点掉在正文
        # 下方。这里在不重新设计换行算法的前提下，把上一行最后一个字
        # 移到标点行前面，让标点跟随文字显示，兼容中英文常见闭合标点。
        if not lines[index] or lines[index][0] not in line_start_punctuation:
            continue
        if len(lines[index - 1]) <= 1:
            continue

        candidate = f"{lines[index - 1][-1]}{lines[index]}"
        candidate_width, _ = get_text_size(candidate)
        if candidate_width <= max_width:
            lines[index] = candidate
            lines[index - 1] = lines[index - 1][:-1]

    result = "\n".join(line.strip() for line in lines if line.strip()).strip()
    height = len(lines) * height
    return result, height


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    # 字幕背景色来自 API/WebUI 参数，可能为空或格式不规范。这里统一只接受
    # #RRGGBB 形式，非法值回退为黑色，避免 PIL 渲染阶段抛出异常中断任务。
    if isinstance(color, str) and color.startswith("#") and len(color) == 7:
        try:
            return (int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16))
        except ValueError:
            pass
    return (0, 0, 0)


def _rounded_subtitle_background_clip(
    width: int,
    height: int,
    color: str,
    alpha: int = 140,
    radius: int = 16,
) -> ImageClip:
    # 新字幕背景仅在用户显式开启时使用：通过 RGBA 图片绘制圆角半透明底板，
    # 再交给 MoviePy 作为透明 ImageClip 参与合成。这样默认路径完全不变，
    # 同时可以低成本试验更柔和的字幕视觉效果。
    rgb = _hex_to_rgb(color)
    safe_alpha = max(0, min(255, int(alpha)))
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle(
        [0, 0, max(0, width - 1), max(0, height - 1)],
        radius=max(0, int(radius)),
        fill=(rgb[0], rgb[1], rgb[2], safe_alpha),
    )
    return ImageClip(np.array(img), transparent=True)


def _get_visible_center_position(
    text_clip: TextClip,
    container_width: int,
    container_height: int,
) -> tuple[int, int]:
    """
    按文字真实可见像素把 TextClip 放到背景容器中心。

    MoviePy 的 TextClip 会按字体行高和 baseline 创建透明画布。很多字体的
    可见字形并不在这个画布的几何中心，直接 `with_position("center")`
    会把整块透明画布居中，导致字幕看起来偏上或偏下。这里读取 TextClip
    的透明 mask，只根据实际有像素的 bbox 计算偏移，让用户看到的文字
    在字幕背景里视觉居中。
    """
    x = int(round((container_width - text_clip.w) / 2))
    y = int(round((container_height - text_clip.h) / 2))

    try:
        if text_clip.mask is None:
            return x, y

        mask_frame = text_clip.mask.get_frame(0)
        ys, _ = np.where(mask_frame > 0.01)
        if len(ys) == 0:
            return x, y

        visible_top = int(ys.min())
        visible_bottom = int(ys.max())
        visible_height = visible_bottom - visible_top + 1
        y = int(round((container_height - visible_height) / 2 - visible_top))
    except Exception as exc:
        logger.debug(f"failed to center subtitle text by visible mask: {str(exc)}")

    return x, y


def subtitle_colors_are_indistinguishable(params: VideoParams) -> bool:
    """判断字幕文字和背景是否同色，提醒用户可能无法看清字幕。"""
    if not params.subtitle_enabled or not params.text_background_color:
        return False

    def normalize_color(value):
        if isinstance(value, bool):
            return "#000000" if value else ""
        return str(value or "").strip().lower()

    text_color = normalize_color(params.text_fore_color)
    background_color = normalize_color(params.text_background_color)
    return bool(text_color and text_color == background_color)


@lru_cache(maxsize=64)
def _subtitle_font_supports_sample(font_path: str, sample: str) -> bool:
    """检查字体是否包含样本文字需要的字形，并缓存重复检查结果。"""
    try:
        font = ImageFont.truetype(font_path, 30)
        missing_mask = font.getmask("\U0010ffff")
        missing_signature = (
            missing_mask.size,
            missing_mask.getbbox(),
            bytes(missing_mask),
        )
        for char in sample:
            char_mask = font.getmask(char)
            char_signature = (
                char_mask.size,
                char_mask.getbbox(),
                bytes(char_mask),
            )
            if char_mask.getbbox() is None or char_signature == missing_signature:
                return False
        return True
    except Exception as e:
        # 字体探测失败不应阻止用户生成；保留日志供环境兼容问题排查。
        logger.warning(f"failed to inspect subtitle font glyphs: {font_path}, {e}")
        return True


def subtitle_font_supports_text(font_path: str, text: str) -> bool:
    """检查字体能否绘制文本中的字母和数字，忽略空白及标点符号。"""
    sample = "".join(
        dict.fromkeys(
            char
            for char in str(text or "")
            if unicodedata.category(char)[0] in {"L", "N"}
        )
    )[:64]
    if not sample:
        return True
    return _subtitle_font_supports_sample(font_path, sample)


def generate_video(
    video_path: str,
    audio_path: str,
    subtitle_path: str,
    output_file: str,
    params: VideoParams,
    bgm_file_override: str | None = None,
) -> bool:
    """
    合成最终视频，并返回本次背景音乐处理是否成功。

    返回值只描述 BGM 处理状态：没有请求 BGM 或成功混合时返回 True；请求了
    BGM 但加载、特效或混合失败时返回 False。即使 BGM 失败仍会继续输出只有
    旁白的视频，让任务编排层决定是否向用户展示降级警告。
    """
    video_width, video_height = resolve_video_size(
        params.video_aspect, getattr(params, "video_resolution", "")
    )

    logger.info(f"generating video: {video_width} x {video_height}")
    logger.info(f"  ① video: {video_path}")
    logger.info(f"  ② audio: {audio_path}")
    logger.info(f"  ③ subtitle: {subtitle_path}")
    logger.info(f"  ④ output: {output_file}")

    # https://github.com/harry0703/MoneyPrinterTurbo/issues/217
    # PermissionError: [WinError 32] The process cannot access the file because it is being used by another process: 'final-1.mp4.tempTEMP_MPY_wvf_snd.mp3'
    # write into the same directory as the output file
    output_dir = os.path.dirname(output_file)

    font_path = ""
    if params.subtitle_enabled:
        if not params.font_name:
            params.font_name = "STHeitiMedium.ttc"
        font_path = os.path.join(utils.font_dir(), params.font_name)
        if os.name == "nt":
            font_path = font_path.replace("\\", "/")

        logger.info(f"  ⑤ font: {font_path}")

    def resolve_subtitle_background_color():
        # 兼容历史参数：API 里 `text_background_color` 既可能是布尔值，
        # 也可能是实际颜色字符串。统一在这里归一化，避免把 True/False
        # 直接传给 TextClip 后出现不可预期的渲染结果。
        if isinstance(params.text_background_color, bool):
            return "#000000" if params.text_background_color else None
        return params.text_background_color

    def create_text_clip(subtitle_item):
        params.font_size = int(params.font_size)
        params.stroke_width = int(params.stroke_width)
        phrase = subtitle_item[1]
        max_width = video_width * 0.9
        bg_color = resolve_subtitle_background_color()
        rounded_bg_enabled = bool(
            getattr(params, "rounded_subtitle_background", False) and bg_color
        )
        has_subtitle_background = bool(bg_color)
        # 圆角背景按文字真实宽度生成，左右留白应更克制；旧矩形背景仍保留
        # 较大的安全边距，避免历史配置中的长字幕贴边或被裁切。
        padding_ratio = 0.4 if rounded_bg_enabled else 0.6
        pad_x = int(params.font_size * padding_ratio) if has_subtitle_background else 0
        # 字幕背景需要给文字左右留出明确内边距。先从可用宽度中扣除
        # padding 再换行，避免长英文或大字号刚好撑满 90% 视频宽度后，
        # 文字贴到背景框边缘，看起来像被裁切。普通矩形背景和圆角背景
        # 都走这条逻辑；无背景字幕则保持原有最大宽度。
        text_max_width = max(1, int(max_width) - 2 * pad_x)
        wrapped_txt, txt_height = wrap_text(
            phrase,
            max_width=text_max_width,
            font=font_path,
            fontsize=params.font_size,
        )
        interline = int(params.font_size * 0.25)
        line_count = wrapped_txt.count("\n") + 1
        vertical_padding = int(params.font_size * 0.35)
        text_clip_margin_y = max(
            int(params.font_size * 0.3), int(params.stroke_width * 2)
        )
        # MoviePy 在 `method=label` 下会自动收缩文本框高度，遇到多行字幕、
        # 描边或背景色时，容易把最后一行的下半部分裁掉。这里显式传入
        # 一个更保守的高度，把行间距和额外上下留白一并算进去，保证字幕
        # 背景框与文字本身都能完整渲染出来。
        clip_h = int(txt_height + vertical_padding + (interline * line_count))

        if rounded_bg_enabled:
            # 圆角背景需要贴合文字宽度，而不是沿用 90% 视频宽度。这里先用
            # PIL 测量最长一行文字，再加水平内边距，避免短字幕出现过宽底板。
            try:
                font = ImageFont.truetype(font_path, params.font_size)
                text_w = max(
                    int(font.getbbox(line)[2] - font.getbbox(line)[0])
                    for line in wrapped_txt.split("\n")
                )
            except Exception as exc:
                logger.warning(
                    f"failed to measure subtitle text width, fallback to max width: {str(exc)}"
                )
                text_w = int(max_width)

            box_w = max(1, min(int(max_width), text_w + 2 * pad_x))
            radius = max(8, int(params.font_size * 0.4))
            text_clip = TextClip(
                text=wrapped_txt,
                font=font_path,
                font_size=params.font_size,
                color=params.text_fore_color,
                bg_color=None,
                stroke_color=params.stroke_color,
                stroke_width=params.stroke_width,
                interline=interline,
                size=(box_w, None),
                text_align="center",
                margin=(0, text_clip_margin_y),
            )
            clip_h = max(clip_h, text_clip.h)
            bg_clip = _rounded_subtitle_background_clip(
                width=box_w,
                height=clip_h,
                color=bg_color,
                alpha=140,
                radius=radius,
            )
            text_position = _get_visible_center_position(text_clip, box_w, clip_h)
            _clip = CompositeVideoClip(
                [bg_clip, text_clip.with_position(text_position)],
                size=(box_w, clip_h),
            )
        elif bg_color:
            size = (
                int(max_width),
                clip_h,
            )
            text_clip = TextClip(
                text=wrapped_txt,
                font=font_path,
                font_size=params.font_size,
                color=params.text_fore_color,
                bg_color=None,
                stroke_color=params.stroke_color,
                stroke_width=params.stroke_width,
                interline=interline,
                size=(int(max_width), None),
                text_align="center",
                margin=(0, text_clip_margin_y),
            )
            size = (size[0], max(size[1], text_clip.h))
            bg_clip = _rounded_subtitle_background_clip(
                width=size[0],
                height=size[1],
                color=bg_color,
                alpha=255,
                radius=0,
            )
            text_position = _get_visible_center_position(text_clip, size[0], size[1])
            _clip = CompositeVideoClip(
                [bg_clip, text_clip.with_position(text_position)],
                size=size,
            )
        else:
            size = (
                int(max_width),
                clip_h,
            )
            _clip = TextClip(
                text=wrapped_txt,
                font=font_path,
                font_size=params.font_size,
                color=params.text_fore_color,
                bg_color=None,
                stroke_color=params.stroke_color,
                stroke_width=params.stroke_width,
                interline=interline,
                size=size,
                text_align="center",
            )
        duration = subtitle_item[0][1] - subtitle_item[0][0]
        _clip = _clip.with_start(subtitle_item[0][0])
        _clip = _clip.with_end(subtitle_item[0][1])
        _clip = _clip.with_duration(duration)
        if params.subtitle_position == "bottom":
            _clip = _clip.with_position(("center", video_height * 0.95 - _clip.h))
        elif params.subtitle_position == "top":
            _clip = _clip.with_position(("center", video_height * 0.05))
        elif params.subtitle_position == "custom":
            # Ensure the subtitle is fully within the screen bounds
            margin = 10  # Additional margin, in pixels
            max_y = video_height - _clip.h - margin
            min_y = margin
            custom_y = (video_height - _clip.h) * (params.custom_position / 100)
            custom_y = max(
                min_y, min(custom_y, max_y)
            )  # Constrain the y value within the valid range
            _clip = _clip.with_position(("center", custom_y))
        else:  # center
            _clip = _clip.with_position(("center", "center"))
        return _clip

    # MoviePy 的 CompositeAudioClip.close() 不会关闭子 AudioFileClip。这里用
    # ExitStack 显式持有所有原始文件 reader，确保成功、字幕异常、混音失败和
    # 视频写入失败等路径都能释放 FFmpeg 子进程，尤其避免 Windows 文件被占用。
    with ExitStack() as clip_stack:
        source_video_clip = clip_stack.enter_context(
            _open_video_clip_quietly(video_path)
        )
        voice_source_clip = clip_stack.enter_context(AudioFileClip(audio_path))
        video_clip = source_video_clip
        audio_clip = voice_source_clip.with_effects(
            [afx.MultiplyVolume(params.voice_volume)]
        )

        def make_textclip(text):
            return TextClip(
                text=text,
                font=font_path,
                font_size=params.font_size,
            )

        if subtitle_path and os.path.exists(subtitle_path):
            sub = clip_stack.enter_context(
                SubtitlesClip(
                    subtitles=subtitle_path,
                    encoding="utf-8",
                    make_textclip=make_textclip,
                )
            )
            text_clips = []
            for item in sub.subtitles:
                clip = create_text_clip(subtitle_item=item)
                text_clips.append(clip)
            video_clip = CompositeVideoClip([video_clip, *text_clips])
            clip_stack.callback(video_clip.close)

        bgm_enabled = bgm_service.should_use_bgm(
            params.bgm_type, params.bgm_volume
        )
        if not bgm_enabled and params.bgm_type:
            # 所有 BGM 来源共用这一条短路规则。音量不大于 0 时不能解析随机或
            # 自定义文件，也不能加载提供商返回的文件，避免无意义的 IO 和混音。
            logger.info(
                f"skipping background music because volume is not positive: "
                f"type={params.bgm_type}, volume={params.bgm_volume}"
            )

        # 提供商配乐可由任务编排层直接传入对应文件。None 表示沿用随机/自定义
        # BGM 解析，空字符串明确禁用本条 BGM；但任何来源都必须先通过通用音量规则。
        bgm_file = ""
        if bgm_enabled:
            bgm_file = (
                bgm_file_override
                if bgm_file_override is not None
                else get_bgm_file(
                    bgm_type=params.bgm_type,
                    bgm_file=params.bgm_file,
                )
            )
        bgm_mix_succeeded = True
        if bgm_file:
            try:
                bgm_effects = [
                    afx.MultiplyVolume(params.bgm_volume),
                    afx.AudioFadeOut(3),
                ]
                # 服务内解析的随机/自定义音乐可能比成片短，需要循环铺满；任务层
                # 通过 override 传入的文件表示提供商已经完成时长适配。这里依据
                # 文件来源决定是否循环，避免今后每增加一个提供商都修改名称白名单。
                if bgm_file_override is None:
                    bgm_effects.append(afx.AudioLoop(duration=video_clip.duration))
                bgm_source_clip = clip_stack.enter_context(AudioFileClip(bgm_file))
                bgm_clip = bgm_source_clip.with_effects(bgm_effects)
                audio_clip = CompositeAudioClip([audio_clip, bgm_clip])
            except Exception:
                bgm_mix_succeeded = False
                # 记录完整堆栈和稳定上下文，便于区分文件解码、MoviePy 特效和
                # CompositeAudioClip 失败；文件内容与 API Key 不会进入日志。
                logger.exception(
                    f"failed to mix background music: type={params.bgm_type}, "
                    f"file={bgm_file}"
                )

        final_video_clip = video_clip.with_audio(audio_clip)
        clip_stack.callback(final_video_clip.close)
        # 显式沿用输入音频的采样率；如果取不到，再回退 MoviePy 默认的 44100Hz。
        # 这样可以减少不同环境，尤其 Docker 中再次重采样带来的音质波动。
        output_audio_fps = int(getattr(audio_clip, "fps", 0) or 44100)
        _write_videofile_with_codec_fallback(
            final_video_clip,
            output_file=output_file,
            codec=_get_configured_video_codec(),
            audio_codec=audio_codec,
            audio_fps=output_audio_fps,
            audio_bitrate=audio_bitrate,
            temp_audiofile_path=_get_temp_audio_dir(output_dir),
            threads=params.n_threads or 2,
            logger=None,
            fps=fps,
        )
        return bgm_mix_succeeded


def preprocess_video(
    materials: List[MaterialInfo],
    clip_duration=4,
    video_aspect: str = VideoAspect.portrait.value,
    video_resolution: str = "",
    image_motion_enabled: bool = False,
    image_motion_preset: str = "",
    image_motion_intensity: float = _IMAGE_MOTION_DEFAULT_INTENSITY,
):
    # WebUI 在某些二次生成场景下可能传入空素材列表，这里直接返回空结果，避免抛出 NoneType 异常。
    if not materials:
        return []

    # 仅返回通过预处理校验的素材，避免低分辨率图片继续进入后续的视频合成流程。
    valid_materials = []
    local_videos_dir = utils.storage_dir("local_videos", create=True)
    target_size = resolve_video_size(video_aspect, video_resolution)
    global_motion_preset = normalize_image_motion_preset(image_motion_preset)
    global_motion_intensity = clamp_image_motion_intensity(image_motion_intensity)

    for material in materials:
        if not material.url:
            continue

        try:
            if getattr(material, "provider", "") == "asset_hub":
                material_source_path = str(
                    asset_hub_manifest.resolve_asset_hub_asset_path(material.url)
                )
            else:
                material_source_path = file_security.resolve_path_within_directory(
                    local_videos_dir, material.url
                )
        except ValueError as exc:
            # local video_source 的素材路径来自 API 参数，必须限制在专用素材目录。
            # 允许用户传文件名，也兼容历史返回的绝对路径，但不允许逃逸到系统
            # 其他目录，避免任意文件读取或通过 MoviePy 探测本地敏感文件。
            if getattr(material, "provider", "") == "asset_hub":
                message_prefix = "asset hub path rejected"
            else:
                message_prefix = "skip unsafe local material"
            logger.warning(
                f"{message_prefix}: {material.url}, "
                f"local_videos_dir: {local_videos_dir}, error: {str(exc)}"
            )
            continue

        ext = utils.parse_extension(material_source_path)
        try:
            # 图片素材直接按图片方式读取，避免先走 VideoFileClip 误判后触发不稳定的回退分支。
            if ext in const.FILE_TYPE_IMAGES:
                clip, material_source_path = _open_image_clip_with_fallback(
                    material_source_path
                )
            elif ext in const.FILE_TYPE_VIDEOS:
                clip, material_source_path = _open_video_clip_with_sanitized_fallback(
                    material_source_path
                )
            else:
                clip = _open_video_clip_quietly(material_source_path)
        except Exception as exc:
            # 非标准扩展名或探测失败时再回退到图片模式，兼容历史上直接传本地图片路径的情况。
            if ext in const.FILE_TYPE_VIDEOS:
                logger.warning(
                    f"skip unreadable local material: {material.url}, error: {str(exc)}"
                )
                continue
            try:
                clip, material_source_path = _open_image_clip_with_fallback(
                    material_source_path
                )
            except Exception as exc:
                logger.warning(
                    f"skip unreadable local material: {material.url}, error: {str(exc)}"
                )
                continue
        try:
            width = clip.size[0]
            height = clip.size[1]
            if not is_material_resolution_acceptable(width, height):
                logger.warning(
                    f"low resolution material: {width}x{height}, minimum "
                    f"{_MIN_MATERIAL_DIMENSION}x{_MIN_MATERIAL_DIMENSION} required "
                    f"(tolerance {_MIN_DIMENSION_TOLERANCE}px)"
                )
                # 探测到低分辨率素材后立即关闭资源，并且不要把该素材返回给后续流程。
                close_clip(clip)
                continue

            if ext in const.FILE_TYPE_IMAGES:
                logger.info(f"processing image: {material_source_path}")
                # 探测尺寸时已经打开过一次素材，这里先释放探测句柄，再重新创建用于导出的图片 clip。
                close_clip(clip)
                duration = float(material.duration or clip_duration)
                asset_motion = getattr(material, "motion", "") or ""
                if asset_motion:
                    motion_preset = normalize_image_motion_preset(asset_motion)
                    motion_intensity = clamp_image_motion_intensity(
                        getattr(material, "motion_intensity", None)
                    )
                elif image_motion_enabled:
                    motion_preset = global_motion_preset
                    motion_intensity = global_motion_intensity
                else:
                    motion_preset = "none"
                    motion_intensity = global_motion_intensity

                if motion_preset == "none":
                    logger.info(
                        "image motion skipped: "
                        f"file={os.path.basename(material_source_path)}, preset=none"
                    )
                    # Preserve the historical local image behavior when motion is disabled.
                    clip = (
                        ImageClip(material_source_path)
                        .with_duration(duration)
                        .with_position("center")
                    )
                    zoom_clip = clip.resized(
                        lambda t: 1 + (duration * 0.03) * (t / clip.duration)
                    )
                    final_clip = CompositeVideoClip([zoom_clip])
                else:
                    logger.info(
                        "image motion applied: "
                        f"file={os.path.basename(material_source_path)}, "
                        f"preset={motion_preset}, "
                        f"intensity={motion_intensity:.2f}, "
                        f"duration={duration:g}"
                    )
                    clip = None
                    final_clip = create_image_motion_clip(
                        image_path=material_source_path,
                        duration=duration,
                        target_size=target_size,
                        motion_preset=motion_preset,
                        intensity=motion_intensity,
                    )

                if getattr(material, "provider", "") == "asset_hub":
                    processed_dir = utils.storage_dir(
                        "cache_videos/asset_hub_images",
                        create=True,
                    )
                    digest = hashlib.sha1(
                        material_source_path.encode("utf-8")
                    ).hexdigest()[:12]
                    stem = os.path.splitext(os.path.basename(material_source_path))[0]
                    video_file = os.path.join(processed_dir, f"{stem}-{digest}.mp4")
                else:
                    video_file = f"{material_source_path}.mp4"
                final_clip.write_videofile(video_file, fps=30, logger=None)
                close_clip(clip)
                close_clip(final_clip)
                material.url = video_file
                logger.success(f"image processed: {video_file}")
            else:
                # 普通视频素材只需要读取尺寸做校验，校验完成后立即释放句柄即可。
                close_clip(clip)
                # Update url to the resolved absolute path so that downstream
                # stages (combine_videos) can open the file without re-resolving.
                material.url = material_source_path
        except Exception:
            close_clip(clip)
            raise

        valid_materials.append(material)

    return valid_materials
