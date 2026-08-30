"""Internal n8n-facing ingress for content jobs awaiting Human Review.

This module deliberately delegates to the existing ingest and review adapters.
It contains no approval, render, upload, scheduling, or cleanup behavior.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from pathlib import Path
import time
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.custom import human_review
from scripts import content_delivery, content_ingest, create_content_job_review, nightly_runner, review_preparation
from scripts.niche_registry import NicheRegistryError, enabled_niches, load_niche


LOG = logging.getLogger(__name__)
app = FastAPI(title="MPT internal automation API", docs_url=None, redoc_url=None, openapi_url=None)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
CANONICAL_SHEET_STATUSES = frozenset({
    "DRAFT", "READY", "PREPARING_REVIEW", "HUMAN_REVIEW_READY",
    "PRODUCTION_READY", "QUEUED_NIGHT", "PRODUCING", "COMPLETED", "ERROR",
})


class ReviewRequest(BaseModel):
    niche_id: str = Field(min_length=1)
    content_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    hook_title: str | None = None
    audio_file_id: str = Field(min_length=1)
    script_file_id: str = Field(min_length=1)
    asset_profile: str = Field(min_length=1)

    class Config:
        extra = "forbid"


class ScheduleRequest(BaseModel):
    run_mode: str
    retry: bool = False

    class Config:
        extra = "forbid"


class ReconcileRequest(BaseModel):
    niche_id: str = Field(min_length=1)
    status: str = Field(min_length=1)
    run_mode: str | None = None
    title: str | None = None
    hook_title: str | None = None
    audio_file_id: str | None = None
    script_file_id: str | None = None
    asset_profile: str | None = None
    cleanup_approved: bool | None = None

    class Config:
        extra = "forbid"


class NicheRequest(BaseModel):
    niche_id: str = Field(min_length=1)

    class Config:
        extra = "forbid"


@app.exception_handler(RequestValidationError)
async def request_validation_error(_: Request, __: RequestValidationError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"error": "invalid request"})


def review_relative_url(content_id: str) -> str:
    """Return a relative Human Review deep link, never a public hostname."""
    return f"/?content_id={quote(content_id, safe='')}"


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("stored content state is invalid") from exc
    if not isinstance(value, dict):
        raise ValueError("stored content state is invalid")
    return value


def _content_job_for(content_id: str) -> tuple[Path, dict[str, Any]] | None:
    if not content_ingest.SAFE_CONTENT_ID.fullmatch(content_id) or content_id in {".", ".."}:
        return None
    matches: list[tuple[Path, dict[str, Any]]] = []
    for metadata_path in content_ingest.DEFAULT_JOB_ROOT.glob(f"*/{content_id}/content.json"):
        metadata = _read_object(metadata_path)
        if metadata.get("content_id") == content_id:
            matches.append((metadata_path.parent, metadata))
    if len(matches) > 1:
        raise ValueError("content_id is ambiguous")
    return matches[0] if matches else None


def _plan_for(job_dir: Path, metadata: dict[str, Any]) -> Path | None:
    try:
        batch_id = create_content_job_review.deterministic_batch_id(
            str(metadata["niche_id"]), str(metadata["content_id"])
        )
        stem = create_content_job_review.produce_batch.sanitize_id(str(metadata["title"]))
    except (KeyError, ValueError) as exc:
        raise ValueError("stored content state is invalid") from exc
    path = human_review.plan_path(batch_id, stem, create_content_job_review.produce_batch.HOST_ROOT)
    return path if path.is_file() else None


def _schedule_record_path(job_dir: Path) -> Path:
    """Keep immediate-launch state with the immutable content job."""
    return job_dir / "production-schedule.json"


def _read_optional_object(path: Path) -> dict[str, Any] | None:
    return _read_object(path) if path.is_file() else None


def _plan_provenance_matches(plan: dict[str, Any], metadata: dict[str, Any], content_id: str) -> bool:
    """Compatibility wrapper for the shared review-plan identity proof."""
    return create_content_job_review.plan_identity_matches_content_job(
        plan, metadata, content_id,
    )


def _same_plan_path(value: Any, plan_path: Path) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return Path(value).resolve() == plan_path.resolve()
    except OSError:
        return False


def _nightly_runtime_state(plan_path: Path, project_root: Path) -> str | None:
    """Observe the existing nightly runner layout; never create or move jobs."""
    queue_root = project_root / "storage" / "nightly_jobs"
    for directory, state in (
        ("pending", "queued_night"),
        ("processing", "producing"),
        ("completed", "completed"),
        ("failed", "error"),
    ):
        root = queue_root / directory
        if not root.is_dir():
            continue
        candidates = root.glob("*.json") if directory == "pending" else root.glob("*/job.json")
        for candidate in candidates:
            try:
                job = _read_object(candidate)
            except ValueError:
                continue
            if job.get("render_mode") == human_review.RENDER_MODE and _same_plan_path(job.get("production_plan_path"), plan_path):
                return state
    return None


def _nightly_job_matches_plan(job: dict[str, Any], plan_path: Path) -> bool:
    """Match a human-review queue job by its stable production-plan identity."""
    return (
        job.get("render_mode") == human_review.RENDER_MODE
        and _same_plan_path(job.get("production_plan_path"), plan_path)
    )


def _find_nightly_job(queue_root: Path, directory: str, plan_path: Path) -> Path | None:
    """Find the exact queue item for a plan in an existing runner directory."""
    root = queue_root / directory
    if not root.is_dir():
        return None
    candidates = root.glob("*.json") if directory == "pending" else root.glob("*/job.json")
    for candidate in candidates:
        try:
            if _nightly_job_matches_plan(_read_object(candidate), plan_path):
                return candidate
        except ValueError:
            continue
    return None


def _hold_pending_nightly_job(queue_root: Path, plan_path: Path) -> str:
    """Atomically claim a pending job for NOW, preserving its queue history.

    The nightly runner owns jobs once they are in processing.  A rename is the
    only claim made here: if the runner wins it first, the source disappears
    and the caller re-observes processing instead of starting a duplicate.
    """
    if _find_nightly_job(queue_root, "processing", plan_path) is not None:
        return "already_processing"
    if (queue_root / "nightly_runner.lock").is_file():
        return "nightly_runner_active"
    pending = _find_nightly_job(queue_root, "pending", plan_path)
    if pending is None:
        return "not_pending"
    held = queue_root / "held"
    held.mkdir(parents=True, exist_ok=True)
    destination = held / f"{pending.stem}-promoted-{time.time_ns()}-{os.getpid()}.json"
    try:
        os.replace(pending, destination)
    except FileNotFoundError:
        return "already_processing" if _find_nightly_job(queue_root, "processing", plan_path) else "not_pending"
    return "held"


def _schedule_runtime_state(job_dir: Path) -> str | None:
    record = _read_optional_object(_schedule_record_path(job_dir))
    if record is None:
        return None
    state = record.get("production_state")
    return state if state in {"producing", "completed", "error"} else "producing"


def _registry_completed(plan: dict[str, Any], project_root: Path) -> bool:
    """Use the existing completed-production registry without creating one."""
    registry_path = project_root / "storage" / create_content_job_review.produce_batch.REGISTRY_NAME
    if not registry_path.is_file():
        return False
    try:
        producer = create_content_job_review.produce_batch
        audio = Path(str(plan.get("audio_path") or ""))
        script = Path(str(plan.get("script_path") or ""))
        if not audio.is_file() or not script.is_file():
            return False
        recipe = producer.production_recipe_for(
            str(plan.get("visual_style") or producer.VISUAL_STYLE_NONE), "karaoke", "bottom",
        )
        record = producer.production_identity(
            str(plan.get("stem") or script.stem), audio, script,
            str(plan.get("material_title") or ""), recipe,
        )
        return producer.production_registry().find_valid(
            record["production_fingerprint"], producer.valid_mp4,
        ) is not None
    except (OSError, ValueError, KeyError):
        return False


def _production_state(job_dir: Path, plan_path: Path, project_root: Path, plan: dict[str, Any] | None = None) -> str:
    immediate = _schedule_runtime_state(job_dir)
    if immediate in {"completed", "error", "producing"}:
        return immediate
    nightly = _nightly_runtime_state(plan_path, project_root)
    if nightly:
        return nightly
    if plan is not None and _registry_completed(plan, project_root):
        return "completed"
    return "not_scheduled"


def _write_schedule_record(path: Path, payload: dict[str, Any]) -> None:
    human_review.write_json_atomic(path, payload)


def _retry_now_schedule(
    job_dir: Path, content_id: str, plan_path: Path,
) -> dict[str, Any]:
    """Explicitly re-arm one failed NOW schedule without touching queue history."""
    schedule_path = _schedule_record_path(job_dir)
    record = _read_optional_object(schedule_path)
    if record is None:
        raise HTTPException(status_code=409, detail="production schedule not found")

    state = record.get("production_state")
    if state != "error":
        # A retry request is intentionally a no-op outside the terminal error
        # state. In particular, never create another producer while a prior
        # attempt is launching, producing, or already completed.
        return {
            "ok": True,
            "content_id": content_id,
            "run_mode": "NOW",
            "production_state": state if isinstance(state, str) else "not_scheduled",
        }

    if record.get("content_id") != content_id or not _same_plan_path(record.get("production_plan_path"), plan_path):
        raise HTTPException(status_code=409, detail="production schedule provenance conflict")

    # Retain schedule identity and future-compatible fields, removing only
    # runtime facts from the failed attempt.
    retry_record = dict(record)
    for field in ("pid", "boot_id", "error", "finished_at"):
        retry_record.pop(field, None)
    retry_record["production_state"] = "launching"
    _write_schedule_record(schedule_path, retry_record)
    return {
        "ok": True,
        "content_id": content_id,
        "run_mode": "NOW",
        "production_state": "launching",
    }


def _nightly_queue_dir() -> Path:
    """Use the runner's own container-or-host queue resolution."""
    return nightly_runner.default_queue_dir(project_root=PROJECT_ROOT)


