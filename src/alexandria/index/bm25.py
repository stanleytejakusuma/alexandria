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
        # busy_timeout makes a concurrent writer WAIT for the lock instead of
        # raising "database is locked" immediately -- the literal prerequisite
        # for a second writer (SPEC-write-path-and-serve.md §3.1). journal_mode
        # is verified, not assumed: WAL can silently fail to engage on some
        # filesystems (e.g. certain network mounts), and a caller depending on
        # concurrent readers-during-write needs to know if it didn't.
        self.connection.execute("PRAGMA busy_timeout=5000")
        self.connection.execute("PRAGMA journal_mode=WAL")
        mode = self.connection.execute("PRAGMA journal_mode").fetchone()[0]
        self.wal_active = str(mode).lower() == "wal"
        self.connection.execute("CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(chunk_id UNINDEXED, text)")
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS chunk_metadata ("
            "chunk_id TEXT PRIMARY KEY, type TEXT, project TEXT, status TEXT, source TEXT, tags TEXT NOT NULL, "
            "entities TEXT NOT NULL, layer TEXT NOT NULL, generated_at TEXT)"
        )
        self.connection.commit()

    # chunks_fts declares chunk_id UNINDEXED, so there is no index to resolve
    # `WHERE chunk_id = ?` -- SQLite scans the whole FTS table for each one.
    # Measured 2026-08-11 at 40,960 rows: 84.7 ms per lookup, i.e. ~347 s of pure
    # scanning for a single 4096-row batch, growing with the table. Deleting the
    # whole batch in one statement turns N scans into one, so the cost per batch
    # stops depending on the batch size. Chunked because SQLite caps host
    # parameters per statement.
    _DELETE_CHUNK = 900

    def index(self, chunks: Iterable[Mapping[str, Any]], *,
              append_only: bool = False) -> None:
        records = [dict(chunk) for chunk in chunks]
        if append_only:
            # A rebuild drops chunks_fts first, so every DELETE below would scan a
            # table that provably cannot hold the id -- batching the scan makes it
            # cheap but not free, and it is still O(table) per flush. Skip it.
            # chunk_metadata's ON CONFLICT keeps re-insertion safe there; chunks_fts
            # has no key, so the de-duplication the DELETE would have provided is
            # done here instead (last wins, matching upsert semantics).
            records = list({chunk["chunk_id"]: chunk for chunk in records}.values())
        with self.connection:
            ids = [] if append_only else [chunk["chunk_id"] for chunk in records]
            for start in range(0, len(ids), self._DELETE_CHUNK):
                batch = ids[start:start + self._DELETE_CHUNK]
                placeholders = ",".join("?" * len(batch))
                self.connection.execute(
                    f"DELETE FROM chunks_fts WHERE chunk_id IN ({placeholders})", batch)
            for chunk in records:
                # Index the heading breadcrumb ALONGSIDE the body. The chunker moves
                # headings out of `text` and into `heading_path`, so indexing text
                # alone made every document title and section heading unsearchable --
                # a systemic recall hole, since a note's title is often its most
                # information-dense line. Measured: a document titled '...frontmatter
                # isolation pinning on all agent types' ranked >200 for a query that
                # was nearly its verbatim title.
                self.connection.execute(
                    "INSERT INTO chunks_fts(chunk_id, text) VALUES (?, ?)",
                    (chunk["chunk_id"], searchable_text(chunk)),
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


def searchable_text(chunk: Mapping[str, Any]) -> str:
    """Body prefixed with its heading breadcrumb, for lexical and dense indexing alike.

    This is the cheap, deterministic half of "contextual retrieval": a chunk carries
    the structural context it came from, so 'retry behaviour' under a 'Payments
    service' heading is findable by either term.
    """
    heading = str(chunk.get("heading_path") or "").strip()
    body = str(chunk.get("text") or "")
    return f"{heading}\n\n{body}" if heading else body


def fts_query(query: str) -> str | None:
    """Return a safe FTS5 OR expression, never raw user query syntax.

    OR, not AND. Pure AND makes BM25 a filter rather than a ranker: every incidental
    word in a natural question becomes mandatory, so one absent term drops an
    otherwise perfect document to no-match. Measured on golden-v1 -- a document
    literally titled 'consult-memory-before-building' ranked below 200 for the query
    'consult memory before building anything new', purely because 'anything' and
    'new' were required. Three of five golden misses had this shape.

    OR is safe here precisely because FTS5's bm25() ranking already rewards documents
    matching more terms, and rarer terms more strongly. The failure mode of naive OR
    (every token optional, so a long question matches almost anything) is a *ranking*
    concern, and ranking is exactly what bm25() plus the downstream reranker handle.
    """
    tokens = [token.casefold() for token in WORD_RE.findall(query)
              if token.casefold() not in STOP_WORDS]
    if not tokens:
        return None
    # Quoted individual tokens keep FTS operators, quotes, and prefix markers literal.
    return " OR ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)


def json_dumps_list(value: Any) -> str:
    return json.dumps([str(item) for item in (value or [])], separators=(",", ":"))
