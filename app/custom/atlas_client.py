"""Small, synchronous client for Atlas' frozen asset API.

Atlas delivery locators are intentionally *not* accepted at this boundary.
The only durable delivery identity is an asset UUID plus a rendition kind.
"""

from __future__ import annotations

import uuid
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

import requests


RENDITION_KINDS = frozenset({"horizontal", "vertical"})
_SCARCITY_REASONS = frozenset({
    "no_candidates_in_scope", "no_requirement_match",
    "no_high_confidence_match", "no_acceptable_rendition",
})


class AtlasError(RuntimeError):
    """Base class for errors returned by or detected around Atlas."""


class AtlasInvalidRequestError(AtlasError):
    """The request cannot be accepted by Atlas."""


class AtlasUnavailableError(AtlasError):
    """Atlas is temporarily unavailable or has exhausted capacity."""


class AtlasServerError(AtlasError):
    """Atlas returned an unexpected server-side failure."""


class AtlasNotFoundError(AtlasError):
    """The requested Atlas asset or rendition does not exist."""


class AtlasProtocolError(AtlasError):
    """Atlas returned a response that does not satisfy the frozen contract."""


def validate_asset_uid(asset_uid: str) -> str:
    """Return a canonical UUID string or raise ``AtlasInvalidRequestError``."""
    if not isinstance(asset_uid, str):
        raise AtlasInvalidRequestError("asset_uid must be a UUID string")
    try:
        return str(uuid.UUID(asset_uid))
    except (ValueError, AttributeError, TypeError) as exc:
        raise AtlasInvalidRequestError("asset_uid must be a UUID string") from exc


def validate_rendition_kind(rendition_kind: str) -> str:
    if rendition_kind not in RENDITION_KINDS:
        raise AtlasInvalidRequestError("rendition_kind must be horizontal or vertical")
    return rendition_kind


def normalize_base_url(base_url: str) -> str:
    """Validate an Atlas origin/base path and remove its trailing slash."""
    if not isinstance(base_url, str) or not base_url.strip():
        raise AtlasInvalidRequestError("base_url must be a non-empty http(s) URL")
    try:
        parsed = urlsplit(base_url.strip())
        # Accessing .port is deliberate: malformed ports otherwise survive
        # urlsplit and produce surprising requests exceptions later.
        _ = parsed.port
    except ValueError as exc:
        raise AtlasInvalidRequestError("base_url is invalid") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise AtlasInvalidRequestError("base_url must use http or https")
    if parsed.username is not None or parsed.password is not None:
        raise AtlasInvalidRequestError("base_url must not contain credentials")
    if parsed.query or parsed.fragment:
        raise AtlasInvalidRequestError("base_url must not contain query or fragment")
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