def _queue_job_count(root: Path, directory: str) -> int:
    path = root / directory
    if not path.is_dir():
        return 0
    if directory == "pending":
        return sum(1 for entry in path.glob("*.json") if entry.is_file())
    return sum(1 for entry in path.glob("*/job.json") if entry.is_file())


def _processing_job_identity(queue_root: Path) -> str | None:
    processing = queue_root / "processing"
    if not processing.is_dir():
        return None
    jobs = sorted(path for path in processing.glob("*/job.json") if path.is_file())
    # The runner-generated processing directory name is a safe queue identity;
    # do not return job payloads or arbitrary filesystem paths.
    return jobs[0].parent.name if jobs else None


def _nightly_status_payload() -> dict[str, Any]:
    queue_root = _nightly_queue_dir()
    lock_present = (queue_root / "nightly_runner.lock").is_file()
    # The canonical runner's lock is the cross-process running authority.
    running = lock_present
    return {
        "ok": True,
        "running": running,
        "lock_present": lock_present,
        "pending_count": _queue_job_count(queue_root, "pending"),
        "processing_count": _queue_job_count(queue_root, "processing"),
        "completed_count": _queue_job_count(queue_root, "completed"),
        "failed_count": _queue_job_count(queue_root, "failed"),
        "current_job": _processing_job_identity(queue_root),
    }


