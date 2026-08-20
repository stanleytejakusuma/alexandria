"""Best-effort append-only query logging."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path

__all__ = ["QueryLogger"]


class QueryLogger:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def log(self, *, query: str, filters: Mapping, tier: str, retrieved_ids: Sequence[str],
            scores: Sequence[float], latency_ms: float, cache_hit: int, client: str) -> str | None:
        """Append a query record, returning the generated query_id on success or
        None on error (falsy either way -- `if not logger.log(...)` still reads
        correctly as "logging failed").

        #9/C1.1: returning the id (not a bare bool) makes this record JOINABLE
        to a later citation record written from the SAME search -- the id was
        always generated internally (uuid4 below) but discarded, so retrieval-
        time and answer-time records could not be linked even in principle
        (spec's own framing). The caller threads this id through to
        AuditLogger.answer's new query_id parameter (see auditlog.py)."""
        query_id = str(uuid.uuid4())
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as connection:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS queries ("
                    "query_id TEXT PRIMARY KEY, ts TEXT NOT NULL, q TEXT NOT NULL, filters TEXT NOT NULL, "
                    "tier TEXT NOT NULL, retrieved_ids TEXT NOT NULL, scores TEXT NOT NULL, latency_ms REAL NOT NULL, "
                    "cache_hit INTEGER NOT NULL, client TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO queries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (query_id, datetime.now(timezone.utc).isoformat(), query,
                     json.dumps(dict(filters), sort_keys=True), tier, json.dumps(list(retrieved_ids)),
                     json.dumps(list(scores)), float(latency_ms), int(cache_hit), client),
                )
            return query_id
        except (OSError, sqlite3.Error):
            return None

    def log_usage(self, *, query_id: str, model: str, prompt_tokens: int, completion_tokens: int,
                  total_tokens: int, cache_read: int = 0) -> bool:
        """Record LLM token usage for one /answer call, joinable to its query_id (SPEC F5).

        No dollar cost is computed or stored: this repo has no pricing table anywhere
        (verified by grep), and inventing one would fabricate a number nobody measured.
        Tokens are the durable fact; a rate file can turn them into cost later without
        needing to have existed at write time.
        """
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as connection:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS usage ("
                    "id TEXT PRIMARY KEY, query_id TEXT NOT NULL, ts TEXT NOT NULL, model TEXT NOT NULL, "
                    "prompt_tokens INTEGER NOT NULL, completion_tokens INTEGER NOT NULL, "
                    "total_tokens INTEGER NOT NULL, cache_read INTEGER NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO usage VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (str(uuid.uuid4()), query_id, datetime.now(timezone.utc).isoformat(), model,
                     int(prompt_tokens), int(completion_tokens), int(total_tokens), int(cache_read)),
                )
            return True
        except (OSError, sqlite3.Error):
            return False

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        # See index/bm25.py §3.1: wait for a concurrent writer instead of
        # raising "database is locked" immediately.
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA journal_mode=WAL")
        return connection
