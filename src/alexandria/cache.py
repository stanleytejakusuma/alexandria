"""Query + response caches for the Alexandria pipeline (embedding cache
lives in the CachedEmbedder; prompt-cache accounting is gateway-side).

Query cache: exact (whitespace-normalized) query -> top-k results, TTL'd.
Response cache: normalized question -> rendered answer page, TTL'd.
Both are sqlite keyed by sha256, append-only stats, never raise.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

QUERY_TTL = 24 * 3600          # 1 day: corpus changes slowly; long enough
                               # for the weekly loop's repeated queries
RESPONSE_TTL = 7 * 24 * 3600   # 1 week: answers drift with the corpus

# Red release-changes (2026-08-09): keys are versioned so a schema or
# pipeline change invalidates old payloads instead of replaying them.
QUERY_SCHEMA_VER = "q2"
# a3 adds an explicit answer-pipeline fingerprint.  a2 payloads lacked knobs
# that change the gathered evidence and native judging, so they must miss rather
# than be replayed under a different answer policy.
RESPONSE_SCHEMA_VER = "a3"
ANSWER_PIPELINE_SCHEMA_VER = "answer-pipeline-v1"
GENERATION_FILE = ".alexandria/index/generation.json"

_CACHE_DIR = ".alexandria/cache"
_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS cache ("
    "key TEXT PRIMARY KEY, payload TEXT NOT NULL, ts REAL NOT NULL)"
)


def canonical(obj) -> str:
    """Versioned canonical serialization for cache-key parts: recursively
    normalized JSON with deterministic ordering and safe rendering of
    arbitrary values (sets, datetimes, floats). NOT repr()."""
    def _norm(v):
        if isinstance(v, dict):
            return {str(k): _norm(val) for k, val in v.items()}
        if isinstance(v, (list, tuple)):
            return [_norm(x) for x in v]
        if isinstance(v, set):
            return sorted(_norm(x) for x in v)
        if isinstance(v, float):
            return repr(v)  # deterministic, locale-independent
        return str(v)
    return json.dumps(_norm(obj), sort_keys=True, separators=(",", ":"))


class GenerationFileCorrupt(Exception):
    """The generation file exists but could not be parsed.

    Deliberately distinct from "never indexed" (missing file -> generation 0,
    unchanged below). SPEC-write-path-and-serve.md F2: silently falling back
    to 0 on a corrupt-but-present file is a resurrection bug, not a safe
    default -- the next write_index_generation() call would then write 1, and
    every cache entry keyed to the real past generations 1..N becomes valid
    again. Stale answers resurface instead of staying invalidated, and a
    rerun entrenches the problem rather than repairing it. Callers on the hot
    read path (SearchEngine._generation) catch this and disable caching for
    that call rather than crash retrieval; write_index_generation refuses to
    guess and lets this propagate, because guessing here is exactly the bug.
    """


def read_index_generation(corpus: str | Path) -> int:
    """Corpus generation counter written atomically after each successful
    index build. Caches keyed with it are invalidated on any reindex."""
    path = Path(corpus).expanduser() / GENERATION_FILE
    if not path.exists():
        return 0
    try:
        data = json.loads(path.read_text())
        return int(data["generation"])
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise GenerationFileCorrupt(f"{path} exists but could not be parsed: {exc}") from exc


def write_index_generation(corpus: str | Path) -> int:
    """Bump and persist the generation counter; returns the new value.

    Locked and atomic end to end (SPEC F2/W3b):
    - fcntl.flock (POSIX; this project targets macOS/Linux, same as the mlx
      embedder path) serializes the read-modify-write across processes, so a
      concurrent index build and a concurrent promote can't race each other
      into writing the same next generation number or losing an increment.
    - the new value is written to a sibling temp file and moved into place
      with os.replace(), which is an atomic rename on POSIX. The previous bare
      path.write_text() could leave a truncated/partial file on a crash
      mid-write; read_index_generation's old behaviour then silently reset
      that to generation 0 -- the exact resurrection bug GenerationFileCorrupt
      now refuses to paper over.
    """
    path = Path(corpus).expanduser() / GENERATION_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(".lock")
    with open(lock_path, "a+") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            gen = read_index_generation(corpus) + 1
            tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
            tmp.write_text(json.dumps({
                "generation": gen,
                "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            }, sort_keys=True) + "\n", encoding="utf-8")
            os.replace(tmp, path)
            return gen
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def _db(corpus: str | Path, name: str) -> sqlite3.Connection:
    d = Path(corpus).expanduser() / _CACHE_DIR
    d.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(d / name, check_same_thread=False)
    # See index/bm25.py §3.1: wait for a concurrent writer instead of raising
    # "database is locked" immediately.
    con.execute("PRAGMA busy_timeout=5000")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute(_SCHEMA)
    con.commit()
    return con


def _key(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def normalize_query(query: str) -> str:
    return " ".join(query.split())


def answer_pipeline_fingerprint(*, grader_a_model: str, grader_b_model: str,
                                base_url: str | None, api_key_env: str | None,
                                retrieval: object,
                                max_follow_up_queries: int,
                                audit_concurrency: int) -> dict[str, object]:
    """Stable, reviewable answer semantics used by ``ResponseCache``.

    Each member can change evidence gathered or whether the native synthesis
    checks emit a page. ``base_url`` and the configured credential *name* select
    the LLM service/account without storing a credential value. Caller identity
    and output destination cannot affect page semantics, so remain outside it.
    """
    return {
        "schema": ANSWER_PIPELINE_SCHEMA_VER,
        "grader_a_model": grader_a_model,
        "grader_b_model": grader_b_model,
        "base_url": base_url,
        "api_key_env": api_key_env,
        "retrieval": retrieval,
        "max_follow_up_queries": max_follow_up_queries,
        "audit_concurrency": audit_concurrency,
    }


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

    def key(self, query: str, k: int, config: object, filters: object = None,
            generation: int = 0) -> str:
        return _key(QUERY_SCHEMA_VER, "q", normalize_query(query), str(k),
                    canonical(config), canonical(filters or {}),
                    str(generation))


class ResponseCache(_Cache):
    def __init__(self, corpus: str | Path) -> None:
        super().__init__(corpus, "responses_cache.sqlite", RESPONSE_TTL)

    def key(self, question: str, model: str, k: int, prompt_version: str,
            generation: int = 0, *, pipeline: object = None) -> str:
        """Key an answer by all stable policy that can change its evidence or gate.

        ``pipeline`` is deliberately explicit rather than an ambient object: its
        canonical serialization is deterministic, makes additions reviewable, and
        lets a cache entry from an old policy miss safely.  The caller supplies
        models and bounded controls that affect gathering and judging; request
        transport/authentication and audit identity are intentionally excluded.
        """
        return _key(RESPONSE_SCHEMA_VER, "a", normalize_query(question), model,
                    str(k), prompt_version, canonical(pipeline or {
                        "schema": ANSWER_PIPELINE_SCHEMA_VER,
                    }), str(generation))
