#!/usr/bin/env python3
"""Adapt one validated content job to the existing Human Review entrypoint.

This intentionally does not implement discovery, plan generation, approval, or
production.  Those remain owned by ``produce_batch.process_job`` and the
existing Human Review pipeline.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.custom import human_review
from app.custom.video_terms import normalize_video_terms
from app.custom.material_source_policy import (
    PROVIDER_ASSET_HUB,
    build_asset_hub_source_policy,
    open_sources_policy,
)
from scripts import produce_batch
from scripts.asset_profile_resolver import AssetProfileError, resolve_asset_profile
from scripts.content_ingest import asset_policy_summary
from scripts.niche_registry import DEFAULT_REGISTRY_PATH, NicheRegistryError, load_niche
from app.custom.mpt_defaults import resolve_effective_mpt_settings


class ContentJobReviewError(ValueError):
    """Raised when a content job cannot safely enter Human Review."""


REQUIRED_METADATA = (
    "content_id", "niche_id", "title", "asset_profile", "audio_sha256", "script_sha256",
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _required_text(metadata: dict[str, Any], field: str) -> str:
    value = metadata.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ContentJobReviewError(f"content.json field '{field}' must be a non-empty string")
    return value.strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_content_job(content_job: Path) -> tuple[dict[str, Any], Path, Path]:
    """Read and validate the local, already-ingested content-job inputs."""
    job_dir = Path(content_job).resolve()
    metadata_path = job_dir / "content.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ContentJobReviewError(f"content.json not found: {metadata_path}") from exc
    except json.JSONDecodeError as exc:
        raise ContentJobReviewError(f"invalid content.json: {exc.msg}") from exc
    if not isinstance(metadata, dict):
        raise ContentJobReviewError("content.json must contain an object")

    for field in REQUIRED_METADATA:
        _required_text(metadata, field)
    for field in ("audio_sha256", "script_sha256"):
        if not SHA256_RE.fullmatch(str(metadata[field])):
            raise ContentJobReviewError(f"content.json field '{field}' must be a lowercase SHA256")

    audio, script = job_dir / "source.mp3", job_dir / "script.txt"
    if not audio.is_file() or audio.stat().st_size == 0:
        raise ContentJobReviewError(f"source.mp3 is missing or empty: {audio}")
    if not script.is_file() or script.stat().st_size == 0:
        raise ContentJobReviewError(f"script.txt is missing or empty: {script}")
    try:
        script.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ContentJobReviewError("script.txt is not valid UTF-8") from exc

    if _sha256(audio) != metadata["audio_sha256"]:
        raise ContentJobReviewError("source.mp3 SHA256 does not match content.json")
    if _sha256(script) != metadata["script_sha256"]:
        raise ContentJobReviewError("script.txt SHA256 does not match content.json")
    return metadata, audio, script


def deterministic_batch_id(niche_id: str, content_id: str) -> str:
    """Return the content lifecycle identity, rejecting unsafe registry IDs."""
    if produce_batch.sanitize_id(niche_id) != niche_id or produce_batch.sanitize_id(content_id) != content_id:
        raise ContentJobReviewError("niche_id and content_id must be filesystem-safe for review identity")
    return f"content-{niche_id}-{content_id}"


def legacy_review_arguments(policy: Any) -> tuple[str, str]:
    """Translate only native policies the legacy review manifest represents exactly."""
    policy_dict = policy.to_dict()
    strict_sources = {"sources": [{"scope": "title", "title": "mi-otra-yo"}]}
    if (
        policy.providers.enabled == (PROVIDER_ASSET_HUB,)
        and not policy.asset_hub.include.generic
        and build_asset_hub_source_policy(policy) == strict_sources
    ):
        return "mi-otra-yo", "title-exclusive"
    if policy_dict == open_sources_policy().to_dict():
        # The existing worker maps an empty title to its established open policy.
        return "", "open"
    raise ContentJobReviewError(
        "resolved asset policy cannot be represented safely by the existing Human Review manifest"
    )


def content_job_provenance(metadata: dict[str, Any], policy: Any) -> dict[str, Any]:
    return {
        "content_id": metadata["content_id"],
        "niche_id": metadata["niche_id"],
        "asset_profile": metadata["asset_profile"],
        "resolved_asset_policy": asset_policy_summary(policy),
        "audio_sha256": metadata["audio_sha256"],
        "script_sha256": metadata["script_sha256"],
        "mpt_defaults": deepcopy(metadata.get("mpt_defaults")),
        "effective_mpt_settings": metadata.get("effective_mpt_settings") or resolve_effective_mpt_settings(metadata.get("mpt_defaults")),
        "video_terms": metadata.get("video_terms"),
    }


_REVIEW_SEMANTIC_MPT_FIELDS = ("video_aspect", "video_clip_duration")


def _review_semantic_mpt_settings(
    *, effective_mpt_settings: Any = None, mpt_defaults: Any = None,
) -> dict[str, Any] | None:
    """Return only settings that change the Human Review visual timeline.

    A plan made before MPT defaults has the same semantics as the resolved
    baseline.  BGM, resolution, and transitions are production concerns: they
    do not alter the candidates or segmentation a reviewer is approving.
    """
    source = effective_mpt_settings if effective_mpt_settings is not None else mpt_defaults
    if source is not None and not isinstance(source, dict):
        return None
    # Deliberately exclude BGM before validation: a render-only stale BGM
    # reference must not invalidate an otherwise reusable visual review.
    visual_defaults = {
        field: source[field]
        for field in ("version", *_REVIEW_SEMANTIC_MPT_FIELDS)
        if source is not None and field in source
    }
    try:
        resolved = resolve_effective_mpt_settings(visual_defaults)
    except (TypeError, ValueError):
        return None
    return {field: resolved[field] for field in _REVIEW_SEMANTIC_MPT_FIELDS}


def _review_mpt_settings_match(
    existing: dict[str, Any], requested: dict[str, Any],
) -> bool:
    """Compare resolved review semantics, treating absent legacy settings as defaults."""
    expected = _review_semantic_mpt_settings(
        effective_mpt_settings=requested.get("effective_mpt_settings"),
        mpt_defaults=requested.get("mpt_defaults"),
    )
    actual = _review_semantic_mpt_settings(
        effective_mpt_settings=existing.get("effective_mpt_settings"),
        mpt_defaults=existing.get("mpt_defaults"),
    )
    return expected is not None and actual == expected


def plan_identity_matches_content_job(
    plan: dict[str, Any], metadata: dict[str, Any], content_id: str,
) -> bool:
    """Verify that a review plan belongs to one immutable content job.

    Legacy plans persist a content-job provenance snapshot.  Current plans
    intentionally do not, so their canonical batch/task identity and source
    artifact paths provide the equivalent proof.  A batch_id alone is never
    sufficient evidence.
    """
    provenance = plan.get("content_job")
    if isinstance(provenance, dict):
        if provenance.get("content_id") != content_id:
            return False
        fields = (
            "niche_id", "asset_profile", "audio_sha256", "script_sha256",
            "resolved_asset_policy", "video_terms",
        )
        existing_settings = dict(provenance)
        for field in ("mpt_defaults", "effective_mpt_settings"):
            existing_settings.setdefault(field, plan.get(field))
        return (
            all(
                provenance.get(field) == metadata.get(field)
                for field in fields
                # Hand-authored legacy content jobs predate resolved policy
                # snapshots.  Their policy is still proven separately by the
                # adapter before this compatibility check.
                if field != "resolved_asset_policy" or field in metadata
            )
            and _review_mpt_settings_match(existing_settings, metadata)
        )

    # Only an omitted/null content_job denotes the current format.  Other
    # malformed legacy provenance must not silently fall through to it.
    if provenance is not None:
        return False

    niche_id = metadata.get("niche_id")
    if not isinstance(niche_id, str) or not niche_id:
        return False
    if plan.get("batch_id") != content_id:
        return False
    try:
        batch_id = deterministic_batch_id(niche_id, content_id)
    except (TypeError, ValueError, ContentJobReviewError):
        return False
    task_id = plan.get("task_id")
    if not isinstance(task_id, str) or not task_id.startswith(f"batch-{batch_id}-"):
        return False

    def has_canonical_suffix(value: Any, filename: str) -> bool:
        if not isinstance(value, str):
            return False
        normalized = value.replace("\\", "/").rstrip("/")
        suffix = f"/storage/content_jobs/{niche_id}/{content_id}/{filename}"
        return normalized.endswith(suffix)

    return (
        has_canonical_suffix(plan.get("audio_path"), "source.mp3")
        and has_canonical_suffix(plan.get("script_path"), "script.txt")
        and _review_mpt_settings_match(plan, metadata)
    )


def _plan_matches_except_video_terms(plan: dict[str, Any], provenance_expected: dict[str, Any], content_id: str) -> bool:
    """Identify a pending plan made from the same job before an operator term edit."""
    provenance = plan.get("content_job")
    if not isinstance(provenance, dict):
        return False
    return (
        provenance.get("content_id") == content_id
        and all(provenance.get(field) == provenance_expected.get(field) for field in (
            "niche_id", "asset_profile", "audio_sha256", "script_sha256", "resolved_asset_policy",
        ))
        and provenance.get("video_terms") != provenance_expected.get("video_terms")
    )


def _existing_plan_matches_content_job(
    plan: dict[str, Any],
    *,
    plan_path: Path,
    batch_id: str,
    stem: str,
    audio: Path,
    script: Path,
    policy: Any,
) -> None:
    """Prove that a legacy pending plan is the one for this content job.

    Older manifests derived their internal ``batch_id`` from the content-job
    directory.  The review queue path and task ID, however, have always used
    the adapter's deterministic batch ID.  Do not rewrite that legacy field:
    the deterministic plan location and task identity are the stable proof.
    """
    if plan.get("review_status") != human_review.STATUS_PENDING:
        raise ContentJobReviewError(f"existing review plan is not pending: {plan_path}")
    expected_task_id = f"batch-{batch_id}-{stem}"
    if (
        plan_path != human_review.plan_path(batch_id, stem, produce_batch.HOST_ROOT).resolve()
        or plan.get("stem") != stem
        or plan.get("job_name") != stem
        or plan.get("task_id") != expected_task_id
    ):
        raise ContentJobReviewError(f"existing review plan identity differs: {plan_path}")
    try:
        plan_audio = Path(str(plan["audio_path"])).resolve()
        plan_script = Path(str(plan["script_path"])).resolve()
    except (KeyError, TypeError, ValueError) as exc:
        raise ContentJobReviewError(f"existing review plan source identity is incomplete: {plan_path}") from exc
    if plan_audio != audio.resolve() or plan_script != script.resolve():
        raise ContentJobReviewError(f"existing review plan source identity differs: {plan_path}")

    # Policy models use tuples internally while review plans are JSON, so
    # compare the JSON representation rather than their Python containers.
    expected_material_policy = json.loads(json.dumps(policy.to_dict()))
    expected_asset_hub_policy = build_asset_hub_source_policy(policy)
    if (
        plan.get("material_source_policy") != expected_material_policy
        or plan.get("asset_hub_source_policy") != expected_asset_hub_policy
    ):
        raise ContentJobReviewError(f"existing review plan source policy differs: {plan_path}")


def _validate_existing_plan(
    plan_path: Path,
    *,
    batch_id: str,
    stem: str,
    audio: Path,
    script: Path,
    policy: Any,
    provenance: dict[str, Any],
    metadata: dict[str, Any],
) -> str:
    plan = human_review.read_json(plan_path)
    _existing_plan_matches_content_job(
        plan, plan_path=plan_path, batch_id=batch_id, stem=stem,
        audio=audio, script=script, policy=policy,
    )
    existing_provenance = plan.get("content_job")
    if existing_provenance is not None:
        if not plan_identity_matches_content_job(plan, metadata, str(metadata.get("content_id", ""))):
            raise ContentJobReviewError(f"existing review plan content_job provenance differs: {plan_path}")
        return "already_exists"

    # This is deliberately the only mutation of a legacy pending plan.
    updated_plan = dict(plan)
    updated_plan["content_job"] = provenance
    human_review.write_json_atomic(plan_path, updated_plan)
    return "provenance_backfilled"


def create_content_job_review(
    content_job: Path,
    *,
    registry_path: Path = DEFAULT_REGISTRY_PATH,
) -> tuple[str, Path]:
    """Create one pending existing Human Review plan, or safely reuse it."""
    metadata, audio, script = load_content_job(content_job)
    niche_id = _required_text(metadata, "niche_id")
    content_id = _required_text(metadata, "content_id")
    title = _required_text(metadata, "title")
    profile = _required_text(metadata, "asset_profile")
    try:
        load_niche(niche_id, registry_path)
        policy = resolve_asset_profile(niche_id, profile, registry_path)
    except (NicheRegistryError, AssetProfileError) as exc:
        raise ContentJobReviewError(str(exc)) from exc

    batch_id = deterministic_batch_id(niche_id, content_id)
    stem = produce_batch.sanitize_id(title)
    review_root = human_review.review_root(produce_batch.HOST_ROOT).resolve()
    plan_path = human_review.plan_path(batch_id, stem, produce_batch.HOST_ROOT).resolve()
    try:
        plan_path.relative_to(review_root)
    except ValueError as exc:  # Defensive: metadata must never select another directory.
        raise ContentJobReviewError("review plan path escapes the review queue") from exc

    provenance = content_job_provenance(metadata, policy)
    raw_video_terms = metadata.get("video_terms")
    if raw_video_terms is not None:
        try:
            # Validate early using the native MPT parser; the worker receives
            # the unmodified raw string and resolves it through that same path.
            normalize_video_terms(raw_video_terms)
        except ValueError as exc:
            raise ContentJobReviewError(str(exc)) from exc
    effective_mpt_settings = resolve_effective_mpt_settings(metadata.get("mpt_defaults"))
    if plan_path.exists():
        existing_plan = human_review.read_json(plan_path)
        if (
            existing_plan.get("review_status") == human_review.STATUS_PENDING
            and _plan_matches_except_video_terms(existing_plan, provenance, content_id)
        ):
            # Pending choices are only preparation, never approval authority.
            # Marking this plan stale makes the existing worker rebuild the
            # same canonical location with the edited operator terms.
            existing_plan["review_status"] = "stale_video_terms"
            human_review.write_json_atomic(plan_path, existing_plan)
        else:
            result = _validate_existing_plan(
                plan_path, batch_id=batch_id, stem=stem, audio=audio, script=script,
                policy=policy, provenance=provenance, metadata=metadata,
            )
            return result, plan_path

    material_title, source_policy = legacy_review_arguments(policy)
    job = produce_batch.Job(stem, audio, script, None, batch_id)
    batch_output_dir = produce_batch.HOST_ROOT / "storage" / "batch_outputs" / batch_id
    report_path = batch_output_dir / produce_batch.REPORT_NAME
    report = produce_batch.init_report(batch_id, [job], report_path)
    produce_batch.write_json_atomic(report_path, report)
    status = produce_batch.process_job(
        job, index=1, total=1, batch_output_dir=batch_output_dir, report=report,
        report_path=report_path, preset="editorial-gold", position="bottom",
        human_review_mode=True, material_title=material_title, source_policy=source_policy,
        mpt_defaults=metadata.get("mpt_defaults"), effective_mpt_settings=effective_mpt_settings,
        video_terms=raw_video_terms,
    )
    if status != human_review.STATUS_PENDING or not plan_path.is_file():
        raise ContentJobReviewError("existing Human Review generator did not create a pending plan")
    plan = human_review.read_json(plan_path)
    if (
        plan.get("review_status") != human_review.STATUS_PENDING
        or plan.get("stem") != stem
    ):
        raise ContentJobReviewError("generated review plan identity differs from content job")
    plan["content_job"] = provenance
    plan["mpt_defaults"] = deepcopy(metadata.get("mpt_defaults"))
    plan["effective_mpt_settings"] = effective_mpt_settings
    plan["aspect_ratio"] = effective_mpt_settings["video_aspect"]
    human_review.write_json_atomic(plan_path, plan)
    return "created", plan_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create an existing Human Review plan for one content job.")
    parser.add_argument("--content-job", required=True, type=Path)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY_PATH)
    args = parser.parse_args(argv)
    try:
        result, plan_path = create_content_job_review(args.content_job, registry_path=args.registry)
    except (ContentJobReviewError, OSError) as exc:
        print(f"CONTENT JOB REVIEW FAILED: {exc}", file=sys.stderr)
        return 1
    if result == "already_exists":
        print("REVIEW ALREADY EXISTS")
    elif result == "provenance_backfilled":
        print("PROVENANCE BACKFILLED")
    else:
        print("REVIEW CREATED")
    print(plan_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
