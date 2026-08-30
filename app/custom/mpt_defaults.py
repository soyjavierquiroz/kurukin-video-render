"""Small, versioned MPT capability defaults for the approved batch path."""

from __future__ import annotations

from typing import Any, Mapping

from app.models.schema import VideoAspect, VideoTransitionMode


class MptDefaultsError(ValueError):
    """Raised when a niche's MPT defaults are unsafe or unsupported."""


DEFAULTS_VERSION = 1
_BGM_MODES = {"NONE", "RANDOM", "CUSTOM", "SONILO", "ELEVENLABS"}
_DEFAULT_KEYS = {"version", "bgm", "video_resolution", "video_aspect", "video_clip_duration", "video_transition_mode"}
_BGM_KEYS = {"mode", "volume", "file_id", "prompt"}


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise MptDefaultsError(f"mpt_defaults.{field} must be an object")
    return value


def _text(value: Any, field: str, *, allow_blank: bool = True) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise MptDefaultsError(f"mpt_defaults.{field} must be a string")
    value = value.strip()
    if not allow_blank and not value:
        raise MptDefaultsError(f"mpt_defaults.{field} must be non-blank")
    return value


def resolve_effective_mpt_settings(mpt_defaults: Any = None) -> dict[str, Any]:
    """Resolve niche defaults over the deliberately no-BGM batch baseline.

    A future per-job override belongs at this boundary; none is accepted yet.
    """
    effective: dict[str, Any] = {
        "version": DEFAULTS_VERSION,
        "bgm": {"mode": "NONE", "volume": 0.0, "file_id": "", "prompt": ""},
        "video_resolution": "",
        "video_aspect": "9:16",
        "video_clip_duration": 5,
        "video_transition_mode": None,
    }
    if mpt_defaults is None:
        return effective
    defaults = _mapping(mpt_defaults, "")
    unknown = set(defaults) - _DEFAULT_KEYS
    if unknown:
        raise MptDefaultsError("unsupported mpt_defaults field(s): " + ", ".join(sorted(unknown)))
    version = defaults.get("version", DEFAULTS_VERSION)
    if version != DEFAULTS_VERSION:
        raise MptDefaultsError(f"unsupported mpt_defaults.version: {version!r} (supported: {DEFAULTS_VERSION})")

    if "video_resolution" in defaults:
        effective["video_resolution"] = _text(defaults["video_resolution"], "video_resolution")
    if "video_aspect" in defaults:
        aspect = _text(defaults["video_aspect"], "video_aspect", allow_blank=False)
        try:
            effective["video_aspect"] = VideoAspect(aspect).value
        except ValueError as exc:
            allowed = ", ".join(item.value for item in VideoAspect)
            raise MptDefaultsError(f"mpt_defaults.video_aspect must be one of: {allowed}") from exc
    if "video_clip_duration" in defaults:
        value = defaults["video_clip_duration"]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise MptDefaultsError("mpt_defaults.video_clip_duration must be an integer >= 1")
        effective["video_clip_duration"] = value
    if "video_transition_mode" in defaults and defaults["video_transition_mode"] is not None:
        transition = _text(defaults["video_transition_mode"], "video_transition_mode", allow_blank=False)
        try:
            effective["video_transition_mode"] = VideoTransitionMode(transition).value
        except ValueError as exc:
            allowed = ", ".join(item.value for item in VideoTransitionMode)
            raise MptDefaultsError(f"mpt_defaults.video_transition_mode must be one of: {allowed}") from exc

    if "bgm" not in defaults:
        return effective
    bgm = _mapping(defaults["bgm"], "bgm")
    unknown_bgm = set(bgm) - _BGM_KEYS
    if unknown_bgm:
        raise MptDefaultsError("unsupported mpt_defaults.bgm field(s): " + ", ".join(sorted(unknown_bgm)))
    mode = _text(bgm.get("mode", "NONE"), "bgm.mode", allow_blank=False).upper()
    if mode not in _BGM_MODES:
        raise MptDefaultsError("mpt_defaults.bgm.mode must be NONE, RANDOM, CUSTOM, SONILO, or ELEVENLABS")
    volume = bgm.get("volume", 0.0 if mode == "NONE" else 0.2)
    if isinstance(volume, bool) or not isinstance(volume, (int, float)) or volume < 0:
        raise MptDefaultsError("mpt_defaults.bgm.volume must be a number >= 0")
    resolved = {"mode": mode, "volume": float(volume), "file_id": "", "prompt": ""}
    if mode == "CUSTOM":
        file_id = _text(bgm.get("file_id"), "bgm.file_id", allow_blank=False)
        # Native resolver restricts resolution to storage/bgm and resource/songs.
        try:
            from app.services import bgm as bgm_service
            bgm_service.resolve_bgm_file(file_id)
        except Exception as exc:
            raise MptDefaultsError("mpt_defaults.bgm.file_id must resolve inside approved MPT BGM roots") from exc
        resolved["file_id"] = file_id
    if mode in {"SONILO", "ELEVENLABS"}:
        resolved["prompt"] = _text(bgm.get("prompt"), "bgm.prompt", allow_blank=False)
    effective["bgm"] = resolved
    return effective


def mpt_video_params(settings: Mapping[str, Any]) -> dict[str, Any]:
    """Translate normalized settings to native ``VideoParams`` fields."""
    settings = resolve_effective_mpt_settings(settings)
    bgm = settings["bgm"]
    mode = bgm["mode"]
    bgm_file = ""
    if mode == "CUSTOM":
        # Resolve at render time too: the persisted identifier is not trusted
        # as a filesystem path, and the native resolver enforces its roots.
        from app.services import bgm as bgm_service
        try:
            bgm_file = bgm_service.resolve_bgm_file(bgm["file_id"])
        except ValueError as exc:
            raise MptDefaultsError(
                "mpt_defaults.bgm.file_id must resolve inside approved MPT BGM roots"
            ) from exc
    params = {
        "video_resolution": settings["video_resolution"],
        "video_aspect": settings["video_aspect"],
        "video_clip_duration": settings["video_clip_duration"],
        "video_transition_mode": settings["video_transition_mode"],
        "bgm_type": {"NONE": "", "RANDOM": "random", "CUSTOM": "custom", "SONILO": "sonilo", "ELEVENLABS": "elevenlabs"}[mode],
        "bgm_file": bgm_file,
        "bgm_volume": 0.0 if mode == "NONE" else bgm["volume"],
        "video_music_prompt": bgm["prompt"] if mode in {"SONILO", "ELEVENLABS"} else "",
        "sonilo_bgm_prompt": bgm["prompt"] if mode == "SONILO" else "",
    }
    return params
