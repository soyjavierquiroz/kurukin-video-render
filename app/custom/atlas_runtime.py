"""Explicit, opt-in construction of the Atlas material capabilities.

This module intentionally reads only the process environment supplied by its
caller.  It does not load a dotenv file and does not register Atlas into any
material policy.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Mapping

from app.custom.atlas_client import AtlasClient, AtlasInvalidRequestError
from app.custom.atlas_materializer import AtlasMaterializer
from app.custom.atlas_provider import AtlasProvider


class AtlasRuntimeConfigurationError(ValueError):
    """Atlas environment configuration is present but invalid."""


@dataclass(frozen=True)
class AtlasRuntime:
    """The three Atlas capabilities backed by one client instance."""

    client: AtlasClient
    provider: AtlasProvider
    materializer: AtlasMaterializer


def _enabled_from_env(value: object) -> bool:
    if value is None:
        return False
    if not isinstance(value, str):
        raise AtlasRuntimeConfigurationError("ATLAS_ENABLED must be true or false")
    normalized = value.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise AtlasRuntimeConfigurationError("ATLAS_ENABLED must be true or false")


def _timeout_from_env(value: object) -> float:
    if value is None:
        return 15.0
    if not isinstance(value, str) or not value.strip():
        raise AtlasRuntimeConfigurationError("ATLAS_TIMEOUT_SECONDS must be a positive number")
    try:
        timeout = float(value)
    except ValueError as exc:
        raise AtlasRuntimeConfigurationError(
            "ATLAS_TIMEOUT_SECONDS must be a positive number"
        ) from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise AtlasRuntimeConfigurationError("ATLAS_TIMEOUT_SECONDS must be a positive number")
    return timeout


def build_atlas_runtime_from_env(
    environ: Mapping[str, str] | None = None,
) -> AtlasRuntime | None:
    """Build Atlas only when explicitly enabled in the process environment.

    ``environ`` exists solely to make this boundary deterministic in tests;
    production callers use ``os.environ``.  Atlas policy membership and scope
    remain owned by the existing MPT discovery path.
    """
    values = os.environ if environ is None else environ
    if not _enabled_from_env(values.get("ATLAS_ENABLED")):
        return None

    base_url = values.get("ATLAS_BASE_URL")
    if not isinstance(base_url, str) or not base_url.strip():
        raise AtlasRuntimeConfigurationError(
            "ATLAS_BASE_URL is required when ATLAS_ENABLED is true"
        )
    timeout = _timeout_from_env(values.get("ATLAS_TIMEOUT_SECONDS"))
    try:
        client = AtlasClient(base_url, timeout_seconds=timeout)
    except AtlasInvalidRequestError as exc:
        raise AtlasRuntimeConfigurationError("ATLAS_BASE_URL or ATLAS_TIMEOUT_SECONDS is invalid") from exc
    return AtlasRuntime(
        client=client,
        provider=AtlasProvider(client),
        materializer=AtlasMaterializer(client),
    )