def _nightly_window_is_open() -> bool:
    """Apply exactly the canonical runner's default window interpretation."""
    args = nightly_runner.build_parser().parse_args([])
    return nightly_runner.is_in_window(
        dt.datetime.now().astimezone(), args.window_start, args.window_end,
    )


def _status_payload(content_id: str) -> dict[str, Any]:
    found = _content_job_for(content_id)
    if found is None:
        records = list(content_ingest.DEFAULT_JOB_ROOT.glob(f"*/{content_id}/{review_preparation.STATE_NAME}"))
        if len(records) == 1:
            record = _read_object(records[0])
            return {
                "ok": True, "content_id": content_id, "niche_id": record.get("niche_id"),
                "content_job_exists": False, "review_exists": False,
                "review_status": None, "review_relative_url": None,
                "status": review_preparation.public_state(record), "production_state": "not_scheduled",
            }
        raise HTTPException(status_code=404, detail="content job not found")
    job_dir, metadata = found
    niche_id = metadata.get("niche_id")
    if not isinstance(niche_id, str) or not niche_id.strip():
        raise ValueError("stored content state is invalid")
    plan_path = _plan_for(job_dir, metadata)
    plan = _read_object(plan_path) if plan_path else None
    return {
        "ok": True,
        "content_id": content_id,
        "niche_id": niche_id,
        "content_job_exists": True,
        "review_exists": plan is not None,
        "review_status": plan.get("review_status") if plan else None,
        "review_relative_url": review_relative_url(content_id) if plan else None,
        "production_state": _production_state(
            job_dir, plan_path, create_content_job_review.produce_batch.HOST_ROOT, plan,
        ) if plan_path else "not_scheduled",
    }


