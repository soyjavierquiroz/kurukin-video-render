"""Durable, host-owned preparation requests for Human Review.

The API writes only this small sidecar.  The host dispatcher performs ingest
and invokes the existing review adapter; it never changes review semantics.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Callable

import requests

from app.custom.kurukin_asset_hub import KurukinAssetHubUnavailableError
from app.custom import human_review
from scripts import content_ingest, create_content_job_review


STATE_NAME = "review-preparation.json"
LOG = logging.getLogger(__name__)
ACTIVE = {"pending", "running", "retry_wait"}
RETRY_DELAYS = (5, 15, 30)


class ReviewPreparationRetryError(ValueError):
    """The requested explicit retry is not permitted for this durable state."""


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def state_path(niche_id: str, content_id: str, *, job_root: Path = content_ingest.DEFAULT_JOB_ROOT) -> Path:
    return Path(job_root) / niche_id / content_id / STATE_NAME


def _write(handle: Any, value: dict[str, Any]) -> None:
    handle.seek(0); handle.truncate()
    json.dump(value, handle, ensure_ascii=False, sort_keys=True)
    handle.write("\n"); handle.flush(); os.fsync(handle.fileno())


def _read(handle: Any) -> dict[str, Any]:
    handle.seek(0)
    value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("review preparation state must be an object")
    return value


def _identity(payload: dict[str, Any]) -> dict[str, Any]:
    """Immutable source identity; editorial title changes do not duplicate work."""
    return {key: payload[key] for key in ("content_id", "niche_id", "audio_file_id", "script_file_id", "asset_profile")}


def _inputs(payload: dict[str, Any]) -> dict[str, Any]:
    return {**_identity(payload), "title": payload["title"]}


def _sanitized_message(exc: BaseException) -> str:
    value = str(exc).replace("\n", " ").strip()
    if any(word in value.lower() for word in ("api_key", "apikey", "authorization", "token", "secret")):
        return "<redacted>"
    return value[:500] or type(exc).__name__


def sheet_error_message(record: dict[str, Any]) -> str | None:
    """Return the bounded, system-owned diagnostic intended for the Sheet."""
    if record.get("state") != "error":
        return None
    value = str(record.get("last_error_message") or "").replace("\n", " ").strip()
    # Current worker failures carry a concise leading summary.  For older
    # records, do not project their arbitrary worker output to the Sheet.
    match = re.match(r"review failed exit=\d+:\s*(.+)", value, re.IGNORECASE)
    if match:
        value = match.group(1).strip()
    elif value.lower().startswith("review failed exit="):
        value = "Human Review preparation failed"
    if not value or any(word in value.lower() for word in ("api_key", "apikey", "authorization", "token", "secret")):
        return "Human Review preparation failed"
    return f"Human Review preparation failed: {value[:240]}"


def _exception_chain(exc: BaseException) -> str:
    values: list[str] = []
    current: BaseException | None = exc
    while current is not None:
        values.append(f"{type(current).__name__}:{_sanitized_message(current)}")
        current = current.__cause__ or current.__context__
    return " <- ".join(values)


def enqueue(payload: dict[str, Any], *, job_root: Path = content_ingest.DEFAULT_JOB_ROOT) -> dict[str, Any]:
    """Create/reuse exactly one durable request, without executing adapters."""
    path = state_path(payload["niche_id"], payload["content_id"], job_root=job_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.seek(0)
            raw = handle.read().strip()
            if raw:
                handle.seek(0)
                existing = _read(handle)
                if _identity(existing) != _identity(payload):
                    raise content_ingest.ContentIngestError("content_id already exists with different review preparation identity")
                return existing
            record = {
                **_inputs(payload), "state": "pending", "attempt": 0,
                "created_at": now(), "started_at": None, "finished_at": None,
                "next_retry_at": None, "last_error_class": None, "last_error_message": None,
            }
            _write(handle, record)
            return record
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def rearm(path: Path) -> dict[str, Any]:
    """Explicitly requeue one terminal failure, retaining its evidence.

    ``attempt`` is reserved here so the retry is observable durably before the
    dispatcher claims it.  ``run_record`` consumes that reservation rather
    than incrementing a second time.
    """
    with path.open("r+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            record = _read(handle)
            state = record.get("state")
            if state in ACTIVE:
                return record
            if state != "error":
                raise ReviewPreparationRetryError("review preparation retry requires terminal error")
            history = record.get("attempt_history")
            if history is None:
                history = []
            if not isinstance(history, list):
                raise ValueError("review preparation history is invalid")
            history.append({
                "attempt": int(record.get("attempt", 0)),
                "state": "error",
                "started_at": record.get("started_at"),
                "finished_at": record.get("finished_at"),
                "last_error_class": record.get("last_error_class"),
                "last_error_message": record.get("last_error_message"),
                "retry_requested_at": now(),
            })
            record.update({"state": "pending", "attempt": int(record.get("attempt", 0)) + 1,
                           "attempt_history": history, "started_at": None,
                           "finished_at": None, "next_retry_at": None,
                           "last_error_class": None, "last_error_message": None,
                           "attempt_reserved": True})
            record.pop("pid", None); record.pop("boot_id", None)
            _write(handle, record)
            return record
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def public_state(record: dict[str, Any]) -> str:
    return "HUMAN_REVIEW_READY" if record.get("state") == "completed" else "ERROR" if record.get("state") == "error" else "PREPARING_REVIEW"


def is_transient(exc: BaseException) -> bool:
    chain: BaseException | None = exc
    while chain is not None:
        if isinstance(chain, (KurukinAssetHubUnavailableError, requests.Timeout, requests.ConnectionError)):
            return True
        chain = chain.__cause__ or chain.__context__
    return False


def due(record: dict[str, Any], clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.timezone.utc)) -> bool:
    if record.get("state") == "pending":
        return True
    if record.get("state") != "retry_wait":
        return False
    try:
        return dt.datetime.fromisoformat(str(record["next_retry_at"])) <= clock()
    except (KeyError, TypeError, ValueError):
        return True


def run_record(path: Path, *, boot_id: str, pid: int, clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.timezone.utc)) -> dict[str, str]:
    """Claim then execute one request synchronously in the host runner process."""
    with path.open("r+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            record = _read(handle)
            if not due(record, clock):
                return {"content_id": str(record.get("content_id", "unknown")), "action": "ignored"}
            attempt = int(record.get("attempt", 0))
            if not record.pop("attempt_reserved", False):
                attempt += 1
            record.update({"state": "running", "attempt": attempt,
                           "started_at": now(), "boot_id": boot_id, "pid": pid, "next_retry_at": None})
            _write(handle, record)
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    content_id, niche_id, attempt = record["content_id"], record["niche_id"], record["attempt"]
    started = dt.datetime.now(dt.timezone.utc)
    try:
        metadata = content_ingest.ingest_content(**_inputs(record))
        _, plan = create_content_job_review.create_content_job_review(path.parent)
        if not plan.is_file():
            raise create_content_job_review.ContentJobReviewError("review plan was not created")
        plan_payload = human_review.read_json(plan)
        if not create_content_job_review.plan_identity_matches_content_job(
            plan_payload, metadata, content_id,
        ):
            raise create_content_job_review.ContentJobReviewError(
                "generated review plan identity differs from content job"
            )
        outcome = "completed"
        error: BaseException | None = None
    except Exception as exc:
        outcome, error = "failed", exc
        LOG.error(
            "review preparation failed content_id=%s niche_id=%s stage=ingest_or_review attempt=%s exception_class=%s exception_chain=%s",
            content_id, niche_id, attempt, type(exc).__name__, _exception_chain(exc),
        )
    elapsed_ms = int((dt.datetime.now(dt.timezone.utc) - started).total_seconds() * 1000)
    with path.open("r+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            current = _read(handle)
            if outcome == "completed":
                current.update({"state": "completed", "finished_at": now(), "next_retry_at": None,
                                "last_error_class": None, "last_error_message": None})
                action = "completed"
            elif is_transient(error) and attempt < len(RETRY_DELAYS):
                delay = RETRY_DELAYS[attempt - 1]
                current.update({"state": "retry_wait", "next_retry_at": (clock() + dt.timedelta(seconds=delay)).isoformat(),
                                "last_error_class": type(error).__name__, "last_error_message": _sanitized_message(error)})
                action = "retry_wait"
            else:
                current.update({"state": "error", "finished_at": now(), "next_retry_at": None,
                                "last_error_class": type(error).__name__, "last_error_message": _sanitized_message(error)})
                action = "error"
            current.pop("pid", None); current.pop("boot_id", None)
            _write(handle, current)
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return {"content_id": content_id, "niche_id": niche_id, "action": action, "attempt": str(attempt), "elapsed_ms": str(elapsed_ms), "error_class": type(error).__name__ if error else ""}
