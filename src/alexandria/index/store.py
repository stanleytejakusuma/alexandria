"""LanceDB vector storage with an offline SQLite compatibility fallback."""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from .filtering import deleted_flag, lancedb_where, normalize_filters, not_deleted_clause, sqlite_where

__all__ = ["VectorStore"]


# "deleted" is a SCALAR_FIELDS member, not a frontmatter-only convenience --
# see docs/SPEC-data-model-and-ambient-capture.md §D4a. A flag that lives only
# in document frontmatter survives `store.upsert`'s field projection (the
# `_normalise_record` return at the bottom of this file keeps only ALL_FIELDS)
# by being silently dropped before the row is written, so the index keeps
# serving a "deleted" chunk forever. Declaring it here is what makes
# `not_deleted_clause` (index/filtering.py) enforceable at all.
SCALAR_FIELDS = ("chunk_id", "doc_id", "text", "heading_path", "type", "project", "status",
                 "source", "layer", "generated_at", "deleted")
ALL_FIELDS = (*SCALAR_FIELDS, "vector", "tags", "entities", "enrichment", "kind", "parent_doc", "target_chunk")


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
            self._db.create_table(self.table_name, data=records, schema=_lance_schema(records))
            return
        # Red 2026-08-09: detect old LanceDB schemas before issuing field
        # projections. A table created before the enrichment columns existed
        # silently drops them on merge; the fix is an explicit rebuild.
        # "deleted" (_normalise_record always supplies "true"/"false", never
        # None) joins this guard for the same reason: an old table missing the
        # column would otherwise merge_insert new rows with no `deleted` value
        # at all -- update_all only touches fields present in `records`, but a
        # brand-new column on an old table is the enrichment-column failure
        # mode over again, so refuse loudly rather than let a tombstone write
        # silently miss the column it depends on.
        existing = {field.name for field in table.schema}
        if any(records[0].get(field) is not None
               for field in ("enrichment", "kind", "parent_doc", "target_chunk", "deleted")):
            missing = [field for field in ("enrichment", "kind", "parent_doc", "target_chunk", "deleted")
                       if field not in existing]
            if missing:
                raise RuntimeError(
                    "index schema predates enrichment/deleted columns "
                    f"({', '.join(missing)}); run `alexandria index --rebuild`")
        merger = table.merge_insert("chunk_id")
        merger.when_matched_update_all().when_not_matched_insert_all().execute(records)

    def append(self, chunks: Iterable[Mapping[str, Any]]) -> None:
        """Insert rows known to be new. Only valid straight after drop().

        merge_insert scans the table for matching chunk_ids on every call, so its
        cost grows with table size and a full rebuild is O(n^2) in the number of
        rows. After drop() every row is new by construction and that scan can only
        ever find nothing, so it is pure waste. Measured on the real 124,751-chunk
        corpus: throughput decayed 480 -> 256 chunks/min as the table passed 90k
        rows, with a projected 4h tail.

        The caller owns the precondition that chunk_ids are unique -- append cannot
        deduplicate the way merge_insert does. cmd_index enforces it once, up front,
        before the rebuild starts (a per-batch check would not catch a collision
        between two different batches).
        """
        records = [_normalise_record(chunk) for chunk in chunks]
        if not records:
            return
        if self._fallback is not None:
            self._fallback.upsert(records)
            return
        table = self._open_table()  # pragma: no cover - requires optional dependency
        if table is None:
            self._db.create_table(self.table_name, data=records, schema=_lance_schema(records))
            return
        table.add(records)

    def search_vector(self, query_vec: list[float], k: int, where: Mapping[str, Any] | None = None) -> list[dict]:
        if k < 1:
            return []
        if self._fallback is not None:
            return self._fallback.search_vector(query_vec, k, where)
        table = self._open_table()  # pragma: no cover - requires optional dependency
        if table is None:
            return []
        search = table.search(query_vec)
        # not_deleted_clause is applied with prefilter=True so a deleted row
        # never occupies one of the k slots in the first place (a post-hoc
        # filter after limit() would silently shrink the candidate pool
        # instead). See index/filtering.py for why this is an allow-list, not a
        # negated deny-list.
        #
        # But a table that PREDATES the `deleted` column has no tombstones to
        # hide -- the column's absence means nothing was ever soft-deleted, and
        # referencing it would make the whole dense leg error and silently
        # degrade retrieval to lexical-only. Skip the tombstone predicate in
        # that case rather than fail a leg over a column that never existed.
        user_predicate = lancedb_where(where)
        has_deleted = "deleted" in {field.name for field in table.schema}
        if has_deleted:
            predicate = (f"{not_deleted_clause()} AND ({user_predicate})"
                         if user_predicate else not_deleted_clause())
        else:
            predicate = user_predicate
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

    def mark_deleted(self, doc_id: str, deleted: bool) -> int:
        """Flip `deleted` on every stored row belonging to doc_id, returning the
        number of rows updated.

        Selects the affected set by stable identity, not by current
        content-derived chunk ids: `doc_id = doc_id` catches ordinary chunks
        (including rows indexed under a PREVIOUS chunk_id after the document was
        edited), and `parent_doc = doc_id` catches synthetic enrichment rows
        whose target_chunk points back into the document. This is the whole of
        SOL-01/SOL-02 -- delete must converge on every row a document ever
        produced, not only the rows its current body regenerates.
        """
        if self._fallback is not None:
            return self._fallback.mark_deleted(doc_id, deleted)
        table = self._open_table()  # pragma: no cover - requires optional dependency at runtime
        if table is None:
            return 0
        existing = {field.name for field in table.schema}
        if "deleted" not in existing:
            # A tombstone cannot be projected onto a table that predates the
            # column; silently touching only the lexical leg would leave the
            # dense leg serving the document. Fail loudly with the same
            # rebuild instruction upsert() uses for the same schema gap.
            raise RuntimeError(
                "index schema predates the deleted column; run "
                "`alexandria index --rebuild` before soft-deleting")
        literal = json.dumps(str(doc_id))
        result = table.update(
            where=f"doc_id = {literal} OR parent_doc = {literal}",
            values={"deleted": deleted_flag(deleted)},
        )
        return int(result.rows_updated)

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
        # See index/bm25.py §3.1: wait for a concurrent writer instead of
        # raising "database is locked" immediately.
        self.connection.execute("PRAGMA busy_timeout=5000")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self._create()

    def _create(self) -> None:
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS chunks ("
            "chunk_id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, text TEXT NOT NULL, heading_path TEXT NOT NULL, "
            "vector TEXT NOT NULL, type TEXT, project TEXT, status TEXT, source TEXT, tags TEXT NOT NULL, "
            "entities TEXT NOT NULL, layer TEXT NOT NULL, generated_at TEXT,"
            " enrichment TEXT, kind TEXT, parent_doc TEXT, target_chunk TEXT,"
            " deleted TEXT NOT NULL DEFAULT 'false')"
        )
        # Red release change: enrich the schema in place for pre-existing DBs
        # (sqlite >= 3.35 supports ADD COLUMN IF NOT EXISTS).
        #
        # "deleted" gets a literal DEFAULT ('false'), not just a bare type --
        # SQLite backfills every EXISTING row with a constant default on
        # ADD COLUMN (unlike CURRENT_TIMESTAMP-style defaults, which cannot
        # backfill). That default is correct, not merely convenient: no row
        # could have been "deleted" before this column existed, since the
        # concept did not exist yet, so retroactively marking every
        # pre-migration row as visible loses nothing. A bare `ADD COLUMN
        # deleted TEXT` would instead leave every existing row NULL, and
        # `not_deleted_clause`'s fail-closed `= 'false'` predicate would then
        # hide the entire pre-migration corpus until the next full reindex.
        for column in ("enrichment TEXT", "kind TEXT", "parent_doc TEXT", "target_chunk TEXT",
                       "deleted TEXT NOT NULL DEFAULT 'false'"):
            try:
                self.connection.execute(f"ALTER TABLE chunks ADD COLUMN {column}")
            except sqlite3.OperationalError:
                pass  # already present
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
            f"SELECT {', '.join(ALL_FIELDS)} FROM chunks WHERE {not_deleted_clause()} AND {clause}", params
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

    def mark_deleted(self, doc_id: str, deleted: bool) -> int:
        flag = deleted_flag(deleted)
        with self.connection:
            cur = self.connection.execute(
                "UPDATE chunks SET deleted = ? WHERE doc_id = ? OR parent_doc = ?",
                (flag, doc_id, doc_id),
            )
            return cur.rowcount

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
    for field in ("tags", "entities"):
        value = record.get(field) or []
        record[field] = [str(item) for item in value]
    record["vector"] = [float(value) for value in record["vector"]]
    # LanceDB (pinned by a live repro, originally found for enrichment/kind/
    # parent_doc/target_chunk, and reproduced again for type/project/status/
    # generated_at/source below): a table whose FIRST-EVER batch has every
    # value None for a nullable scalar column gets that column typed as
    # Arrow `null` (no inferable type), not nullable-string. The instant any
    # later batch supplies a real string for that column, merge_insert's
    # schema unification chokes and raises an opaque "Spill has sent an
    # error" instead of a real type error. Empty string is the safe neutral
    # value for every nullable text field here; routing code checks
    # truthiness, so "" behaves as absent exactly like None did.
    for field in ("type", "project", "status", "source", "generated_at",
                  "enrichment", "kind", "parent_doc", "target_chunk"):
        value = record.get(field)
        record[field] = value if value is not None else ""
    # See index/filtering.py:deleted_flag for the write-time default (missing
    # -> "false") and why it is intentionally more permissive than the
    # read-time fail-closed predicate.
    record["deleted"] = deleted_flag(record.get("deleted"))
    return {field: record.get(field) for field in ALL_FIELDS}