def _review_preparation_record(niche_id: str, content_id: str) -> dict[str, Any] | None:
    path = review_preparation.state_path(niche_id, content_id, job_root=content_ingest.DEFAULT_JOB_ROOT)
    return _read_optional_object(path)


def _sheet_projection(
    content_id: str, niche_id: str, status: str, run_mode: str | None,
    *, review_exists: bool = False, error: str | None = None,
) -> dict[str, Any]:
    """Return the small, path-free Sheet representation used by n8n."""
    return {
        "ok": error is None,
        "content_id": content_id,
        "niche_id": niche_id,
        "status": status,
        "run_mode": run_mode,
        "review_url": review_relative_url(content_id) if review_exists else None,
        "final_drive_file_id": None,
        "final_drive_url": None,
        "checksum": None,
        "error": error,
    }


def _require_review_fields(content_id: str, payload: ReconcileRequest) -> ReviewRequest:
    fields = ("title", "audio_file_id", "script_file_id", "asset_profile")
    if any(not isinstance(getattr(payload, field), str) or not getattr(payload, field).strip() for field in fields):
        raise HTTPException(status_code=400, detail="review inputs are required for READY content")
    return ReviewRequest(
        niche_id=payload.niche_id, content_id=content_id, title=payload.title,
        hook_title=payload.hook_title, audio_file_id=payload.audio_file_id,
        script_file_id=payload.script_file_id, asset_profile=payload.asset_profile,
    )


def _is_identity_conflict(message: str) -> bool:
    return "already exists with different" in message or "already exists with a different" in message


def _validate_pending_plan(
    plan_path: Path, content_id: str, metadata: dict[str, Any] | None = None,
) -> None:
    plan = _read_object(plan_path)
    if plan.get("review_status") != human_review.STATUS_PENDING:
        raise ValueError("review plan is not pending")
    if metadata is None:
        found = _content_job_for(content_id)
        metadata = found[1] if found is not None else None
    if metadata is None or not _plan_provenance_matches(plan, metadata, content_id):
        raise ValueError("review plan does not match content job")


def _existing_idempotent_review(payload: ReviewRequest) -> bool:
    """Return whether a pending review is safely reusable without invoking adapters.

    Content jobs and review plans can have been created before this API, and
    their absolute source paths may reflect a different bind-mount location.
    The immutable Drive identity and content-job provenance are the proof of
    identity; adapter-local paths, titles, and batch representations are not.
    """
    found = _content_job_for(payload.content_id)
    if found is None:
        return False
    job_dir, metadata = found
    expected_identity = {
        "content_id": payload.content_id,
        "niche_id": payload.niche_id,
        "audio_file_id": payload.audio_file_id,
        "script_file_id": payload.script_file_id,
        "asset_profile": payload.asset_profile,
    }
    for field, expected in expected_identity.items():
        if metadata.get(field) != expected:
            raise content_ingest.ContentIngestError(
                f"content_id already exists with different {field}; refusing to overwrite content identity"
            )

    plan_path = _plan_for(job_dir, metadata)
    if plan_path is None:
        return False
    plan = _read_object(plan_path)
    if plan.get("review_status") == human_review.STATUS_PENDING and _plan_provenance_matches(
        plan, metadata, payload.content_id
    ):
        return True
    return False


def _validate_enabled_niche(niche_id: str) -> None:
    try:
        niche = load_niche(niche_id)
    except NicheRegistryError as exc:
        raise HTTPException(status_code=404, detail="unknown niche_id") from exc
    enabled = niche.get("enabled", True)
    if not isinstance(enabled, bool):
        LOG.error("niche registry has invalid enabled value for niche_id=%r", niche_id)
        raise HTTPException(status_code=500, detail="unable to read niche registry")
    if not enabled:
        raise HTTPException(status_code=403, detail="niche is disabled")


@app.get("/healthz")
def healthz() -> dict[str, bool]:
    return {"ok": True}


@app.get("/v1/niches")
def list_niches() -> dict[str, Any]:
    try:
        niches = [
            {
                "niche_id": niche_id,
                "enabled": True,
                "sheet_id": niche["sheet_id"],
                "sheet_tab": niche.get("sheet_tab"),
            }
            for niche_id, niche in enabled_niches()
        ]
    except (NicheRegistryError, KeyError, TypeError):
        LOG.exception("niche registry lookup failed")
        raise HTTPException(status_code=500, detail="unable to read niche registry")
    return {"ok": True, "niches": niches}


