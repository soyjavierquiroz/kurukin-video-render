"""Atlas search adapter, deliberately separate from existing provider wiring."""

from __future__ import annotations

from typing import Any, Mapping
from urllib.parse import urlsplit

from app.custom.atlas_client import (
    AtlasClient,
    AtlasInvalidRequestError,
    AtlasProtocolError,
    validate_asset_uid,
    validate_rendition_kind,
)
from app.custom.material_discovery import MaterialCandidate


_SCOPE_KINDS = frozenset({"title", "titles", "all_titles", "brand", "general", "catalog"})


def build_atlas_scope_from_params(params: Any) -> dict[str, str] | None:
    """Build a title-only Atlas scope from MPT's explicit Atlas identity.

    ``atlas_title_id`` is deliberately the sole input.  In particular, MPT's
    display subject, task identity, and Asset Hub policy must never become an
    Atlas scope by implication.
    """
    title_id = getattr(params, "atlas_title_id", None)
    if not isinstance(title_id, str):
        return None
    title_id = title_id.strip()
    if not title_id:
        return None
    return {"kind": "title", "title_id": title_id}


def _clean_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _explicit_scope(request_data: Mapping[str, Any], scope: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return only an Atlas-specific scope; no legacy provider scope is read."""
    raw = scope if scope is not None else request_data.get("atlas_scope")
    if not isinstance(raw, Mapping):
        raise AtlasInvalidRequestError("an explicit atlas_scope is required")
    kind = raw.get("kind")
    if kind not in _SCOPE_KINDS:
        raise AtlasInvalidRequestError("atlas_scope kind is unsupported")
    allowed = {"kind"}
    result: dict[str, Any] = {"kind": kind}
    if kind == "title":
        allowed.add("title_id")
        title_id = _clean_text(raw.get("title_id"))
        if not title_id:
            raise AtlasInvalidRequestError("title scope requires title_id")
        result["title_id"] = title_id
    elif kind == "titles":
        allowed.add("title_ids")
        title_ids = raw.get("title_ids")
        if not isinstance(title_ids, list) or not title_ids or any(not _clean_text(item) for item in title_ids):
            raise AtlasInvalidRequestError("titles scope requires non-empty title_ids")
        cleaned = [_clean_text(item) for item in title_ids]
        if len(set(cleaned)) != len(cleaned):
            raise AtlasInvalidRequestError("title_ids must be unique")
        result["title_ids"] = cleaned
    elif kind == "brand":
        allowed.add("brand_id")
        brand_id = _clean_text(raw.get("brand_id"))
        if not brand_id:
            raise AtlasInvalidRequestError("brand scope requires brand_id")
        result["brand_id"] = brand_id
    if set(raw) - allowed:
        raise AtlasInvalidRequestError("atlas_scope contains fields invalid for its kind")
    return result


def _orientation_preference(request_data: Mapping[str, Any]) -> str | None:
    values = (
        request_data.get("orientation"), request_data.get("aspect_ratio"),
        request_data.get("format"), request_data.get("video_orientation"),
    )
    normalized = {str(value or "").strip().lower().replace(" ", "") for value in values}
    if normalized & {"landscape", "16:9", "16/9", "horizontal"}:
        return "horizontal"
    if normalized & {"portrait", "9:16", "9/16", "vertical"}:
        return "vertical"
    return None


def _query_text(request_data: Mapping[str, Any]) -> str:
    for key in ("query", "query_text", "search_term", "term"):
        text = _clean_text(request_data.get(key))
        if text:
            return text
    return ""


def _validated_limit(request_data: Mapping[str, Any]) -> int:
    if "limit" not in request_data:
        return 20
    limit = request_data["limit"]
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise AtlasInvalidRequestError("Atlas limit must be an integer from 1 through 100")
    return limit


def build_atlas_search_payload(
    request_data: Mapping[str, Any], *, scope: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Map only query and orientation into Atlas' conservative search contract."""
    if not isinstance(request_data, Mapping):
        raise AtlasInvalidRequestError("Atlas request data must be a mapping")
    preferred = _orientation_preference(request_data)
    rendition = {
        "acceptable_kinds": ["horizontal", "vertical"],
        "preferred_kind": preferred,
        "preferred_required": False,
    }
    return {
        "schema_version": "asset_search_v1",
        "query": _query_text(request_data),
        "scope": _explicit_scope(request_data, scope),
        # SceneVisualIntent fields intentionally remain absent: they are not
        # established hard Atlas requirements at this integration boundary.
        "requirements": {"rendition": rendition},
        "preferences": {"preferred_rendition_kind": preferred},
        "unknown_policy": "exclude",
        "minimum_confidence": "high",
        "limit": _validated_limit(request_data),
    }


def validate_logical_locator(locator: str, asset_uid: str, rendition_kind: str, delivery: str) -> str:
    """Require exactly the public Atlas logical delivery path, with no URL parts."""
    uid = validate_asset_uid(asset_uid)
    kind = validate_rendition_kind(rendition_kind)
    if not isinstance(locator, str):
        raise AtlasProtocolError("Atlas logical locator must be a string")
    parsed = urlsplit(locator)
    expected = f"/v1/assets/{uid}/renditions/{kind}/{delivery}"
    if (
        parsed.scheme or parsed.netloc or parsed.query or parsed.fragment
        or parsed.path != expected or parsed.path != locator
        or ".." in parsed.path.split("/") or "\\" in locator
    ):
        raise AtlasProtocolError("Atlas returned an unsafe logical locator")
    return locator


def atlas_candidate_to_material_candidate(candidate: Mapping[str, Any], *, search_term: str, ordinal: int) -> MaterialCandidate:
    """Convert one frozen Atlas result without promoting its local score to rank."""
    if not isinstance(candidate, Mapping):
        raise AtlasProtocolError("Atlas candidate must be an object")
    try:
        uid = validate_asset_uid(candidate.get("asset_uid"))
    except AtlasInvalidRequestError as exc:
        raise AtlasProtocolError("Atlas candidate has an invalid asset_uid") from exc
    rendition = candidate.get("selected_rendition")
    if not isinstance(rendition, Mapping):
        raise AtlasProtocolError("Atlas candidate is missing selected_rendition")
    try:
        kind = validate_rendition_kind(rendition.get("kind"))
    except AtlasInvalidRequestError as exc:
        raise AtlasProtocolError("Atlas candidate has an invalid rendition kind") from exc
    content_locator = validate_logical_locator(rendition.get("content_locator"), uid, kind, "content")
    thumbnail_value = rendition.get("thumbnail_locator")
    thumbnail_locator = None if thumbnail_value is None else validate_logical_locator(thumbnail_value, uid, kind, "thumbnail")
    required = (
        "source_provenance", "catalog_placement", "confidence", "atlas_local_score",
        "score_components", "requirement_matches", "contradictions", "evidence",
    )
    if any(name not in candidate for name in required):
        raise AtlasProtocolError("Atlas candidate omits required frozen fields")
    if not isinstance(ordinal, int) or ordinal < 1:
        raise AtlasProtocolError("Atlas candidate ordinal must be positive")
    source_info = {
        "asset_uid": uid,
        "rendition_kind": kind,
        "provenance": candidate["source_provenance"],
        "catalog_placement": candidate["catalog_placement"],
        "confidence": candidate["confidence"],
        "atlas_local_score": candidate["atlas_local_score"],
        "score_components": candidate["score_components"],
        "requirement_matches": candidate["requirement_matches"],
        "contradictions": candidate["contradictions"],
        "evidence": candidate["evidence"],
        "content_locator": content_locator,
        "thumbnail_locator": thumbnail_locator,
    }
    return MaterialCandidate(
        provider="atlas",
        canonical_id=f"{uid}:{kind}",
        dedupe_key=f"atlas:{uid}:{kind}",
        search_term=search_term,
        rank=ordinal,
        url=None,
        orientation={"horizontal": "landscape", "vertical": "portrait"}[kind],
        source_info=source_info,
    )


class AtlasProvider:
    """A narrowly scoped Atlas candidate adapter; it performs no fallback."""

    provider = "atlas"

    def __init__(self, client: AtlasClient) -> None:
        self.client = client

    def search(self, request_data: Mapping[str, Any], *, scope: Mapping[str, Any] | None = None) -> list[MaterialCandidate]:
        payload = build_atlas_search_payload(request_data, scope=scope)
        response = self.client.search(payload)
        candidates = response.get("candidates")
        if not isinstance(candidates, list):
            raise AtlasProtocolError("Atlas search response has invalid candidates")
        return [
            atlas_candidate_to_material_candidate(item, search_term=payload["query"], ordinal=index)
            for index, item in enumerate(candidates, start=1)
        ]
