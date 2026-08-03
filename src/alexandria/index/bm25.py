"""SQLite FTS5 lexical retrieval with the same metadata gate as dense search."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from .filtering import sqlite_where

__all__ = ["BM25Index"]

WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
STOP_WORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in", "is", "it",
    "of", "on", "or", "that", "the", "to", "was", "were", "what", "when", "where", "with",
})
METADATA_COLUMNS = ("type", "project", "status", "source", "tags", "entities", "layer", "generated_at")


class BM25Index:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(chunk_id UNINDEXED, text)")
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS chunk_metadata ("
            "chunk_id TEXT PRIMARY KEY, type TEXT, project TEXT, status TEXT, source TEXT, tags TEXT NOT NULL, "
            "entities TEXT NOT NULL, layer TEXT NOT NULL, generated_at TEXT)"
        )
        self.connection.commit()

    def index(self, chunks: Iterable[Mapping[str, Any]]) -> None:
        records = [dict(chunk) for chunk in chunks]
        with self.connection:
            for chunk in records:
                self.connection.execute("DELETE FROM chunks_fts WHERE chunk_id = ?", (chunk["chunk_id"],))
                self.connection.execute(
                    "INSERT INTO chunks_fts(chunk_id, text) VALUES (?, ?)",
                    (chunk["chunk_id"], chunk["text"]),
                )
                doc_id = str(chunk["doc_id"])
                layer = "wiki" if doc_id.startswith("wiki/") else "sources"
                values = [chunk.get(field) for field in ("type", "project", "status", "source")]
                values += [json_dumps_list(chunk.get("tags")), json_dumps_list(chunk.get("entities")),
                           layer, chunk.get("generated_at")]
                self.connection.execute(
                    "INSERT INTO chunk_metadata(chunk_id, type, project, status, source, tags, entities, layer, generated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(chunk_id) DO UPDATE SET type=excluded.type, project=excluded.project, "
                    "status=excluded.status, source=excluded.source, tags=excluded.tags, entities=excluded.entities, "
                    "layer=excluded.layer, generated_at=excluded.generated_at",
                    [chunk["chunk_id"], *values],
                )

    def search(self, query: str, k: int, where: Mapping[str, Any] | None = None) -> list[tuple[str, float]]:
        if k < 1:
            return []
        expression = fts_query(query)
        if expression is None:
            return []
        clause, params = sqlite_where(where, alias="m")
        rows = self.connection.execute(
            "SELECT f.chunk_id, -bm25(chunks_fts) AS score "
            "FROM chunks_fts AS f JOIN chunk_metadata AS m ON m.chunk_id = f.chunk_id "
            f"WHERE chunks_fts MATCH ? AND {clause} "
            "ORDER BY bm25(chunks_fts), f.chunk_id LIMIT ?",
            [expression, *params, k],
        ).fetchall()
        return [(str(chunk_id), float(score)) for chunk_id, score in rows]

    def drop(self) -> None:
        with self.connection:
            self.connection.execute("DELETE FROM chunks_fts")
            self.connection.execute("DELETE FROM chunk_metadata")


def fts_query(query: str) -> str | None:
    """Return an FTS5 AND expression, never raw user query syntax."""
    tokens = [token.casefold() for token in WORD_RE.findall(query) if token.casefold() not in STOP_WORDS]
    if not tokens:
        return None
    # Quoted individual tokens keep FTS operators, quotes, and prefix markers literal.
    return " AND ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)


def json_dumps_list(value: Any) -> str:
    return json.dumps([str(item) for item in (value or [])], separators=(",", ":"))
