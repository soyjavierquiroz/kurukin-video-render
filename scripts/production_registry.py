"""Atomic, content-addressed registry for completed batch productions.

This module deliberately knows nothing about the renderer.  Callers provide the
project's MP4 validator and duration reader so registry reuse has exactly the
same validation contract as normal production.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import time
import unicodedata
from typing import Any, Callable


def normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def identity(
    title: str,
    audio: Path,
    script: Path,
    material_title: str = "",
    production_recipe_version: str = "",
) -> dict[str, str]:
    audio_sha256 = sha256_file(audio)
    script_sha256 = sha256_file(script)
    payload = {
        "material_title": normalize(material_title),
        "title": normalize(title),
        "audio_sha256": audio_sha256,
        "script_sha256": script_sha256,
        "production_recipe_version": str(production_recipe_version or ""),
    }
    fingerprint = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "production_fingerprint": fingerprint,
        "normalized_title": payload["title"],
        "original_title": title,
        "material_title": material_title,
        "audio_sha256": audio_sha256,
        "script_sha256": script_sha256,
        "production_recipe_version": payload["production_recipe_version"],
    }


class ProductionRegistry:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS completed_productions (
                    production_fingerprint TEXT PRIMARY KEY,
                    normalized_title TEXT NOT NULL,
                    original_title TEXT NOT NULL,
                    material_title TEXT NOT NULL,
                    audio_sha256 TEXT NOT NULL,
                    script_sha256 TEXT NOT NULL,
                    final_mp4_path TEXT NOT NULL,
                    batch_id TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    final_duration REAL NOT NULL,
                    final_size INTEGER NOT NULL,
                    status TEXT NOT NULL
                )
            """)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(completed_productions)")}
            if "production_recipe_version" not in columns:
                conn.execute("ALTER TABLE completed_productions ADD COLUMN production_recipe_version TEXT NOT NULL DEFAULT ''")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def find_valid(self, fingerprint: str, valid_mp4: Callable[[Path], bool]) -> sqlite3.Row | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM completed_productions WHERE production_fingerprint=? AND status='completed'",
                (fingerprint,),
            ).fetchone()
            if row is None:
                return None
            if valid_mp4(Path(row["final_mp4_path"])):
                return row
            conn.execute("DELETE FROM completed_productions WHERE production_fingerprint=?", (fingerprint,))
        return None

    def upsert(self, record: dict[str, str], final_mp4: Path, batch_id: str, duration: float) -> bool:
        """Register a completed output; return False when it was already identical."""
        values = (*[record[key] for key in (
            "production_fingerprint", "normalized_title", "original_title", "material_title", "audio_sha256", "script_sha256", "production_recipe_version"
        )], final_mp4.resolve().as_posix(), batch_id, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), duration,
                  final_mp4.stat().st_size, "completed")
        with self._connect() as conn:
            existing = conn.execute("SELECT 1 FROM completed_productions WHERE production_fingerprint=?", (record["production_fingerprint"],)).fetchone()
            conn.execute("""
                INSERT INTO completed_productions
                (production_fingerprint, normalized_title, original_title, material_title, audio_sha256, script_sha256, production_recipe_version,
                 final_mp4_path, batch_id, completed_at, final_duration, final_size, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(production_fingerprint) DO UPDATE SET
                  normalized_title=excluded.normalized_title, original_title=excluded.original_title,
                  material_title=excluded.material_title, audio_sha256=excluded.audio_sha256,
                  script_sha256=excluded.script_sha256, production_recipe_version=excluded.production_recipe_version, final_mp4_path=excluded.final_mp4_path,
                  batch_id=excluded.batch_id, completed_at=excluded.completed_at,
                  final_duration=excluded.final_duration, final_size=excluded.final_size, status='completed'
            """, values)
        return existing is None
