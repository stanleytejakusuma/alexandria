"""LanceDB vector storage with an offline SQLite compatibility fallback."""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from .filtering import lancedb_where, normalize_filters, sqlite_where

__all__ = ["VectorStore"]


SCALAR_FIELDS = ("chunk_id", "doc_id", "text", "heading_path", "type", "project", "status",
                 "source", "layer", "generated_at")
ALL_FIELDS = (*SCALAR_FIELDS, "vector", "tags", "entities")


class VectorStore:
    """Chunk vectors and filterable metadata, persisted below the corpus index directory."""

    table_name = "chunks"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        self._lancedb = _try_lancedb()
        if self._lancedb is None:
            self._fallback = _SQLiteVectorStore(self.path / "fallback.sqlite")
            self._db = None
        else:  # pragma: no cover - requires optional dependency at runtime
            self._fallback = None
            self._db = self._lancedb.connect(str(self.path))

    def upsert(self, chunks: Iterable[Mapping[str, Any]]) -> None:
        records = [_normalise_record(chunk) for chunk in chunks]
        if not records:
            return
        if self._fallback is not None:
            self._fallback.upsert(records)
            return
        table = self._open_table()  # pragma: no cover - requires optional dependency
        if table is None:
            self._db.create_table(self.table_name, data=records)
            return
        merger = table.merge_insert("chunk_id")
        merger.when_matched_update_all().when_not_matched_insert_all().execute(records)

    def search_vector(self, query_vec: list[float], k: int, where: Mapping[str, Any] | None = None) -> list[dict]:
        if k < 1:
            return []
        if self._fallback is not None:
            return self._fallback.search_vector(query_vec, k, where)
        table = self._open_table()  # pragma: no cover - requires optional dependency
        if table is None:
            return []
        search = table.search(query_vec)
        predicate = lancedb_where(where)
        if predicate:
            search = search.where(predicate, prefilter=True)
        rows = search.limit(k).to_list()
        return [_record_from_lance(row) for row in rows]

    def get(self, chunk_id: str) -> dict | None:
        if self._fallback is not None:
            return self._fallback.get(chunk_id)
        table = self._open_table()  # pragma: no cover - requires optional dependency
        if table is None:
            return None
        literal = json.dumps(str(chunk_id))
        rows = table.search().where(f"chunk_id = {literal}", prefilter=True).limit(1).to_list()
        return _record_from_lance(rows[0]) if rows else None

    def get_many(self, chunk_ids: Iterable[str]) -> dict[str, dict]:
        """Fetch many records in ONE query.

        Fusion previously called get() per candidate -- up to 40 separate scans of the
        whole table per query, measured at ~494ms of pure overhead. Batching collapses
        that into a single predicate. Missing ids are simply absent from the result:
        callers already handle a candidate vanishing, and inventing a placeholder
        would put a fabricated record into the retrieval path.
        """
        ids = [str(chunk_id) for chunk_id in chunk_ids]
        if not ids:
            return {}
        if self._fallback is not None:
            found = {}
            for chunk_id in ids:
                record = self._fallback.get(chunk_id)
                if record is not None:
                    found[chunk_id] = record
            return found
        table = self._open_table()  # pragma: no cover - requires optional dependency
        if table is None:
            return {}
        predicate = ", ".join(json.dumps(chunk_id) for chunk_id in ids)
        rows = (table.search()
                .where(f"chunk_id IN ({predicate})", prefilter=True)
                .limit(len(ids))
                .to_list())
        return {row["chunk_id"]: _record_from_lance(row) for row in rows}

    def count(self) -> int:
        if self._fallback is not None:
            return self._fallback.count()
        table = self._open_table()  # pragma: no cover - requires optional dependency
        return int(table.count_rows()) if table is not None else 0

    def drop(self) -> None:
        if self._fallback is not None:
            self._fallback.drop()
            return
        if self._open_table() is not None:  # pragma: no cover - requires optional dependency
            self._db.drop_table(self.table_name)

    def _open_table(self):
        if self.table_name not in self._db.table_names():
            return None
        return self._db.open_table(self.table_name)


