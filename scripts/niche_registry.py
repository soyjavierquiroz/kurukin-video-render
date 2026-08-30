#!/usr/bin/env python3
"""Load and validate the versioned, non-secret niche registry."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY_PATH = PROJECT_ROOT / "config" / "niches.json"
SUPPORTED_VERSION = 1
REQUIRED_FIELDS = (
    "sheet_id",
    "rclone_remote",
    "final_drive_folder_id",
    "default_asset_profile",
)


class NicheRegistryError(ValueError):
    """Raised when a niche registry cannot be used safely."""


def _non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _read_registry(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise NicheRegistryError(f"niche registry not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise NicheRegistryError(f"invalid niche registry JSON: {path}: {exc.msg}") from exc

    if not isinstance(payload, dict):
        raise NicheRegistryError("niche registry root must be an object")
    if payload.get("version") != SUPPORTED_VERSION:
        raise NicheRegistryError(
            f"unsupported niche registry version: {payload.get('version')!r} "
            f"(supported: {SUPPORTED_VERSION})"
        )
    if not isinstance(payload.get("niches"), dict):
        raise NicheRegistryError("niche registry field 'niches' must be an object")
    return payload


def _validate_niche(niche_id: str, niche: Any) -> dict[str, Any]:
    if not isinstance(niche, dict):
        raise NicheRegistryError(f"niche '{niche_id}' must be an object")

    for field in REQUIRED_FIELDS:
        if not _non_empty_string(niche.get(field)):
            raise NicheRegistryError(
                f"niche '{niche_id}' field '{field}' must be a non-empty string"
            )

    profiles = niche.get("allowed_asset_profiles")
    if not isinstance(profiles, list) or not profiles:
        raise NicheRegistryError(
            f"niche '{niche_id}' field 'allowed_asset_profiles' must be a non-empty list"
        )
    if not all(_non_empty_string(profile) for profile in profiles):
        raise NicheRegistryError(
            f"niche '{niche_id}' allowed asset profiles must be non-empty strings"
        )
    if len(profiles) != len(set(profiles)):
        raise NicheRegistryError(
            f"niche '{niche_id}' allowed asset profiles contain duplicates"
        )
    if niche["default_asset_profile"] not in profiles:
        raise NicheRegistryError(
            f"niche '{niche_id}' default asset profile must be in allowed asset profiles"
        )
    return niche


def load_niche(niche_id: str, registry_path: Path = DEFAULT_REGISTRY_PATH) -> dict[str, Any]:
    """Return one validated niche configuration without performing network I/O."""
    registry = _read_registry(Path(registry_path))
    niches = registry["niches"]
    if niche_id not in niches:
        raise NicheRegistryError(f"unknown niche_id: {niche_id}")
    return _validate_niche(niche_id, niches[niche_id])


def enabled_niches(registry_path: Path = DEFAULT_REGISTRY_PATH) -> list[tuple[str, dict[str, Any]]]:
    """Return enabled registry entries, validating each before use.

    ``enabled`` is intentionally backwards-compatible: existing entries are
    enabled unless they explicitly opt out.
    """
    registry = _read_registry(Path(registry_path))
    result: list[tuple[str, dict[str, Any]]] = []
    for niche_id, raw_niche in registry["niches"].items():
        niche = _validate_niche(niche_id, raw_niche)
        enabled = niche.get("enabled", True)
        if not isinstance(enabled, bool):
            raise NicheRegistryError(f"niche '{niche_id}' field 'enabled' must be a boolean")
        if enabled:
            result.append((niche_id, niche))
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate a non-secret niche configuration.")
    parser.add_argument("--niche", required=True, help="Niche ID to validate")
    parser.add_argument(
        "--registry",
        type=Path,
        default=DEFAULT_REGISTRY_PATH,
        help="Path to the niche registry JSON file",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        niche = load_niche(args.niche, args.registry)
    except NicheRegistryError as exc:
        print(f"NICHE CONFIG ERROR: {exc}", file=sys.stderr)
        return 1

    print("NICHE CONFIG OK")
    print(f"niche_id={args.niche}")
    print(f"rclone_remote={niche['rclone_remote']}")
    print(f"asset_profiles={len(niche['allowed_asset_profiles'])}")
    print(f"default_asset_profile={niche['default_asset_profile']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