class AtlasClient:
    """Requests-based Atlas client with an injectable session for tests."""

    def __init__(
        self,
        base_url: str,
        timeout_seconds: float = 10.0,
        session: requests.Session | Any | None = None,
    ) -> None:
        if not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
            raise AtlasInvalidRequestError("timeout_seconds must be positive")
        self.base_url = normalize_base_url(base_url)
        self.timeout_seconds = float(timeout_seconds)
        self.session = session if session is not None else requests.Session()

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def public_thumbnail_url(
        self,
        asset_uid: str,
        rendition_kind: str,
        thumbnail_locator: str,
    ) -> str:
        """Resolve only Atlas' exact public logical thumbnail endpoint.

        Discovery carries this logical path without turning it into a content
        URL.  Human Review can safely expose the resulting public thumbnail
        URL, but must not accept an arbitrary URL or a content locator here.
        """
        uid = validate_asset_uid(asset_uid)
        kind = validate_rendition_kind(rendition_kind)
        if not isinstance(thumbnail_locator, str):
            raise AtlasInvalidRequestError("thumbnail_locator must be a string")
        parsed = urlsplit(thumbnail_locator)
        expected = f"/v1/assets/{uid}/renditions/{kind}/thumbnail"
        if (
            parsed.scheme or parsed.netloc or parsed.query or parsed.fragment
            or parsed.path != expected or parsed.path != thumbnail_locator
            or ".." in parsed.path.split("/") or "\\" in thumbnail_locator
        ):
            raise AtlasInvalidRequestError("thumbnail_locator is not an Atlas public logical thumbnail")
        return self._url(thumbnail_locator)

    @staticmethod
    def _payload(payload: Any) -> Any:
        if hasattr(payload, "model_dump"):
            return payload.model_dump(mode="json")
        if isinstance(payload, Mapping):
            return dict(payload)
        raise AtlasInvalidRequestError("search payload must be a mapping or contract model")

    @staticmethod
    def _error_message(response: Any) -> str:
        try:
            body = response.json()
        except (ValueError, TypeError, AttributeError):
            return f"Atlas returned HTTP {getattr(response, 'status_code', 'unknown')}"
        if isinstance(body, Mapping) and isinstance(body.get("message"), str):
            return body["message"]
        return f"Atlas returned HTTP {getattr(response, 'status_code', 'unknown')}"

    def _raise_for_status(self, response: Any) -> None:
        status = getattr(response, "status_code", None)
        if not isinstance(status, int):
            raise AtlasProtocolError("Atlas response has no integer status_code")
        if 200 <= status < 300:
            return
        message = self._error_message(response)
        if status in {400, 401, 403, 422}:
            raise AtlasInvalidRequestError(message)
        if status == 404:
            raise AtlasNotFoundError(message)
        if status in {429, 503}:
            raise AtlasUnavailableError(message)
        if 500 <= status < 600:
            raise AtlasServerError(message)
        raise AtlasProtocolError(f"unexpected Atlas HTTP status {status}: {message}")

    @staticmethod
    def _validate_search_response(body: Any) -> dict[str, Any]:
        if not isinstance(body, Mapping):
            raise AtlasProtocolError("Atlas search response must be an object")
        if body.get("schema_version") != "asset_candidates_v1":
            raise AtlasProtocolError("Atlas search response has an invalid schema_version")
        candidates = body.get("candidates")
        if not isinstance(candidates, list) or not all(isinstance(item, Mapping) for item in candidates):
            raise AtlasProtocolError("Atlas search response has invalid candidates")
        scarcity_reason = body.get("scarcity_reason")
        if scarcity_reason is not None and scarcity_reason not in _SCARCITY_REASONS:
            raise AtlasProtocolError("Atlas search response has an invalid scarcity_reason")
        allowed_candidate_fields = {
            "asset_uid", "producer", "source_provenance", "catalog_placement",
            "selected_rendition", "evidence", "requirement_matches",
            "contradictions", "confidence", "score_components", "atlas_local_score",
        }
        required_candidate_fields = allowed_candidate_fields - {"contradictions"}
        normalized_candidates: list[dict[str, Any]] = []
        for item in candidates:
            candidate = dict(item)
            if set(candidate) - allowed_candidate_fields or not required_candidate_fields <= set(candidate):
                raise AtlasProtocolError("Atlas candidate does not match the frozen contract")
            candidate.setdefault("contradictions", [])
            try:
                validate_asset_uid(candidate["asset_uid"])
            except AtlasInvalidRequestError as exc:
                raise AtlasProtocolError("Atlas candidate has an invalid asset_uid") from exc
            rendition = candidate["selected_rendition"]
            if not isinstance(rendition, Mapping):
                raise AtlasProtocolError("Atlas candidate rendition is invalid")
            try:
                validate_rendition_kind(rendition.get("kind"))
            except AtlasInvalidRequestError as exc:
                raise AtlasProtocolError("Atlas candidate rendition is invalid") from exc
            if not isinstance(rendition.get("content_locator"), str):
                raise AtlasProtocolError("Atlas candidate content locator is invalid")
            if rendition.get("thumbnail_locator") is not None and not isinstance(rendition.get("thumbnail_locator"), str):
                raise AtlasProtocolError("Atlas candidate thumbnail locator is invalid")
            if (
                not isinstance(candidate["producer"], str)
                or not isinstance(candidate["source_provenance"], Mapping)
                or not isinstance(candidate["catalog_placement"], Mapping)
                or not isinstance(candidate["evidence"], Mapping)
                or not isinstance(candidate["requirement_matches"], Mapping)
                or not isinstance(candidate["score_components"], Mapping)
                or not isinstance(candidate["contradictions"], list)
                or not all(isinstance(value, str) for value in candidate["contradictions"])
            ):
                raise AtlasProtocolError("Atlas candidate has invalid structured fields")
            if candidate["confidence"] not in {"high", "uncertain"}:
                raise AtlasProtocolError("Atlas candidate confidence is invalid")
            if not isinstance(candidate["atlas_local_score"], (int, float)) or isinstance(candidate["atlas_local_score"], bool):
                raise AtlasProtocolError("Atlas candidate local score is invalid")
            normalized_candidates.append(candidate)
        result = dict(body)
        result["candidates"] = normalized_candidates
        return result

    def search(self, payload: Any) -> dict[str, Any]:
        """POST a frozen search payload; an HTTP 200 scarcity is returned normally."""
        try:
            response = self.session.post(
                self._url("/v1/assets/search"),
                json=self._payload(payload),
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise AtlasUnavailableError("Atlas search request failed") from exc
        self._raise_for_status(response)
        try:
            body = response.json()
        except (ValueError, TypeError, AttributeError) as exc:
            raise AtlasProtocolError("Atlas search response is not JSON") from exc
        return self._validate_search_response(body)

    def _open_delivery(self, asset_uid: str, rendition_kind: str, suffix: str) -> Any:
        uid = validate_asset_uid(asset_uid)
        kind = validate_rendition_kind(rendition_kind)
        try:
            response = self.session.get(
                self._url(f"/v1/assets/{uid}/renditions/{kind}/{suffix}"),
                timeout=self.timeout_seconds,
                stream=True,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise AtlasUnavailableError("Atlas delivery request failed") from exc
        try:
            self._raise_for_status(response)
        except AtlasError:
            # A streamed requests response owns a socket even before its body
            # is consumed.  Error responses never reach a caller, so release
            # that socket here without replacing the useful typed error.
            close = getattr(response, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
            raise
        return response

    def open_content(self, asset_uid: str, rendition_kind: str) -> Any:
        return self._open_delivery(asset_uid, rendition_kind, "content")

    def open_thumbnail(self, asset_uid: str, rendition_kind: str) -> Any:
        return self._open_delivery(asset_uid, rendition_kind, "thumbnail")
