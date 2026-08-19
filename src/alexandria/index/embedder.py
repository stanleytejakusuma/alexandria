"""Pluggable local embeddings with a content-addressed SQLite cache."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import sqlite3
import sys
import time
from collections.abc import Callable
from pathlib import Path
from threading import Lock
from typing import Protocol, runtime_checkable

from ..model_load import DEFAULT_LOAD_TIMEOUT, load_with_timeout

__all__ = ["CachedEmbedder", "Embedder", "EmbeddingCacheBusy", "HashEmbedder",
           "LocalEmbedder", "MLXEmbedder"]


# CachedEmbedder is the sole vector boundary used by CLI indexing, promotion,
# and retrieval. It makes L2 normalization a code-enforced storage/query
# contract rather than a hardware-sensitive observation about a provider probe.
NORMALIZATION_POLICY = "l2"

# How long a normal (writing) cache waits for the cooperative cache lock before
# failing loudly. A read-only evaluator legitimately holds its shared snapshot
# for the whole of a leg-ablation run, so an unbounded LOCK_EX would let one
# evaluation wedge every later index run with no diagnostic -- the identical
# silent-stall failure writelock.py already refuses for the corpus lock. Same
# order of magnitude as that bounded wait, since a cache batch is sub-second.
DEFAULT_CACHE_LOCK_TIMEOUT = 30.0
_CACHE_LOCK_POLL = 0.05


class EmbeddingCacheBusy(RuntimeError):
    """A cache writer could not take the cooperative lock within its deadline."""


def _lock_exclusive(handle, timeout: float, what: str) -> None:
    """Bounded LOCK_EX: wait, then name the blocker instead of hanging."""
    deadline = time.monotonic() + max(timeout, 0.0)
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if time.monotonic() >= deadline:
                raise EmbeddingCacheBusy(
                    f"embedding cache lock held by an active read-only reader "
                    f"(evaluation/ablation) for more than {timeout:.0f}s while {what}; "
                    f"retry once that run finishes") from exc
            time.sleep(_CACHE_LOCK_POLL)


@runtime_checkable
class Embedder(Protocol):
    """An embedding implementation usable by the retrieval pipeline."""

    @property
    def dim(self) -> int: ...

    @property
    def name(self) -> str: ...

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class HashEmbedder:
    """Deterministic, dependency-free embeddings for offline tests and demos."""

    def __init__(self, dim: int = 384) -> None:
        if dim < 1:
            raise ValueError("embedding dimension must be positive")
        self._dim = dim

    @property
    def name(self) -> str:
        return f"hash-{self._dim}"

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        values: list[float] = []
        counter = 0
        seed = hashlib.sha256(text.encode("utf-8")).digest()
        while len(values) < self._dim:
            digest = hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
            for start in range(0, len(digest), 4):
                raw = int.from_bytes(digest[start:start + 4], "big")
                values.append((raw / 2**31) - 1.0)
                if len(values) == self._dim:
                    break
            counter += 1
        norm = math.sqrt(sum(value * value for value in values)) or 1.0
        return [value / norm for value in values]


# Grounded in measurement, not a guess: 603 tokens was the largest chunk this
# corpus's chunker ever produced (see tests/test_chunker.py). 640 gives headroom.
DEFAULT_MAX_LENGTH = 640

# Qwen3-Embedding is instruction-aware. This exact string ships in the model's own
# config_sentence_transformers.json as the "query" prompt -- but default_prompt_name
# is null, so nothing applies it unless explicitly requested, and we were not. The
# model card quantifies the omission: "not using an instruct on the query side can
# lead to a drop in retrieval performance by approximately 1% to 5%", and paraphrase
# queries (our measured weak spot) are precisely the trained-for case. Documents are
# embedded RAW -- the card is explicit: "No need to add instruction for retrieval
# documents." Note: no space after "Query:" (matches the official template), and the
# instruction stays in English even for non-English queries (also per the card).
# Pure text-prepend, so it works identically for the PyTorch and MLX providers.
QUERY_PREFIX = ("Instruct: Given a web search query, retrieve relevant passages "
                "that answer the query\nQuery:")


class LocalEmbedder:
    """Sentence-transformers embedding provider, loaded only when actually used.

    Pads every batch to a FIXED length rather than PyTorch/HF's default per-batch
    dynamic padding. This is a direct fix for a confirmed, currently-open PyTorch MPS
    bug (pytorch/pytorch#154329): the MPS backend compiles and permanently caches a
    GPU execution graph per distinct input shape, and torch.mps.empty_cache() does
    not touch that cache. Our chunks vary in length, so dynamic padding means nearly
    every batch is a new shape -- this was the measured cause of unbounded swap
    growth (21GB -> 30GB+) during a single indexing run. Fixed-length padding keeps
    the shape constant, so PyTorch only ever compiles one graph.

    Padding tokens are excluded from attention, so this does not change the
    resulting embedding values for real tokens -- only removes the shape variance
    that was triggering new graph compilation.
    """

    def __init__(self, model: str = "Qwen/Qwen3-Embedding-0.6B", batch_size: int = 32,
                 device: str | None = None, max_length: int = DEFAULT_MAX_LENGTH,
                 load_timeout: float = DEFAULT_LOAD_TIMEOUT) -> None:
        self.model_name = model
        self.batch_size = batch_size
        self.device = device
        self.max_length = max_length
        self.load_timeout = load_timeout
        self._model = None

    @property
    def name(self) -> str:
        return self.model_name

    @property
    def dim(self) -> int:
        return int(self._load().get_sentence_embedding_dimension())

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors = self._load().encode(
            texts,
            batch_size=self.batch_size,
            show_progress_bar=False,
            normalize_embeddings=True,
            processing_kwargs={"text": {"padding": "max_length",
                                        "max_length": self.max_length,
                                        "truncation": True}},
        )
        return [list(map(float, vector)) for vector in vectors]

    def _load(self):
        if self._model is not None:
            return self._model
        try:
            import torch
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:  # pragma: no cover - exercised in installed runtime
            raise RuntimeError("local embeddings require sentence-transformers") from exc
        device = self.device or _best_device(torch)
        # #44: unlike the reranker, an embedder must NEVER silently degrade --
        # there is no safe substitute for a real vector, and a garbage/zero
        # vector would poison the index or a query invisibly. This bounds the
        # load so a slow (not absent) network fails FAST AND LOUD with a
        # clear cause instead of hanging the caller's first index/search.
        self._model = load_with_timeout(
            lambda: SentenceTransformer(self.model_name, device=device),
            timeout=self.load_timeout,
            description=f"local embedder model {self.model_name!r} (provider: local)")
        return self._model


def _best_device(torch) -> str:
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


class MLXEmbedder:
    """Apple-native embeddings via MLX, as an alternative to PyTorch/MPS.

    PyTorch's MPS backend compiles and permanently caches a GPU graph per distinct
    input shape and never releases it (pytorch/pytorch#154329, open). A full index run
    on this corpus grew system swap by ~10GB as a result. MLX is Apple's own
    framework with a different memory model and no equivalent cache, so this provider
    sidesteps the bug rather than mitigating it.

    Uses 8-bit quantized weights by default: less memory and faster, at some cost in
    numeric fidelity versus fp32. Because the model name is part of the embedding
    cache key, switching providers correctly invalidates cached vectors rather than
    silently mixing quantized and full-precision embeddings in one index.
    """

    def __init__(self, model: str = "mlx-community/Qwen3-Embedding-0.6B-8bit",
                 batch_size: int = 32, max_length: int = DEFAULT_MAX_LENGTH,
                 load_timeout: float = DEFAULT_LOAD_TIMEOUT) -> None:
        self.model_name = model
        self.batch_size = batch_size
        self.max_length = max_length
        self.load_timeout = load_timeout
        self._model = None
        self._processor = None
        self._generate = None
        self._import_error: Exception | None = None

    @property
    def name(self) -> str:
        return self.model_name

    @property
    def dim(self) -> int:
        return len(self.embed(["probe"])[0])

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        self._load()
        vectors: list[list[float]] = []
        # Batched so a large call cannot materialise one enormous activation tensor.
        for start in range(0, len(texts), self.batch_size):
            batch = texts[start:start + self.batch_size]
            out = self._generate(self._model, self._processor, batch,
                                 max_length=self.max_length)
            vectors.extend([float(value) for value in row]
                           for row in out.text_embeds.tolist())
        return vectors

    def _load(self) -> None:
        if self._import_error is not None:
            raise RuntimeError(
                "MLX embeddings require the 'mlx' and 'mlx-embeddings' packages "
                f"({self._import_error})") from self._import_error
        if self._model is not None:
            return
        try:
            from mlx_embeddings import generate, load
        except ImportError as exc:  # pragma: no cover - exercised in installed runtime
            raise RuntimeError(
                "MLX embeddings require the 'mlx' and 'mlx-embeddings' packages") from exc
        # #44: same requirement as LocalEmbedder -- load(model_name) pulls
        # weights over the network with no native timeout, and there is no
        # safe substitute for a real vector, so this must fail fast and loud
        # on a slow/hung network rather than silently degrade or hang.
        self._model, self._processor = load_with_timeout(
            lambda: load(self.model_name),
            timeout=self.load_timeout,
            description=f"MLX embedder model {self.model_name!r} (provider: mlx)")
        self._generate = generate


def _l2_normalize(vector, *, expected_dim: int | None = None) -> list[float]:
    """Return a finite, nonzero vector at exactly the wrapper's L2 scale.

    ``math.hypot`` deliberately replaces ``sqrt(sum(x*x))`` here: the latter
    overflows for finite large values and underflows for finite tiny ones, both
    of which could turn a legitimate provider vector into an all-zero cached
    vector. The optional width check is part of the storage boundary too: a
    numeric legacy cache row is not compatible merely because it parses.
    """
    try:
        clean = [float(value) for value in vector]
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("embedder returned a non-numeric vector") from exc
    if not clean or not all(math.isfinite(value) for value in clean):
        raise ValueError("embedder returned an empty or non-finite vector")
    if expected_dim is not None and len(clean) != expected_dim:
        raise ValueError(
            f"embedder returned dimension {len(clean)}, expected {expected_dim}")
    norm = math.hypot(*clean)
    if not math.isfinite(norm) or norm == 0.0:
        raise ValueError("embedder returned a zero or unnormalizable vector")
    normalized = [value / norm for value in clean]
    normalized_norm = math.hypot(*normalized)
    if (not all(math.isfinite(value) for value in normalized)
            or normalized_norm == 0.0
            or not math.isclose(normalized_norm, 1.0, rel_tol=1e-12, abs_tol=1e-12)):
        raise ValueError("embedder produced an invalid L2-normalized vector")
    return normalized


class CachedEmbedder:
    """Cache embeddings by ``sha256(model_name + '\n' + text)`` and enforce L2.

    This common production boundary normalizes every returned vector before it
    can reach indexing or retrieval. Normal mode keeps cache entries durable;
    read-only mode uses existing hits but computes misses without persisting or
    creating cache files/SQLite sidecars (for evaluation-only callers).
    """

    def __init__(self, provider: Embedder, cache_path: str | Path, *, progress_every: int = 250,
                 progress_stream=None, on_progress: Callable[[dict], None] | None = None,
                 read_only: bool = False,
                 lock_timeout: float = DEFAULT_CACHE_LOCK_TIMEOUT) -> None:
        self.provider = provider
        self.cache_path = Path(cache_path)
        self._cache_lock_path = self.cache_path.with_name(f"{self.cache_path.name}.lock")
        self.read_only = read_only
        self.lock_timeout = lock_timeout
        # The read-only setup path can fail before sqlite3.connect() returns.
        # Keep a concrete sentinel so cleanup of that partial setup is safe.
        self._connection: sqlite3.Connection | None = None
        self._read_lock = None
        self._write_lock = None
        if read_only:
            # ``mode=ro`` alone can create ``-wal``/``-shm`` sidecars while reading
            # a WAL database. ``immutable=1`` prevents those writes as well as DDL
            # and INSERTs, but it assumes the database does not change underneath
            # it. Normal cache writes take this lock exclusively; the evaluator
            # holds it shared for its short lifetime. If a legacy cache lacks the
            # cooperative lock, compute uncached instead of trusting its live file.
            if self.cache_path.is_file() and self._cache_lock_path.is_file():
                try:
                    lock = open(self._cache_lock_path, "r")
                    fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    self._read_lock = lock
                    uri = f"{self.cache_path.resolve().as_uri()}?mode=ro&immutable=1"
                    self._connection = sqlite3.connect(uri, uri=True, check_same_thread=False)
                    self._connection.execute("PRAGMA busy_timeout=5000")
                except (OSError, sqlite3.Error):
                    # A cache/lock can rotate between check and open. Close the
                    # immutable connection *before* releasing its shared lock:
                    # SQLite close can touch WAL/checkpoint state, which must not
                    # overlap an exclusive normal writer. Then compute uncached;
                    # a read-only evaluator never repairs a cache.
                    if self._connection is not None:
                        self._connection.close()
                        self._connection = None
                    if self._read_lock is not None:
                        fcntl.flock(self._read_lock, fcntl.LOCK_UN)
                        self._read_lock.close()
                        self._read_lock = None
            else:
                # Do not create cache or lock directories just to evaluate.
                self._connection = None
        else:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_lock_path.touch(exist_ok=True)
            self._write_lock = open(self._cache_lock_path, "a+")
            _lock_exclusive(self._write_lock, lock_timeout, "opening the cache")
            try:
                self._connection = sqlite3.connect(self.cache_path, check_same_thread=False)
                # See index/bm25.py §3.1: wait for a concurrent writer instead of raising
                # "database is locked" immediately.
                self._connection.execute("PRAGMA busy_timeout=5000")
                self._connection.execute("PRAGMA journal_mode=WAL")
                self._connection.execute(
                    "CREATE TABLE IF NOT EXISTS embeddings (cache_key TEXT PRIMARY KEY, vector TEXT NOT NULL)"
                )
                self._connection.commit()
            finally:
                fcntl.flock(self._write_lock, fcntl.LOCK_UN)
        self._lock = Lock()
        self.progress_every = max(1, progress_every)
        self.progress_stream = progress_stream or sys.stderr
        self.on_progress = on_progress
        self.last_cache_stats = {"hits": 0, "misses": 0}

    @property
    def name(self) -> str:
        return self.provider.name

    @property
    def dim(self) -> int:
        return self.provider.dim

    @property
    def normalization_policy(self) -> str:
        """The normalization semantics this wrapper actually enforces.

        Provider output is allowed to vary by backend/device, but every vector
        this wrapper returns or persists is L2-normalized. The manifest records
        this declared, code-enforced policy as compatibility identity; a probe's
        observed norm remains useful diagnostics only.
        """
        return NORMALIZATION_POLICY

    def embed_queries(self, texts: list[str]) -> list[list[float]]:
        """Embed QUERIES: instruct-prefixed per the model's own template.

        Lives here (not per-provider) so every provider gets it identically. The
        cache key carries BOTH the explicit query mode token and the prefixed
        text (Red 2026-08-09: asymmetric models produce different vectors for
        the same text in query vs document space; mode must be part of the
        identity, never implicit)."""
        return self.embed([f"{QUERY_PREFIX}{text}" for text in texts], mode="q")

    def embed(self, texts: list[str], *, mode: str = "d") -> list[list[float]]:
        if not texts:
            self.last_cache_stats = {"hits": 0, "misses": 0}
            return []
        started = time.monotonic()
        keys = [self._key(text, mode) for text in texts]
        vectors: list[list[float] | None] = [None] * len(texts)
        pending: dict[str, tuple[str, list[int]]] = {}
        hits = 0
        with self._lock:
            for index, (text, key) in enumerate(zip(texts, keys, strict=True)):
                cached = self._read(key)
                if cached is not None:
                    try:
                        # Legacy rows can predate the wrapper policy. Normalize on
                        # every read, but only accept a vector with this provider's
                        # declared width; an invalid row is an ordinary cache miss.
                        vectors[index] = _l2_normalize(cached, expected_dim=self.dim)
                    except ValueError:
                        # A repeated bad cache key remains one provider request
                        # with every caller position restored from that result.
                        if key in pending:
                            pending[key][1].append(index)
                        else:
                            pending[key] = (text, [index])
                    else:
                        hits += 1
                elif key in pending:
                    pending[key][1].append(index)
                else:
                    pending[key] = (text, [index])
        missing = list(pending.items())
        if missing:
            produced = self.provider.embed([text for _, (text, _) in missing])
            if len(produced) != len(missing):
                raise RuntimeError("embedder returned a different number of vectors")
            # Validate the entire provider response before taking the SQL mutation
            # lock. No malformed vector may leave an earlier row of this batch in
            # the durable cache, and read-only callers get the same boundary.
            clean_produced = [
                _l2_normalize(vector, expected_dim=self.dim) for vector in produced
            ]
            with self._lock:
                if self._connection is not None and not self.read_only:
                    # An immutable evaluator owns a shared lock for its short
                    # lifetime. Wait before changing this SQLite file rather than
                    # violating immutable=1's no-concurrent-change precondition --
                    # but bounded, so a long-lived reader cannot wedge indexing.
                    _lock_exclusive(self._write_lock, self.lock_timeout,
                                    "persisting a cache batch")
                try:
                    if self._connection is not None and not self.read_only:
                        # A provider batch is one cache transaction. Prevalidation
                        # above prevents vector failures; this rollback additionally
                        # prevents disk/SQLite failures after the first SQL statement
                        # from becoming durable during a later successful call.
                        self._connection.execute("BEGIN")
                    for (key, (_, indexes)), clean in zip(missing, clean_produced, strict=True):
                        if self._connection is not None and not self.read_only:
                            self._connection.execute(
                                "INSERT INTO embeddings(cache_key, vector) VALUES(?, ?) "
                                "ON CONFLICT(cache_key) DO UPDATE SET vector=excluded.vector",
                                (key, json.dumps(clean, separators=(",", ":"))),
                            )
                        for index in indexes:
                            vectors[index] = clean
                    if self._connection is not None and not self.read_only:
                        self._connection.commit()
                except Exception:
                    if self._connection is not None and not self.read_only:
                        self._connection.rollback()
                    raise
                finally:
                    if self._connection is not None and not self.read_only:
                        fcntl.flock(self._write_lock, fcntl.LOCK_UN)
        self.last_cache_stats = {"hits": hits, "misses": len(texts) - hits}
        self._report_progress(len(texts), started)
        return [vector for vector in vectors if vector is not None]

    @property
    def revision(self) -> str:
        """Provider revision where one exists (pinned weights); '' means the
        provider name already pins the model (local weights)."""
        return getattr(self.provider, "revision", "")

    def _key(self, text: str, mode: str = "d") -> str:
        return hashlib.sha256(
            f"{self.name}\\n{self.revision}\\n{self.dim}\\n{self.normalization_policy}\\n"
            f"{mode}\\n{text}".encode("utf-8")
        ).hexdigest()

    def close(self) -> None:
        """Release the SQLite connection and any cooperative cache lock.

        SQLite may checkpoint/clean up WAL state while closing. A normal cache
        therefore holds its exclusive lock through close, just as it does for
        initialization and mutations; immutable readers release their snapshot
        only after closing the read-only connection.
        """
        with self._lock:
            if self._connection is not None and not self.read_only:
                _lock_exclusive(self._write_lock, self.lock_timeout, "closing the cache")
                try:
                    self._connection.close()
                    self._connection = None
                finally:
                    fcntl.flock(self._write_lock, fcntl.LOCK_UN)
            elif self._connection is not None:
                self._connection.close()
                self._connection = None
            if self._read_lock is not None:
                fcntl.flock(self._read_lock, fcntl.LOCK_UN)
                self._read_lock.close()
                self._read_lock = None
            if self._write_lock is not None:
                self._write_lock.close()
                self._write_lock = None

    def _read(self, key: str) -> list[float] | None:
        if self._connection is None:
            return None
        try:
            row = self._connection.execute(
                "SELECT vector FROM embeddings WHERE cache_key = ?", (key,)
            ).fetchone()
        except sqlite3.Error:
            # A read-only evaluator must not repair a missing/corrupt schema.
            return None
        if row is None:
            return None
        try:
            parsed = json.loads(row[0])
            if not isinstance(parsed, list) or not all(isinstance(value, (int, float)) for value in parsed):
                raise ValueError("not a numeric vector")
            return [float(value) for value in parsed]
        except (TypeError, ValueError, OverflowError, json.JSONDecodeError):
            return None

    def _report_progress(self, total: int, started: float) -> None:
        elapsed = max(time.monotonic() - started, 1e-9)
        rate = total / elapsed
        event = {
            "count": total,
            "rate_per_second": rate,
            "eta_seconds": 0.0,
            "cache": dict(self.last_cache_stats),
        }
        if self.on_progress is not None:
            self.on_progress(event)
        if total >= self.progress_every:
            print(f"embedding: {total} chunks, {rate * 60:.1f}/min, eta=0.0m "
                  f"(cache {self.last_cache_stats['hits']} hit/{self.last_cache_stats['misses']} miss)",
                  file=self.progress_stream, flush=True)
