"""Safe streaming materialization for Atlas content delivery."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from app.custom.atlas_client import AtlasClient, validate_asset_uid, validate_rendition_kind


_SHA256_ETAG = re.compile(r"^[0-9a-fA-F]{64}$")


class AtlasMaterializationError(RuntimeError):
    """A delivery response could not be safely materialized."""


def _header(response: Any, name: str) -> str | None:
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    try:
        value = headers.get(name)
        if value is None:
            value = headers.get(name.lower())
    except AttributeError:
        value = None
    return value if isinstance(value, str) else None


def _validate_content_response(response: Any) -> tuple[int | None, str]:
    status = getattr(response, "status_code", None)
    if status != 200:
        raise AtlasMaterializationError("Atlas content response must have HTTP 200")
    if bool(getattr(response, "is_redirect", False)) or 300 <= status < 400:
        raise AtlasMaterializationError("Atlas content response must not redirect")
    content_type = _header(response, "Content-Type")
    if not content_type or content_type.split(";", 1)[0].strip().lower() != "video/mp4":
        raise AtlasMaterializationError("Atlas content response must be video/mp4")
    raw_length = _header(response, "Content-Length")
    length: int | None = None
    if raw_length is not None:
        if not raw_length.isdecimal():
            raise AtlasMaterializationError("Atlas Content-Length is invalid")
        length = int(raw_length)
    etag = _header(response, "ETag")
    if etag is None or len(etag) < 2 or not (etag.startswith('"') and etag.endswith('"')):
        raise AtlasMaterializationError("Atlas ETag must be quoted")
    sha256 = etag[1:-1]
    if not _SHA256_ETAG.fullmatch(sha256):
        raise AtlasMaterializationError("Atlas ETag must contain a SHA-256 digest")
    return length, sha256


class AtlasMaterializer:
    """Downloads Atlas video only through UUID/rendition reconstruction."""

    def __init__(self, client: AtlasClient, *, chunk_size: int = 64 * 1024) -> None:
        if not isinstance(chunk_size, int) or chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        self.client = client
        self.chunk_size = chunk_size

    def materialize(self, asset_uid: str, rendition_kind: str, destination_directory: str | Path) -> Path:
        uid = validate_asset_uid(asset_uid)
        kind = validate_rendition_kind(rendition_kind)
        directory = Path(destination_directory)
        directory.mkdir(parents=True, exist_ok=True)
        if not directory.is_dir():
            raise AtlasMaterializationError("destination_directory is not a directory")
        destination = directory / f"atlas-{uid}-{kind}.mp4"
        response = self.client.open_content(uid, kind)
        temporary_path: Path | None = None
        try:
            content_length, expected_hash = _validate_content_response(response)
            written = 0
            digest = hashlib.sha256()
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=f".{destination.name}.", suffix=".tmp", dir=directory, delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                for chunk in response.iter_content(chunk_size=self.chunk_size):
                    if not chunk:
                        continue
                    if not isinstance(chunk, bytes):
                        raise AtlasMaterializationError("Atlas stream yielded a non-bytes chunk")
                    temporary.write(chunk)
                    written += len(chunk)
                    digest.update(chunk)
            if content_length is not None and written != content_length:
                raise AtlasMaterializationError("Atlas Content-Length does not match streamed bytes")
            if digest.hexdigest().lower() != expected_hash.lower():
                raise AtlasMaterializationError("Atlas SHA-256 ETag does not match streamed content")
            os.replace(temporary_path, destination)
            temporary_path = None
            return destination
        except Exception:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()

    def open_thumbnail(self, asset_uid: str, rendition_kind: str) -> Any:
        """Return Atlas' safely reconstructed thumbnail stream for future callers."""
        return self.client.open_thumbnail(validate_asset_uid(asset_uid), validate_rendition_kind(rendition_kind))
