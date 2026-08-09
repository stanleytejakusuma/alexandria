"""Query + response caches for the Alexandria pipeline (embedding cache
lives in the CachedEmbedder; prompt-cache accounting is gateway-side).

Query cache: exact (whitespace-normalized) query -> top-k results, TTL'd.
Response cache: normalized question -> rendered answer page, TTL'd.
Both are sqlite keyed by sha256, append-only stats, never raise.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

QUERY_TTL = 24 * 3600          # 1 day: corpus changes slowly; long enough
                               # for the weekly loop's repeated queries
RESPONSE_TTL = 7 * 24 * 3600   # 1 week: answers drift with the corpus

_CACHE_DIR = ".alexandria/cache"
_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS cache ("
    "key TEXT PRIMARY KEY, payload TEXT NOT NULL, ts REAL NOT NULL)"
)


def _db(corpus: str | Path, name: str) -> sqlite3.Connection:
    d = Path(corpus).expanduser() / _CACHE_DIR
    d.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(d / name)
    con.execute(_SCHEMA)
    con.commit()
    return con


def _key(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def normalize_query(query: str) -> str:
    return " ".join(query.split())


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    size: int = 0
    oldest: float | None = None
    newest: float | None = None

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0


class _Cache:
    def __init__(self, corpus: str | Path, name: str, ttl: int) -> None:
        self.con = _db(corpus, name)
        self.ttl = ttl
        self.errors: list[str] = []

    def get(self, key: str):
        try:
            row = self.con.execute(
                "SELECT payload, ts FROM cache WHERE key = ?", (key,)).fetchone()
            if row is None:
                return None
            payload, ts = row
            if time.time() - ts > self.ttl:
                return None
            return json.loads(payload)
        except Exception as exc:  # cache must never break the pipeline
            self.errors.append(f"{type(exc).__name__}: {exc}")
            return None

    def put(self, key: str, payload) -> None:
        try:
            self.con.execute(
                "INSERT INTO cache (key, payload, ts) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET payload=excluded.payload, "
                "ts=excluded.ts",
                (key, json.dumps(payload), time.time()))
            self.con.commit()
        except Exception as exc:
            self.errors.append(f"{type(exc).__name__}: {exc}")

    def stats(self) -> CacheStats:
        st = CacheStats()
        try:
            row = self.con.execute(
                "SELECT COUNT(*), MIN(ts), MAX(ts) FROM cache").fetchone()
            st.size, st.oldest, st.newest = row
        except Exception as exc:
            self.errors.append(f"{type(exc).__name__}: {exc}")
        return st

    def clear(self) -> int:
        try:
            n = self.con.execute("SELECT COUNT(*) FROM cache").fetchone()[0]
            self.con.execute("DELETE FROM cache")
            self.con.commit()
            return n
        except Exception as exc:
            self.errors.append(f"{type(exc).__name__}: {exc}")
            return 0


class QueryCache(_Cache):
    def __init__(self, corpus: str | Path) -> None:
        super().__init__(corpus, "queries_cache.sqlite", QUERY_TTL)

    def key(self, query: str, k: int, config_sig: str,
            filters_sig: str = "") -> str:
        return _key("q", normalize_query(query), str(k), config_sig, filters_sig)


class ResponseCache(_Cache):
    def __init__(self, corpus: str | Path) -> None:
        super().__init__(corpus, "responses_cache.sqlite", RESPONSE_TTL)

    def key(self, question: str, model: str, k: int, prompt_version: str) -> str:
        return _key("a", normalize_query(question), model, str(k), prompt_version)