@app.get("/v1/nightly/status")
def nightly_status() -> dict[str, Any]:
    try:
        return _nightly_status_payload()
    except OSError:
        LOG.exception("nightly status lookup failed")
        raise HTTPException(status_code=500, detail="unable to read nightly state")


@app.post("/v1/nightly/run")
def run_nightly() -> dict[str, Any]:
    try:
        status = _nightly_status_payload()
        if status["running"]:
            return {"ok": True, "nightly_state": "already_running"}
        if status["pending_count"] == 0:
            return {"ok": True, "nightly_state": "no_pending"}
        if not _nightly_window_is_open():
            return {"ok": True, "nightly_state": "outside_window"}
        # pending/ is the durable request signal; the host execution runner
        # observes it and invokes the canonical runner on its next tick.
        return {"ok": True, "nightly_state": "started"}
    except OSError:
        LOG.exception("nightly status lookup failed")
        raise HTTPException(status_code=500, detail="unable to read nightly state")
    except Exception as exc:
        LOG.exception("nightly run status check failed")
        raise HTTPException(status_code=500, detail="unable to check nightly state")


@app.get("/v1/content/{content_id}")
def get_content(content_id: str) -> dict[str, Any]:
    try:
        return _status_payload(content_id)
    except HTTPException:
        raise
    except ValueError:
        LOG.warning("content status lookup failed for content_id=%r", content_id)
        raise HTTPException(status_code=500, detail="unable to read content state")


