#!/usr/bin/env python3
"""Create one validated, local content job from Google Drive file IDs.

This deliberately stops before batch planning, review, rendering, or upload.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.custom.material_source_policy import PROVIDER_ASSET_HUB, build_asset_hub_source_policy
from app.custom.mpt_defaults import resolve_effective_mpt_settings

try:  # Supports both ``python scripts/...`` and package imports in tests.
    from scripts.asset_profile_resolver import AssetProfileError, resolve_asset_profile
    from scripts.niche_registry import DEFAULT_REGISTRY_PATH, NicheRegistryError, load_niche
except ModuleNotFoundError:  # pragma: no cover - exercised by the direct CLI
    from asset_profile_resolver import AssetProfileError, resolve_asset_profile
    from niche_registry import DEFAULT_REGISTRY_PATH, NicheRegistryError, load_niche


DEFAULT_JOB_ROOT = PROJECT_ROOT / "storage" / "content_jobs"
SAFE_CONTENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
CONTENT_SCHEMA_VERSION = 1


class ContentIngestError(ValueError):
    """Raised when a content job cannot be safely created or reused."""


def _required_text(field: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContentIngestError(f"{field} must be a non-empty string")
    return value.strip()


def validate_request(
    niche_id: str,
    content_id: str,
    title: str,
    audio_file_id: str,
    script_file_id: str,
    asset_profile: str,
    registry_path: Path = DEFAULT_REGISTRY_PATH,
) -> tuple[dict[str, Any], Any]:
    """Validate all user metadata before any network operation."""
    niche_id = _required_text("niche", niche_id)
    content_id = _required_text("content_id", content_id)
    if not SAFE_CONTENT_ID.fullmatch(content_id) or content_id in {".", ".."}:
        raise ContentIngestError(
            "content_id must be filesystem-safe (letters, digits, '.', '_' and '-')"
        )
    _required_text("title", title)
    _required_text("audio_file_id", audio_file_id)
    _required_text("script_file_id", script_file_id)
    _required_text("asset_profile", asset_profile)
    try:
        niche = load_niche(niche_id, registry_path)
    except NicheRegistryError as exc:
        raise ContentIngestError(str(exc)) from exc
    try:
        policy = resolve_asset_profile(niche_id, asset_profile, registry_path)
    except AssetProfileError as exc:
        raise ContentIngestError(str(exc)) from exc
    return niche, policy


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def audio_duration_seconds(path: Path) -> float:
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(path),
            ],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        duration = float(result.stdout.strip())
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        raise ContentIngestError(f"ffprobe validation failed for audio: {path.name}") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise ContentIngestError(f"audio duration must be greater than zero: {path.name}")
    return duration


def validate_downloads(
    audio_path: Path,
    script_path: Path,
    duration_reader: Callable[[Path], float] = audio_duration_seconds,
) -> tuple[float, str, str]:
    if not audio_path.is_file() or audio_path.stat().st_size == 0:
        raise ContentIngestError("downloaded source.mp3 is missing or empty")
    duration = duration_reader(audio_path)
    if duration <= 0:
        raise ContentIngestError("audio duration must be greater than zero: source.mp3")
    if not script_path.is_file() or script_path.stat().st_size == 0:
        raise ContentIngestError("downloaded script.txt is missing or empty")
    try:
        script = script_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ContentIngestError("downloaded script.txt is not valid UTF-8") from exc
    if not script.strip():
        raise ContentIngestError("downloaded script.txt contains no non-whitespace text")
    return duration, _sha256(audio_path), _sha256(script_path)


def download_by_file_id(rclone_remote: str, file_id: str, target: Path) -> None:
    """Copy a Drive object ID directly to a deterministic local filename."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.partial")
    try:
        subprocess.run(
            ["rclone", "backend", "copyid", f"{rclone_remote}:", file_id, str(temporary)],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if not temporary.is_file():
            raise ContentIngestError(f"rclone did not produce {target.name}")
        os.replace(temporary, target)
    except subprocess.CalledProcessError as exc:
        raise ContentIngestError(_rclone_download_error(target, exc.stderr)) from exc
    finally:
        temporary.unlink(missing_ok=True)


def _rclone_download_error(target: Path, stderr: Any) -> str:
    """Return a concise, secret-free source-download diagnostic.

    Rclone/Google errors can include request URLs and credential refresh
    details.  Durable preparation state is visible to the Sheet, so retain
    only a stable failure class and never the Drive ID or stderr itself.
    """
    name = "Source audio" if target.name == "source.mp3" else (
        "Source script" if target.name == "script.txt" else "Source file"
    )
    detail = str(stderr or "").lower()
    if any(marker in detail for marker in (
        "invalid_grant", "couldn't fetch token", "could not fetch token",
        "unauthorized", "authentication", "oauth",
    )):
        reason = "authentication failed"
    elif any(marker in detail for marker in ("permission denied", "forbidden", "insufficient permissions")):
        reason = "permission denied"
    elif any(marker in detail for marker in ("not found", "404", "does not exist")):
        reason = "file not found or inaccessible"
    elif "shortcut" in detail:
        reason = "object is a shortcut and cannot be downloaded as a source file"
    else:
        reason = "download failed"
    return f"{name} could not be downloaded from Drive: {reason}"


def asset_policy_summary(policy: Any) -> dict[str, Any]:
    """Serialize only the resolved, non-secret provenance needed downstream."""
    asset_hub_enabled = policy.providers.is_enabled(PROVIDER_ASSET_HUB)
    return {
        "providers": list(policy.providers.enabled),
        "asset_hub": {
            "sources": build_asset_hub_source_policy(policy)["sources"] if asset_hub_enabled else [],
            "generic_fallback": bool(policy.asset_hub.include.generic),
        },
    }


def _existing_identity(
    metadata_path: Path,
    audio_file_id: str,
    script_file_id: str,
    asset_profile: str,
) -> None:
    try:
        existing = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContentIngestError(f"existing content.json is invalid: {metadata_path}") from exc
    if existing.get("audio_file_id") != audio_file_id or existing.get("script_file_id") != script_file_id:
        raise ContentIngestError(
            "content_id already exists with different source Drive IDs; refusing to overwrite content identity"
        )
    if existing.get("asset_profile") != asset_profile:
        raise ContentIngestError(
            "content_id already exists with a different asset_profile; "
            "refusing to overwrite content identity"
        )


def ingest_content(
    *,
    niche_id: str,
    content_id: str,
    title: str,
    audio_file_id: str,
    script_file_id: str,
    asset_profile: str,
    video_terms: str | None = None,
    registry_path: Path = DEFAULT_REGISTRY_PATH,
    job_root: Path = DEFAULT_JOB_ROOT,
    download_file: Callable[[str, str, Path], None] = download_by_file_id,
    duration_reader: Callable[[Path], float] = audio_duration_seconds,
) -> dict[str, Any]:
    """Create or safely revalidate one local content job."""
    niche, policy = validate_request(
        niche_id, content_id, title, audio_file_id, script_file_id, asset_profile, registry_path
    )
    job_dir = Path(job_root) / niche_id / content_id
    metadata_path = job_dir / "content.json"
    audio_path = job_dir / "source.mp3"
    script_path = job_dir / "script.txt"

    if job_dir.exists() and not metadata_path.is_file():
        # The asynchronous API writes this durable command before ingest.
        # No other partial content-job layout is safe to adopt.
        if {entry.name for entry in job_dir.iterdir()} != {"review-preparation.json"}:
            raise ContentIngestError(f"content job directory exists without content.json: {job_dir}")
    existing = metadata_path.is_file()
    if existing:
        _existing_identity(metadata_path, audio_file_id, script_file_id, asset_profile)
    else:
        job_dir.mkdir(parents=True, exist_ok=True)

    try:
        if not (audio_path.is_file() and script_path.is_file()):
            download_file(niche["rclone_remote"], audio_file_id, audio_path)
            download_file(niche["rclone_remote"], script_file_id, script_path)

        duration, audio_sha256, script_sha256 = validate_downloads(
            audio_path, script_path, duration_reader
        )
        if existing:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if (
                metadata.get("audio_sha256") != audio_sha256
                or metadata.get("script_sha256") != script_sha256
                or metadata.get("audio_duration_seconds") != duration
            ):
                raise ContentIngestError(
                    "existing content job inputs differ from content.json; refusing to overwrite provenance"
                )
            # This is the one operator-owned mutable input.  It is not a
            # system projection; a Sheet edit intentionally replaces it.
            normalized_input = video_terms if isinstance(video_terms, str) and video_terms.strip() else None
            if metadata.get("video_terms") != normalized_input:
                if normalized_input is None:
                    metadata.pop("video_terms", None)
                else:
                    metadata["video_terms"] = normalized_input
                temporary = metadata_path.with_name(f".{metadata_path.name}.{uuid.uuid4().hex}.partial")
                temporary.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                os.replace(temporary, metadata_path)
            return metadata

        metadata = {
            "schema_version": CONTENT_SCHEMA_VERSION,
            "content_id": content_id,
            "niche_id": niche_id,
            "title": title,
            "audio_file_id": audio_file_id,
            "script_file_id": script_file_id,
            "asset_profile": asset_profile,
            "rclone_remote": niche["rclone_remote"],
            "audio_sha256": audio_sha256,
            "script_sha256": script_sha256,
            "audio_duration_seconds": duration,
            "ingested_at": datetime.now(timezone.utc).isoformat(),
            "resolved_asset_policy": asset_policy_summary(policy),
            "mpt_defaults": niche.get("mpt_defaults"),
            "effective_mpt_settings": resolve_effective_mpt_settings(niche.get("mpt_defaults")),
        }
        if isinstance(video_terms, str) and video_terms.strip():
            metadata["video_terms"] = video_terms
        temporary = metadata_path.with_name(f".{metadata_path.name}.{uuid.uuid4().hex}.partial")
        try:
            temporary.write_text(
                json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            os.replace(temporary, metadata_path)
        finally:
            temporary.unlink(missing_ok=True)
        return metadata
    except ContentIngestError:
        if not existing:
            _remove_incomplete_ingest_artifacts(job_dir)
        raise
    except Exception as exc:
        if not existing:
            _remove_incomplete_ingest_artifacts(job_dir)
        raise ContentIngestError("content ingest failed before content.json was committed") from exc


def _remove_incomplete_ingest_artifacts(job_dir: Path) -> None:
    """Retain the durable async command while clearing failed download residue."""
    if not job_dir.exists():
        return
    for child in job_dir.iterdir():
        if child.name == "review-preparation.json":
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)
    try:
        job_dir.rmdir()
    except OSError:
        # A durable review-preparation command intentionally keeps the directory.
        pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create one validated local content job.")
    parser.add_argument("--niche", required=True)
    parser.add_argument("--content-id", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--audio-file-id", required=True)
    parser.add_argument("--script-file-id", required=True)
    parser.add_argument("--asset-profile", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        metadata = ingest_content(
            niche_id=args.niche, content_id=args.content_id, title=args.title,
            audio_file_id=args.audio_file_id, script_file_id=args.script_file_id,
            asset_profile=args.asset_profile,
        )
    except ContentIngestError as exc:
        print(f"CONTENT INGEST ERROR: {exc}", file=sys.stderr)
        return 1
    print("CONTENT INGEST OK")
    print(f"content_id={metadata['content_id']}")
    print(f"job_dir={DEFAULT_JOB_ROOT / metadata['niche_id'] / metadata['content_id']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
