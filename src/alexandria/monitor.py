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
            scores: Sequence[float], latency_ms: float, cache_hit: bool, client: str) -> bool:
        """Append a query record, returning false rather than disrupting retrieval on error."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self.path) as connection:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS queries ("
                    "query_id TEXT PRIMARY KEY, ts TEXT NOT NULL, q TEXT NOT NULL, filters TEXT NOT NULL, "
                    "tier TEXT NOT NULL, retrieved_ids TEXT NOT NULL, scores TEXT NOT NULL, latency_ms REAL NOT NULL, "
                    "cache_hit INTEGER NOT NULL, client TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO queries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (str(uuid.uuid4()), datetime.now(timezone.utc).isoformat(), query,
                     json.dumps(dict(filters), sort_keys=True), tier, json.dumps(list(retrieved_ids)),
                     json.dumps(list(scores)), float(latency_ms), int(cache_hit), client),
                )
            return True
        except (OSError, sqlite3.Error):
            return False