@app.post("/v1/content/{content_id}/schedule")
def schedule_content(content_id: str, payload: ScheduleRequest) -> dict[str, Any]:
    if payload.run_mode not in {"NIGHT", "NOW"}:
        raise HTTPException(status_code=400, detail="invalid run_mode")
    if payload.retry and payload.run_mode != "NOW":
        raise HTTPException(status_code=400, detail="retry is only supported for NOW")
    try:
        found = _content_job_for(content_id)
        if found is None:
            raise HTTPException(status_code=404, detail="content job not found")
        job_dir, metadata = found
        plan_path = _plan_for(job_dir, metadata)
        if plan_path is None:
            raise HTTPException(status_code=409, detail="human review not found")
        plan = _read_object(plan_path)
        if not _plan_provenance_matches(plan, metadata, content_id):
            raise HTTPException(status_code=409, detail="review provenance conflict")
        if plan.get("review_status") != human_review.STATUS_APPROVED:
            raise HTTPException(status_code=409, detail="review is not approved")

        if payload.retry:
            return _retry_now_schedule(job_dir, content_id, plan_path)

        project_root = create_content_job_review.produce_batch.HOST_ROOT
        state = _production_state(job_dir, plan_path, project_root, plan)
        if (
            payload.run_mode == "NOW"
            and state == "producing"
            and _schedule_runtime_state(job_dir) is None
            and _find_nightly_job(_nightly_queue_dir(), "processing", plan_path) is not None
        ):
            return {
                "ok": True, "content_id": content_id, "run_mode": "NOW",
                "production_state": "producing", "schedule_state": "already_processing",
            }
        if state in {"completed", "producing"}:
            return {"ok": True, "content_id": content_id, "run_mode": payload.run_mode, "production_state": state}

        promoted_from: str | None = None
        if state == "queued_night":
            if payload.run_mode == "NIGHT":
                return {"ok": True, "content_id": content_id, "run_mode": "NIGHT", "production_state": "queued_night"}
            promotion = _hold_pending_nightly_job(_nightly_queue_dir(), plan_path)
            if promotion == "already_processing":
                return {
                    "ok": True, "content_id": content_id, "run_mode": "NOW",
                    "production_state": "producing", "schedule_state": "already_processing",
                }
            if promotion == "nightly_runner_active":
                return {
                    "ok": True, "content_id": content_id, "run_mode": "NOW",
                    "production_state": "queued_night", "schedule_state": "nightly_runner_active",
                }
            if promotion == "not_pending":
                state = _production_state(job_dir, plan_path, project_root, plan)
                if state in {"completed", "producing", "queued_night"}:
                    return {"ok": True, "content_id": content_id, "run_mode": "NOW", "production_state": state}
                # A vanished item is not authority to start a second producer.
                return {
                    "ok": True, "content_id": content_id, "run_mode": "NOW",
                    "production_state": "not_scheduled", "schedule_state": "not_pending",
                }
            promoted_from = "queued_night"

        if payload.run_mode == "NIGHT":
            human_review.enqueue_approved_plan(plan_path, plan, project_root=project_root)
            return {"ok": True, "content_id": content_id, "run_mode": "NIGHT", "production_state": "queued_night"}

        schedule_record = _schedule_record_path(job_dir)
        try:
            fd = os.open(schedule_record, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            state = _schedule_runtime_state(job_dir) or "producing"
            return {"ok": True, "content_id": content_id, "run_mode": "NOW", "production_state": state}
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({
                "content_id": content_id,
                "production_plan_path": plan_path.as_posix(),
                "production_state": "launching",
            }, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        response = {"ok": True, "content_id": content_id, "run_mode": "NOW", "production_state": "producing"}
        if promoted_from:
            response.update({"schedule_state": "started_now", "promoted_from": promoted_from})
        return response
    except HTTPException:
        raise
    except Exception:
        LOG.exception("content scheduling failed for content_id=%r", content_id)
        raise HTTPException(status_code=500, detail="unable to schedule production")


@app.post("/v1/content/{content_id}/reconcile")
def reconcile_content(content_id: str, payload: ReconcileRequest) -> dict[str, Any]:
    """Reconcile a Sheet projection using the existing review and schedule logic."""
    _validate_enabled_niche(payload.niche_id)
    requested_status = payload.status.strip().upper()
    run_mode = payload.run_mode.strip().upper() if isinstance(payload.run_mode, str) else None
    if run_mode not in {None, "NIGHT", "NOW"}:
        raise HTTPException(status_code=400, detail="invalid run_mode")
    if requested_status not in CANONICAL_SHEET_STATUSES:
        raise HTTPException(status_code=400, detail="invalid status")
    if requested_status == "DRAFT":
        try:
            found = _content_job_for(content_id)
            if found is not None and found[1].get("niche_id") != payload.niche_id:
                raise HTTPException(status_code=409, detail="content identity conflict")
        except HTTPException:
            raise
        except Exception:
            LOG.exception("draft content identity lookup failed for content_id=%r", content_id)
            raise HTTPException(status_code=500, detail="unable to read content state")
        return _sheet_projection(content_id, payload.niche_id, "DRAFT", run_mode)
    try:
        found = _content_job_for(content_id)
        if found is None:
            review = _require_review_fields(content_id, payload)
            create_review(review)
            preparation = _review_preparation_record(payload.niche_id, content_id)
            if preparation is not None:
                return _sheet_projection(content_id, payload.niche_id, review_preparation.public_state(preparation), run_mode)
            found = _content_job_for(content_id)
            if found is None:
                return _sheet_projection(content_id, payload.niche_id, "PREPARING_REVIEW", run_mode)

        job_dir, metadata = found
        if metadata.get("niche_id") != payload.niche_id:
            raise HTTPException(status_code=409, detail="content identity conflict")
        plan_path = _plan_for(job_dir, metadata)
        plan = _read_object(plan_path) if plan_path else None
        if plan is None:
            # Existing jobs without a plan can still use the canonical review
            # creation path, but only when the inputs needed by that path exist.
            review = _require_review_fields(content_id, payload)
            create_review(review)
            preparation = _review_preparation_record(payload.niche_id, content_id)
            if preparation is not None:
                return _sheet_projection(content_id, payload.niche_id, review_preparation.public_state(preparation), run_mode)
            plan_path = _plan_for(job_dir, metadata)
            plan = _read_object(plan_path) if plan_path else None
            if plan is None:
                return _sheet_projection(content_id, payload.niche_id, "PREPARING_REVIEW", run_mode)

        state = _production_state(job_dir, plan_path, create_content_job_review.produce_batch.HOST_ROOT, plan)
        if state == "error":
            return _sheet_projection(content_id, payload.niche_id, "ERROR", run_mode, review_exists=True, error="production error")
        if state == "completed":
            try:
                delivery = content_delivery.read_delivery(
                    job_dir / "delivery.json", content_id=content_id, niche_id=payload.niche_id,
                )
            except content_delivery.DeliveryError:
                return _sheet_projection(
                    content_id, payload.niche_id, "ERROR", run_mode, review_exists=True,
                    error="delivery incomplete",
                )
            projection = _sheet_projection(content_id, payload.niche_id, "COMPLETED", run_mode, review_exists=True)
            projection.update({
                "final_drive_file_id": delivery["final_drive_file_id"],
                "final_drive_url": delivery["final_drive_url"],
                "checksum": delivery["checksum"],
            })
            return projection
        if state == "producing":
            return _sheet_projection(content_id, payload.niche_id, "PRODUCING", run_mode, review_exists=True)
        if plan.get("review_status") != human_review.STATUS_APPROVED:
            return _sheet_projection(content_id, payload.niche_id, "HUMAN_REVIEW_READY", run_mode, review_exists=True)
        if run_mode is None:
            raise HTTPException(status_code=400, detail="run_mode is required for approved content")

        scheduled = schedule_content(content_id, ScheduleRequest(run_mode=run_mode))
        production_state = scheduled.get("production_state")
        status = {
            "queued_night": "QUEUED_NIGHT",
            "producing": "PRODUCING",
            "completed": "COMPLETED",
            "error": "ERROR",
        }.get(production_state, "ERROR")
        return _sheet_projection(
            content_id, payload.niche_id, status, run_mode, review_exists=True,
            error="production error" if status == "ERROR" else None,
        )
    except HTTPException:
        raise
    except Exception:
        LOG.exception("content reconcile failed for content_id=%r", content_id)
        return _sheet_projection(content_id, payload.niche_id, "ERROR", run_mode, error="unable to reconcile content")


@app.post("/v1/content/review", response_model=None)
def create_review(payload: ReviewRequest) -> JSONResponse | dict[str, Any]:
    try:
        # Validate before any Drive/Asset Hub work so client mistakes are 400s.
        content_ingest.validate_request(
            payload.niche_id, payload.content_id, payload.title,
            payload.audio_file_id, payload.script_file_id, payload.asset_profile,
        )
    except content_ingest.ContentIngestError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    try:
        if _existing_idempotent_review(payload):
            return {
                "ok": True,
                "content_id": payload.content_id,
                "niche_id": payload.niche_id,
                "status": "HUMAN_REVIEW_READY",
                "review_status": human_review.STATUS_PENDING,
                "review_relative_url": review_relative_url(payload.content_id),
            }
        record = review_preparation.enqueue(payload.model_dump() if hasattr(payload, "model_dump") else payload.dict(), job_root=content_ingest.DEFAULT_JOB_ROOT)
    except content_ingest.ContentIngestError as exc:
        status = 409 if _is_identity_conflict(str(exc)) else 500
        LOG.warning("review request persistence failed content_id=%r exception_class=%s message=%s", payload.content_id, type(exc).__name__, review_preparation._sanitized_message(exc))
        raise HTTPException(status_code=status, detail="content identity conflict" if status == 409 else "content ingest failed")
    except Exception:
        # Do not include an upstream exception value here: libraries may echo
        # credentials in it.  The request identity is sufficient for diagnosis.
        LOG.error("review request persistence failed content_id=%r stage=enqueue exception_class=%s message=%s", payload.content_id, type(exc).__name__, review_preparation._sanitized_message(exc))
        raise HTTPException(status_code=500, detail="unable to prepare human review")

    return JSONResponse(status_code=202, content={
        "ok": True,
        "content_id": payload.content_id,
        "niche_id": payload.niche_id,
        "status": review_preparation.public_state(record),
        "review_status": human_review.STATUS_PENDING,
        "review_relative_url": review_relative_url(payload.content_id) if record.get("state") == "completed" else None,
    })


@app.post("/v1/content/{content_id}/review/retry")
def retry_review_preparation(content_id: str, payload: NicheRequest) -> dict[str, Any]:
    """Explicitly re-arm only a terminal review-preparation failure."""
    _validate_enabled_niche(payload.niche_id)
    path = review_preparation.state_path(payload.niche_id, content_id, job_root=content_ingest.DEFAULT_JOB_ROOT)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="review preparation not found")
    try:
        record = review_preparation.rearm(path)
    except (OSError, ValueError) as exc:
        LOG.error("review retry failed content_id=%r stage=rearm exception_class=%s message=%s", content_id, type(exc).__name__, review_preparation._sanitized_message(exc))
        raise HTTPException(status_code=500, detail="unable to retry review preparation")
    return {"ok": True, "content_id": content_id, "niche_id": payload.niche_id,
            "status": review_preparation.public_state(record)}
