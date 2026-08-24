"""Internal n8n-facing ingress for content jobs awaiting Human Review.

This module deliberately delegates to the existing ingest and review adapters.
It contains no approval, render, upload, scheduling, or cleanup behavior.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
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
