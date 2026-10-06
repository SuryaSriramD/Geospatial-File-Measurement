"""Transactional SQLite persistence with one connection per operation."""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.schemas import FeatureResult, FileInfo


class Repository:
    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / "geospatial.sqlite3"

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            with conn:
                yield conn
        finally:
            conn.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS files (
                    id TEXT PRIMARY KEY,
                    metadata TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS features (
                    file_id TEXT NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                    feature_index INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (file_id, feature_index)
                );
            """)

    def save(self, metadata: FileInfo, features: list[FeatureResult]) -> None:
        # Either the metadata and every feature are saved, or nothing is saved.
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO files(id, metadata) VALUES (?, ?)",
                (str(metadata.id), metadata.model_dump_json()),
            )
            conn.executemany(
                "INSERT INTO features(file_id, feature_index, payload) VALUES (?, ?, ?)",
                ((str(metadata.id), f.index, f.model_dump_json()) for f in features),
            )

    def get_file(self, file_id: str) -> dict | None:
        with self.connection() as conn:
            row = conn.execute("SELECT metadata FROM files WHERE id=?", (file_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def get_features(self, file_id: str, limit: int, offset: int) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT payload FROM features WHERE file_id=? "
                "ORDER BY feature_index LIMIT ? OFFSET ?",
                (file_id, limit, offset),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]