class _SQLiteVectorStore:
    """Minimal fallback for offline source tests when optional LanceDB is unavailable.

    Production always selects LanceDB once the declared dependency is installed; this
    keeps the project test suite network-free as required by the work order.
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self._create()

    def _create(self) -> None:
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS chunks ("
            "chunk_id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, text TEXT NOT NULL, heading_path TEXT NOT NULL, "
            "vector TEXT NOT NULL, type TEXT, project TEXT, status TEXT, source TEXT, tags TEXT NOT NULL, "
            "entities TEXT NOT NULL, layer TEXT NOT NULL, generated_at TEXT)"
        )
        self.connection.commit()

    def upsert(self, records: list[dict]) -> None:
        columns = ", ".join(ALL_FIELDS)
        placeholders = ", ".join("?" for _ in ALL_FIELDS)
        updates = ", ".join(f"{field}=excluded.{field}" for field in ALL_FIELDS if field != "chunk_id")
        values = [
            tuple(record[field] if field not in {"vector", "tags", "entities"}
                  else json.dumps(record[field], separators=(",", ":")) for field in ALL_FIELDS)
            for record in records
        ]
        self.connection.executemany(
            f"INSERT INTO chunks ({columns}) VALUES ({placeholders}) "
            f"ON CONFLICT(chunk_id) DO UPDATE SET {updates}", values
        )
        self.connection.commit()

    def search_vector(self, query_vec: list[float], k: int, where: Mapping[str, Any] | None) -> list[dict]:
        clause, params = sqlite_where(where)
        rows = self.connection.execute(
            f"SELECT {', '.join(ALL_FIELDS)} FROM chunks WHERE {clause}", params
        ).fetchall()
        scored = []
        for row in rows:
            record = _row_to_record(row)
            score = _cosine(query_vec, record["vector"])
            record["_distance"] = 1.0 - score
            scored.append(record)
        return sorted(scored, key=lambda row: (row["_distance"], row["chunk_id"]))[:k]

    def get(self, chunk_id: str) -> dict | None:
        row = self.connection.execute(
            f"SELECT {', '.join(ALL_FIELDS)} FROM chunks WHERE chunk_id = ?", (chunk_id,)
        ).fetchone()
        return _row_to_record(row) if row else None

    def count(self) -> int:
        return int(self.connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])

    def drop(self) -> None:
        self.connection.execute("DROP TABLE IF EXISTS chunks")
        self.connection.commit()
        self._create()


def _normalise_record(chunk: Mapping[str, Any]) -> dict:
    record = dict(chunk)
    required = {"chunk_id", "doc_id", "text", "heading_path", "vector"}
    missing = required - record.keys()
    if missing:
        raise ValueError(f"chunk record is missing: {', '.join(sorted(missing))}")
    doc_id = str(record["doc_id"])
    record["layer"] = "wiki" if doc_id.startswith("wiki/") else "sources"
    for field in ("type", "project", "status", "source", "generated_at"):
        record[field] = record.get(field)
    for field in ("tags", "entities"):
        value = record.get(field) or []
        record[field] = [str(item) for item in value]
    record["vector"] = [float(value) for value in record["vector"]]
    return {field: record.get(field) for field in ALL_FIELDS}


def _try_lancedb():
    try:
        import lancedb
    except ImportError:
        return None
    return lancedb


def _cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        raise ValueError("query vector dimension does not match stored vectors")
    denominator = math.sqrt(sum(value * value for value in left)) * math.sqrt(sum(value * value for value in right))
    return sum(a * b for a, b in zip(left, right, strict=True)) / denominator if denominator else 0.0


def _row_to_record(row) -> dict:
    record = dict(zip(ALL_FIELDS, row, strict=True))
    for field in ("vector", "tags", "entities"):
        record[field] = json.loads(record[field])
    return record


def _record_from_lance(row: Mapping[str, Any]) -> dict:
    result = dict(row)
    if "_distance" not in result:
        result["_distance"] = 0.0
    return result
