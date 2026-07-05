"""Reusable subtitle style presets for Kurukin/MoneyPrinterTurbo jobs."""

from __future__ import annotations

from pathlib import Path
from typing import Any


FONT_PREFERENCES = (
    "Montserrat-Bold.ttf",
    "MontserratBold.ttf",
    "BeVietnamPro-Bold.ttf",
    "MicrosoftYaHeiBold.ttc",
    "STHeitiMedium.ttc",
)

ALLOWED_OVERRIDES = {
    "subtitle_position",
    "custom_position",
    "font_name",
    "text_fore_color",
    "text_background_color",
    "rounded_subtitle_background",
    "font_size",
    "stroke_color",
    "stroke_width",
}

PRESET_ALIASES = {
    "center_white_black_outline": "clean_center_bold",
    "safe_center_white_black_outline": "clean_center_bold_safe",
    "bottom_white_black_outline": "clean_bottom_bold",
}

PRESETS = {
    "clean_center_bold": {
        "subtitle_position": "center",
        "text_fore_color": "#FFFFFF",
        "text_background_color": False,
        "rounded_subtitle_background": False,
        "font_size": 72,
        "stroke_color": "#000000",
        "stroke_width": 3,
    },
    "clean_center_bold_safe": {
        "subtitle_position": "center",
        "text_fore_color": "#FFFFFF",
        "text_background_color": False,
        "rounded_subtitle_background": False,
        "font_size": 54,
        "stroke_color": "#000000",
        "stroke_width": 2,
    },
    "clean_bottom_bold": {
        "subtitle_position": "bottom",
        "text_fore_color": "#FFFFFF",
        "text_background_color": False,
        "rounded_subtitle_background": False,
        "font_size": 66,
        "stroke_color": "#000000",
        "stroke_width": 3,
    },
    "boxed_bottom": {
        "subtitle_position": "bottom",
        "text_fore_color": "#FFFFFF",
        "text_background_color": "#000000",
        "rounded_subtitle_background": False,
        "font_size": 60,
        "stroke_color": "#000000",
        "stroke_width": 1,
    },
    "large_hook_center": {
        "subtitle_position": "center",
        "text_fore_color": "#FFFFFF",
        "text_background_color": False,
        "rounded_subtitle_background": False,
        "font_size": 88,
        "stroke_color": "#000000",
        "stroke_width": 4,
    },
}


class SubtitleStylePresetError(Exception):
    """Expected preset resolution or validation error."""


def _font_path(fonts_dir: str | Path, font_name: str) -> Path:
    return Path(fonts_dir).expanduser().resolve() / font_name


def resolve_bold_font(fonts_dir: str | Path) -> str:
    for font_name in FONT_PREFERENCES:
        if _font_path(fonts_dir, font_name).is_file():
            return font_name

    preferred = ", ".join(FONT_PREFERENCES)
    raise SubtitleStylePresetError(
        f"no subtitle font fallback found in {Path(fonts_dir)}; expected one of: {preferred}"
    )


def resolve_preset_name(preset_name: str) -> str:
    resolved = PRESET_ALIASES.get(preset_name, preset_name)
    if resolved not in PRESETS:
        available = ", ".join(sorted([*PRESETS, *PRESET_ALIASES]))
        raise SubtitleStylePresetError(
            f"unknown subtitle_style_preset {preset_name!r}; available: {available}"
        )
    return resolved


def validate_overrides(overrides: Any, fonts_dir: str | Path) -> dict[str, Any]:
    if overrides is None:
        return {}
    if not isinstance(overrides, dict):
        raise SubtitleStylePresetError("subtitle_style_overrides must be a JSON object")

    invalid_keys = sorted(set(overrides) - ALLOWED_OVERRIDES)
    if invalid_keys:
        allowed = ", ".join(sorted(ALLOWED_OVERRIDES))
        invalid = ", ".join(invalid_keys)
        raise SubtitleStylePresetError(
            f"subtitle_style_overrides contains unsupported field(s): {invalid}; allowed: {allowed}"
        )

    normalized = dict(overrides)
    font_name = normalized.get("font_name")
    if font_name is not None:
        if not isinstance(font_name, str) or not font_name.strip():
            raise SubtitleStylePresetError(
                "subtitle_style_overrides.font_name must be a non-empty string"
            )
        if Path(font_name).name != font_name or "/" in font_name or "\\" in font_name:
            raise SubtitleStylePresetError(
                "subtitle_style_overrides.font_name must be a filename inside resource/fonts"
            )
        if not _font_path(fonts_dir, font_name).is_file():
            raise SubtitleStylePresetError(
                f"subtitle_style_overrides.font_name does not exist in {Path(fonts_dir)}: {font_name}"
            )
        normalized["font_name"] = font_name

    return normalized


def resolve_subtitle_style(
    preset_name: Any,
    overrides: Any,
    fonts_dir: str | Path,
) -> tuple[str | None, dict[str, Any], dict[str, Any] | None]:
    normalized_overrides = validate_overrides(overrides, fonts_dir)

    if preset_name is None:
        if normalized_overrides:
            return None, normalized_overrides, dict(normalized_overrides)
        return None, {}, None
    if not isinstance(preset_name, str) or not preset_name.strip():
        raise SubtitleStylePresetError(
            "subtitle_style_preset must be a non-empty string when provided"
        )

    resolved_name = resolve_preset_name(preset_name.strip())
    style = dict(PRESETS[resolved_name])
    style["font_name"] = resolve_bold_font(fonts_dir)
    style.update(normalized_overrides)
    return resolved_name, normalized_overrides, style