def _lance_schema(records: list[dict]):
    """Explicit PyArrow schema for a brand-new table.

    Do NOT rely on `create_table(data=records)`'s automatic type inference:
    when every value in the FIRST-EVER batch for a nullable text field is
    None, or every list is empty, PyArrow cannot infer a real type and picks
    Arrow's `null` type for that column (or `list<item: null>`). The column
    silently keeps that type for the table's lifetime. The instant a LATER
    batch supplies a real string/list value for it, merge_insert's schema
    unification chokes and lancedb raises an opaque "Spill has sent an
    error" instead of a real type error -- reproduced live via a plain
    single-document corpus (no frontmatter tags/type/project/status) indexed
    once, then a second unrelated fact remembered and promoted through
    `alexandria.promote.promote_pending`. Declaring the schema up front
    means every table starts with the type it will always need, regardless
    of what the first batch happens to contain.
    """
    import pyarrow as pa

    dim = len(records[0]["vector"]) if records else 0
    text_fields = ("chunk_id", "doc_id", "text", "heading_path", "type", "project",
                   "status", "source", "layer", "generated_at", "enrichment", "kind",
                   "parent_doc", "target_chunk", "deleted")
    fields = [pa.field(name, pa.string()) for name in text_fields]
    fields.append(pa.field("vector", pa.list_(pa.float32(), dim)))
    fields.append(pa.field("tags", pa.list_(pa.string())))
    fields.append(pa.field("entities", pa.list_(pa.string())))
    return pa.schema(fields)


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
