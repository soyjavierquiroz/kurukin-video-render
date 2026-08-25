"""Idempotent final delivery of completed Human Review batch renders."""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
from typing import Any

from app.custom import human_review
from scripts.content_ingest import SAFE_CONTENT_ID
from scripts.niche_registry import load_niche
from scripts.production_registry import sha256_file
from scripts.produce_batch import REPORT_NAME, sanitize_batch_id, valid_mp4


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REQUIRED_DELIVERY_FIELDS = (
    "content_id", "niche_id", "final_drive_file_id", "final_drive_url", "checksum",
)
SHA256_HEX = re.compile(r"^[0-9a-fA-F]{64}$")


class DeliveryError(RuntimeError):
    """A completed render could not be safely delivered."""


def _host_path(value: str | Path) -> Path:
    """Map the container project mount to the local project root when needed."""
    path = Path(value)
    if path.is_absolute():
        try:
            return PROJECT_ROOT / path.relative_to("/MoneyPrinterTurbo")
        except ValueError:
            pass
    return path


def _resolve_under(root: Path, *parts: str | Path, description: str) -> Path:
    """Resolve a path and reject paths (including symlinks) escaping ``root``."""
    base = root.resolve(strict=False)
    candidate = base.joinpath(*parts).resolve(strict=False)
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise DeliveryError(f"invalid {description}") from exc
    return candidate


def _read_object(path: Path, description: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DeliveryError(f"invalid {description}") from exc
    if not isinstance(value, dict):
        raise DeliveryError(f"invalid {description}")
    return value


def _required_string(payload: dict[str, Any], field: str, description: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise DeliveryError(f"invalid {description}")
    return value


def read_delivery(path: Path, *, content_id: str | None = None, niche_id: str | None = None) -> dict[str, str]:
    """Read and validate a local delivery sidecar; never contacts Drive."""
    payload = _read_object(path, "delivery record")
    result = {field: _required_string(payload, field, "delivery record") for field in REQUIRED_DELIVERY_FIELDS}
    if not SHA256_HEX.fullmatch(result["checksum"]):
        raise DeliveryError("invalid delivery record")
    if result["final_drive_url"] != f"https://drive.google.com/file/d/{result['final_drive_file_id']}/view":
        raise DeliveryError("invalid delivery record")
    if content_id is not None and result["content_id"] != content_id:
        raise DeliveryError("delivery identity mismatch")
    if niche_id is not None and result["niche_id"] != niche_id:
        raise DeliveryError("delivery identity mismatch")
    return result


def _plan_identity(plan: dict[str, Any]) -> tuple[str, str]:
    provenance = plan.get("content_job")
    if not isinstance(provenance, dict):
        raise DeliveryError("invalid production plan")
    return (
        _required_string(provenance, "content_id", "production plan"),
        _required_string(provenance, "niche_id", "production plan"),
    )


def _completed_batch_final(plan: dict[str, Any]) -> Path:
    script_path = _host_path(_required_string(plan, "script_path", "production plan"))
    batch_id = str(plan.get("batch_id") or sanitize_batch_id(script_path.parent))
    stem = str(plan.get("stem") or script_path.stem)
    if not batch_id.strip() or not stem.strip():
        raise DeliveryError("invalid production plan")
    outputs_root = PROJECT_ROOT / "storage" / "batch_outputs"
    batch_output_dir = _resolve_under(outputs_root, batch_id, description="batch output directory")
    report_path = _resolve_under(batch_output_dir, REPORT_NAME, description="batch report")
    report = _read_object(report_path, "batch report")
    jobs = report.get("jobs")
    entry = jobs.get(stem) if isinstance(jobs, dict) else None
    if not isinstance(entry, dict) or entry.get("status") != "completed":
        raise DeliveryError("batch render is not completed")
    batch_final = entry.get("batch_final")
    if not isinstance(batch_final, str) or not batch_final.strip():
        raise DeliveryError("completed batch render has no final video")
    path = _host_path(batch_final)
    if not path.is_absolute():
        path = batch_output_dir / path
    path = _resolve_under(batch_output_dir, path, description="completed batch final")
    if not valid_mp4(path):
        raise DeliveryError("completed batch final is invalid")
    return path


def _rclone_remote_path(remote: str, name: str) -> str:
    return f"{remote.rstrip(':')}:{name}"


def _run_rclone(*args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["rclone", *args], check=True, capture_output=True, text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise DeliveryError("delivery upload failed") from exc


def _drive_file_id(remote_path: str, folder_id: str) -> str:
    completed = _run_rclone(
        "lsjson", remote_path, "--stat", "--drive-root-folder-id", folder_id,
    )
    try:
        metadata = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise DeliveryError("delivery metadata is invalid") from exc
    entries = metadata if isinstance(metadata, list) else [metadata]
    if not entries or not isinstance(entries[0], dict):
        raise DeliveryError("delivery metadata is invalid")
    return _required_string(entries[0], "ID", "delivery metadata")


def finalize_production_plan(production_plan_path: str | Path) -> dict[str, str]:
    """Upload the canonical completed MP4 and atomically persist its sidecar."""
    plan = _read_object(_host_path(production_plan_path), "production plan")
    if plan.get("review_status") != human_review.STATUS_APPROVED:
        raise DeliveryError("production plan is not approved")
    content_id, niche_id = _plan_identity(plan)
    if not SAFE_CONTENT_ID.fullmatch(content_id) or content_id in {".", ".."}:
        raise DeliveryError("invalid content identity")
    job_root = PROJECT_ROOT / "storage" / "content_jobs"
    job_dir = _resolve_under(job_root, niche_id, content_id, description="content identity")
    metadata = _read_object(job_dir / "content.json", "content identity")
    if metadata.get("content_id") != content_id or metadata.get("niche_id") != niche_id:
        raise DeliveryError("content identity mismatch")

    local_mp4 = _completed_batch_final(plan)
    checksum = sha256_file(local_mp4)
    delivery_path = job_dir / "delivery.json"
    if delivery_path.is_file():
        try:
            existing = read_delivery(delivery_path, content_id=content_id, niche_id=niche_id)
        except DeliveryError:
            existing = None
        if existing is not None and existing["checksum"] == checksum:
            return existing

    niche = load_niche(niche_id)
    remote_path = _rclone_remote_path(str(niche["rclone_remote"]), f"{content_id}.mp4")
    folder_id = str(niche["final_drive_folder_id"])
    _run_rclone(
        "copyto", local_mp4.as_posix(), remote_path,
        "--drive-root-folder-id", folder_id,
    )
    file_id = _drive_file_id(remote_path, folder_id)
    payload = {
        "content_id": content_id,
        "niche_id": niche_id,
        "final_drive_file_id": file_id,
        "final_drive_url": f"https://drive.google.com/file/d/{file_id}/view",
        "checksum": checksum,
    }
    human_review.write_json_atomic(delivery_path, payload)
    return payload
