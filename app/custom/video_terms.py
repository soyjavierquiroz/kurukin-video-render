"""Dependency-free parsing for native ``VideoParams.video_terms`` values."""

from __future__ import annotations

import re
from typing import Any


def normalize_video_terms(value: Any) -> list[str]:
    """Apply the native comma/Chinese-comma video-terms parsing contract.

    This deliberately preserves empty terms, ordering, and list handling from
    the MPT task path.  Callers decide whether an empty input should trigger
    generated-term fallback before invoking this parser.
    """
    if isinstance(value, str):
        return [term.strip() for term in re.split(r"[,，]", value)]
    if isinstance(value, list):
        return [term.strip() for term in value]
    raise ValueError("video_terms must be a string or a list of strings.")
