"""Internal n8n-facing ingress for content jobs awaiting Human Review.

This module deliberately delegates to the existing ingest and review adapters.
It contains no approval, render, upload, scheduling, or cleanup behavior.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import subprocess
import sys
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.custom import human_review
from scripts import content_ingest, create_content_job_review


LOG = logging.getLogger(__name__)
app = FastAPI(title="MPT internal automation API", docs_url=None, redoc_url=None, openapi_url=None)


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
    provenance = plan.get("content_job")
    if not isinstance(provenance, dict) or provenance.get("content_id") != content_id:
        return False
    fields = (
        "niche_id", "asset_profile", "audio_sha256", "script_sha256",
        "resolved_asset_policy",
    )
    return all(provenance.get(field) == metadata.get(field) for field in fields)


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


def _launch_immediate_production(schedule_record: Path) -> None:
    """Detach the canonical approved-plan producer from the API request."""
    subprocess.Popen(
        [sys.executable, "-m", "scripts.automation_api", "--run-now", schedule_record.as_posix()],
        cwd=Path(__file__).resolve().parents[1].as_posix(),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _run_immediate_production(schedule_record: Path) -> int:
    """Worker entry point which invokes the existing approved-plan producer only."""
    try:
        record = _read_object(schedule_record)
        plan_path = Path(str(record["production_plan_path"]))
        record["production_state"] = "producing"
        record["pid"] = os.getpid()
        _write_schedule_record(schedule_record, record)
        status = create_content_job_review.produce_batch.process_approved_review_plan(plan_path)
        record["production_state"] = "completed" if status == "completed" else "error"
        _write_schedule_record(schedule_record, record)
        return 0 if status == "completed" else 1
    except Exception:
        try:
            record = _read_object(schedule_record)
            record["production_state"] = "error"
            _write_schedule_record(schedule_record, record)
        except Exception:
            LOG.error("immediate production state update failed")
        LOG.exception("immediate production failed")
        return 1


def _status_payload(content_id: str) -> dict[str, Any]:
    found = _content_job_for(content_id)
    if found is None:
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


def _is_identity_conflict(message: str) -> bool:
    return "already exists with different" in message or "already exists with a different" in message


def _validate_pending_plan(plan_path: Path, content_id: str) -> None:
    plan = _read_object(plan_path)
    if plan.get("review_status") != human_review.STATUS_PENDING:
        raise ValueError("review plan is not pending")
    provenance = plan.get("content_job")
    if not isinstance(provenance, dict) or provenance.get("content_id") != content_id:
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
    provenance = plan.get("content_job")
    immutable_provenance = (
        "content_id", "niche_id", "asset_profile", "audio_sha256", "script_sha256",
        "resolved_asset_policy",
    )
    if (
        plan.get("review_status") == human_review.STATUS_PENDING
        and isinstance(provenance, dict)
        and all(provenance.get(field) == metadata.get(field) for field in immutable_provenance)
    ):
        return True
    return False


@app.get("/healthz")
def healthz() -> dict[str, bool]:
    return {"ok": True}


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

        project_root = create_content_job_review.produce_batch.HOST_ROOT
        state = _production_state(job_dir, plan_path, project_root, plan)
        if state in {"completed", "producing", "queued_night"}:
            return {"ok": True, "content_id": content_id, "run_mode": payload.run_mode, "production_state": state}

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
        try:
            _launch_immediate_production(schedule_record)
        except Exception:
            _write_schedule_record(schedule_record, {
                "content_id": content_id,
                "production_plan_path": plan_path.as_posix(),
                "production_state": "error",
            })
            raise
        return {"ok": True, "content_id": content_id, "run_mode": "NOW", "production_state": "producing"}
    except HTTPException:
        raise
    except Exception:
        LOG.exception("content scheduling failed for content_id=%r", content_id)
        raise HTTPException(status_code=500, detail="unable to schedule production")


@app.post("/v1/content/review")
def create_review(payload: ReviewRequest) -> dict[str, Any]:
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
                "review_status": human_review.STATUS_PENDING,
                "review_relative_url": review_relative_url(payload.content_id),
            }
        metadata = content_ingest.ingest_content(
            niche_id=payload.niche_id,
            content_id=payload.content_id,
            title=payload.title,
            audio_file_id=payload.audio_file_id,
            script_file_id=payload.script_file_id,
            asset_profile=payload.asset_profile,
        )
        _, plan_path = create_content_job_review.create_content_job_review(
            content_ingest.DEFAULT_JOB_ROOT / metadata["niche_id"] / metadata["content_id"]
        )
        _validate_pending_plan(plan_path, payload.content_id)
    except content_ingest.ContentIngestError as exc:
        status = 409 if _is_identity_conflict(str(exc)) else 500
        LOG.warning("content ingest failed for content_id=%r: %s", payload.content_id, exc)
        raise HTTPException(status_code=status, detail="content identity conflict" if status == 409 else "content ingest failed")
    except create_content_job_review.ContentJobReviewError as exc:
        LOG.warning("review creation failed for content_id=%r: %s", payload.content_id, exc)
        raise HTTPException(status_code=409 if "differs" in str(exc) else 500, detail="review conflict" if "differs" in str(exc) else "review creation failed")
    except Exception:
        # Do not include an upstream exception value here: libraries may echo
        # credentials in it.  The request identity is sufficient for diagnosis.
        LOG.error("automation request failed for content_id=%r", payload.content_id)
        raise HTTPException(status_code=500, detail="unable to prepare human review")

    return {
        "ok": True,
        "content_id": payload.content_id,
        "niche_id": payload.niche_id,
        "review_status": human_review.STATUS_PENDING,
        "review_relative_url": review_relative_url(payload.content_id),
    }


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--run-now":
        sys.exit(_run_immediate_production(Path(sys.argv[2])))
    raise SystemExit("automation_api is served by uvicorn")
