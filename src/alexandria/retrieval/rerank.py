"""Cross-encoder reranking with an identity implementation for offline tests."""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..model_load import DEFAULT_COOLDOWN, DEFAULT_LOAD_TIMEOUT, load_with_timeout

__all__ = ["CrossEncoderReranker", "IdentityReranker", "RerankCandidate", "Reranker"]

# Keyed by (model_name, half_precision): the underlying torch CrossEncoder,
# shared across every CrossEncoderReranker instance in the process. Loading
# is what's expensive (multi-second cold start); constructing a wrapper
# instance is not, and any code path that builds a second SearchEngine in
# one process (§5.8's "the server can rebuild in place", or simply several
# tests in one pytest run) must not pay the load cost -- or repeat it at
# all. Repeated same-process load/discard of the torch model was observed
# to destabilize the MPS backend (a segfault) under back-to-back test runs
# each constructing their own CrossEncoderReranker.
_MODEL_CACHE: dict[tuple[str, bool], object] = {}
# The failure-cooldown side of this lives in model_load.py now (shared, keyed
# facility -- see its module docstring and DEFAULT_COOLDOWN), not here: the
# first fix's per-site mechanisms (module cache for the reranker, per-instance
# memoization for the embedders) diverged and the embedder one was WRONG for
# serve's long-lived instance shape (Red review, 2026-08-20). One policy, in
# one place, by construction.
# Sharing one torch model across ServeContext instances (each with its own
# engine_lock) means the engine_lock no longer serializes every caller of
# that model -- two different SearchEngines, or leftover daemon threads
# from a prior request, can invoke it concurrently. PyTorch's MPS backend
# is not safe against concurrent kernel launches from multiple Python
# threads (observed as a segfault under back-to-back test runs); this lock
# guards the actual model, at the resource, independent of how many
# engines/callers reference it.
_MODEL_LOCK = threading.Lock()


@dataclass(frozen=True)
class RerankCandidate:
    chunk_id: str
    text: str
    score: float


@runtime_checkable
class Reranker(Protocol):
    def rerank(self, query: str, candidates: list[RerankCandidate], k: int) -> list[RerankCandidate]: ...


class IdentityReranker:
    """Preserve fusion order exactly; used by every offline test."""

    def rerank(self, query: str, candidates: list[RerankCandidate], k: int) -> list[RerankCandidate]:
        del query
        return list(candidates[:max(0, k)])


class CrossEncoderReranker:
    """Lazy sentence-transformers cross encoder for top-N reranking.

    Runs in half precision by default. Measured on this corpus with 20 real
    candidates: 2138ms fp32 -> 685ms fp16, a 3.12x speedup with **byte-identical
    top-5 ordering** and no NaNs. That combination is what makes it safe to default
    on -- every other latency lever measured (truncating candidates, a smaller
    reranker, reranking fewer candidates) bought speed by changing results:

        truncate to 256 tokens : 1.9x faster, 1 of 5 results changed
        bge-reranker-base      : 3.3x faster, 3 of 5 results changed
        prefetch 20 -> 10      : 2x faster,   20% of results changed

    The reranker genuinely earns its cost: 5 of 25 final results across five real
    queries came from fusion ranks 11-20, i.e. reranking reaches past what fusion
    alone would have returned.

    Set half_precision=False to force fp32 (some models emit NaNs in fp16 -- see
    huggingface/sentence-transformers#3498 for the Qwen3 embedding case; this
    reranker was explicitly verified NaN-free).
    """

    def __init__(self, model: str = "BAAI/bge-reranker-v2-m3", *,
                 half_precision: bool | None = None, load_timeout: float = DEFAULT_LOAD_TIMEOUT,
                 cooldown: float = DEFAULT_COOLDOWN) -> None:
        self.model_name = model
        # R6: None means AUTO -- resolved per-device at load time (fp16 on
        # CUDA/MPS, fp32 on bare CPU). The old hard True default encoded an
        # Apple-Silicon measurement (3.12x) that actively backfires on
        # NAS-class x86 CPUs, which emulate fp16 in software. Explicit
        # True/False still wins over everything; ALEXANDRIA_RERANK_HALF
        # overrides auto without code changes.
        self.half_precision = half_precision
        self.load_timeout = load_timeout
        self.cooldown = cooldown
        self._model = None

    def _resolve_half(self) -> bool:
        """Resolve the tri-state to a concrete precision.

        Precedence: explicit constructor arg > ALEXANDRIA_RERANK_HALF env >
        device auto-detect at load time. A malformed env value raises here
        -- loud refusal beats silently picking a default (the same policy
        as ALEXANDRIA_ANSWER_TIMEOUT's startup validation)."""
        if isinstance(self.half_precision, bool):
            return self.half_precision
        raw = os.environ.get("ALEXANDRIA_RERANK_HALF", "").strip().lower()
        if raw:
            if raw in ("on", "true", "1"):
                return True
            if raw in ("off", "false", "0"):
                return False
            raise ValueError(
                f"ALEXANDRIA_RERANK_HALF must be on|off (got {raw!r})")
        import torch  # deferred: only needed when auto actually resolves

        return bool(torch.cuda.is_available() or (
            getattr(torch.backends, "mps", None) is not None
            and torch.backends.mps.is_available()))

    def rerank(self, query: str, candidates: list[RerankCandidate], k: int) -> list[RerankCandidate]:
        if not candidates or k < 1:
            return []
        with _MODEL_LOCK:
            scores = self._load().predict([(query, candidate.text) for candidate in candidates])
        ranked = [
            RerankCandidate(candidate.chunk_id, candidate.text, float(score))
            for candidate, score in zip(candidates, scores, strict=True)
        ]
        ordered = sorted(enumerate(ranked), key=lambda item: (-item[1].score, item[0]))[:k]
        return [item[1] for item in ordered]

    def _load(self):
        if self._model is not None:
            return self._model
        # Resolve ONCE per instance, before the cache key: auto must not
        # flip between ticks (a device that appears mid-process would
        # otherwise build a second model under a different key).
        self.half_precision = self._resolve_half()
        cache_key = (self.model_name, self.half_precision)
        cached = _MODEL_CACHE.get(cache_key)
        if cached is not None:
            self._model = cached
            return self._model
        try:
            from sentence_transformers import CrossEncoder
        except ImportError as exc:  # pragma: no cover - exercised in installed runtime
            raise RuntimeError("cross-encoder reranking requires sentence-transformers") from exc
        # #44 + the CI-hang fix (2026-08-20): CrossEncoder(...) issues several
        # sequential, individually-bounded HTTP requests with no bound on the
        # TOTAL -- a slow (not absent) network hangs the caller's first query
        # for minutes. Bounding it here turns that hang into a raised
        # ModelLoadTimeout, which propagates through rerank() unswallowed so
        # search.py's existing try/except (already correct for any reranker
        # failure) catches it and degrades to fusion order. The keyed cooldown
        # in model_load.py makes a persistently-dead network cost ONE
        # attempt per cooldown window, not one per caller -- the exact
        # compounding bug that hung CI twice.
        model = load_with_timeout(
            lambda: CrossEncoder(self.model_name),
            timeout=self.load_timeout,
            description=f"reranker model {self.model_name!r}",
            key=f"reranker:{self.model_name}:{self.half_precision}",
            cooldown=self.cooldown)
        if self.half_precision:
            try:
                model.model.half()
            except Exception:  # pragma: no cover - fp16 unsupported on this backend
                pass           # fp32 is correct, just slower -- never fail a query
        self._model = model
        _MODEL_CACHE[cache_key] = model
        return self._model
