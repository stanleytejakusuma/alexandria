"""Pluggable local embeddings with a content-addressed SQLite cache."""

from __future__ import annotations

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

__all__ = ["CachedEmbedder", "Embedder", "HashEmbedder", "LocalEmbedder", "MLXEmbedder"]


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
                 device: str | None = None, max_length: int = DEFAULT_MAX_LENGTH) -> None:
        self.model_name = model
        self.batch_size = batch_size
        self.device = device
        self.max_length = max_length
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
        self._model = SentenceTransformer(self.model_name, device=device)
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
                 batch_size: int = 32, max_length: int = DEFAULT_MAX_LENGTH) -> None:
        self.model_name = model
        self.batch_size = batch_size
        self.max_length = max_length
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
        self._model, self._processor = load(self.model_name)
        self._generate = generate


class CachedEmbedder:
    """Cache embeddings by ``sha256(model_name + '\\n' + text)``.

    Normal mode keeps entries durable across interrupted index runs; corrupt
    values are ignored and overwritten by a fresh provider call. ``read_only``
    mode uses an existing cache for hits, computes misses without persisting
    them, and never creates a cache file, parent directory, or SQLite sidecar.
    """

    def __init__(self, provider: Embedder, cache_path: str | Path, *, progress_every: int = 250,
                 progress_stream=None, on_progress: Callable[[dict], None] | None = None,
                 read_only: bool = False) -> None:
        self.provider = provider
        self.cache_path = Path(cache_path)
        self.read_only = read_only
        self._connection: sqlite3.Connection | None
        if read_only:
            # ``mode=ro`` alone can create ``-wal``/``-shm`` sidecars while reading
            # a WAL database. ``immutable=1`` prevents those writes as well as DDL
            # and INSERTs. It is safe for this short-lived evaluation reader: it may
            # use an older cache snapshot if a normal indexing writer races it, which
            # only turns a would-be hit into a computed (never persisted) miss.
            if self.cache_path.is_file():
                uri = f"{self.cache_path.resolve().as_uri()}?mode=ro&immutable=1"
                try:
                    self._connection = sqlite3.connect(uri, uri=True, check_same_thread=False)
                    self._connection.execute("PRAGMA busy_timeout=5000")
                except sqlite3.Error:
                    # A cache can disappear between is_file() and connect while an
                    # indexer rotates it. Fall back to uncached computation rather
                    # than creating or repairing anything in this read-only path.
                    self._connection = None
            else:
                # Do not create cache directories or a SQLite database just to read.
                self._connection = None
        else:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._connection = sqlite3.connect(self.cache_path, check_same_thread=False)
            # See index/bm25.py §3.1: wait for a concurrent writer instead of raising
            # "database is locked" immediately.
            self._connection.execute("PRAGMA busy_timeout=5000")
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS embeddings (cache_key TEXT PRIMARY KEY, vector TEXT NOT NULL)"
            )
            self._connection.commit()
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
                    vectors[index] = cached
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
            with self._lock:
                for (key, (_, indexes)), vector in zip(missing, produced, strict=True):
                    clean = [float(value) for value in vector]
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
            f"{self.name}\\n{self.revision}\\n{mode}\\n{text}".encode("utf-8")
        ).hexdigest()

    def _read(self, key: str) -> list[float] | None:
        if self._connection is None:
            return None
        try:
            row = self._connection.execute(
                "SELECT vector FROM embeddings WHERE cache_key = ?", (key,)
            ).fetchone()
        except sqlite3.Error:
            # A read-only evaluator must not repair a missing/corrupt schema. Treat
            # it like any other cache miss and calculate the vector without writing.
            return None
        if row is None:
            return None
        try:
            parsed = json.loads(row[0])
            if not isinstance(parsed, list) or not all(isinstance(value, (int, float)) for value in parsed):
                raise ValueError("not a numeric vector")
            return [float(value) for value in parsed]
        except (TypeError, ValueError, json.JSONDecodeError):
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
