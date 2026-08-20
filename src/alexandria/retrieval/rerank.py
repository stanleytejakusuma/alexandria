"""Cross-encoder reranking with an identity implementation for offline tests."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..model_load import DEFAULT_LOAD_TIMEOUT, ModelLoadTimeout, load_with_timeout

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
# THE BUG THAT HUNG CI (2026-08-20): a failed load was never remembered, so a
# persistently slow/unreachable network made EVERY caller independently
# re-pay the full load_timeout -- one 30s bound, multiplied by every search in
# a test suite (or every real query in production), compounded into an
# 11-minute-plus stall. Observed live: two consecutive CI runs on the same
# commit, both cancelled by the 30-minute job cap, the gap between test
# progress lines matching exactly. Same bug CLASS this repo already fixed
# once for LLM calls (backlog #28 -> #47, RequestDeadline): a per-call cap
# that does not compose across repeated callers is not actually a cap.
# Keyed identically to _MODEL_CACHE; value is the monotonic time the failure
# was recorded. A cache_key present here (and NOT in _MODEL_CACHE) means "do
# not attempt this again until the cooldown expires" -- checked BEFORE
# touching the network, so the second-and-later caller fails in microseconds,
# not by re-running the doomed load.
_FAILURE_CACHE: dict[tuple[str, bool], float] = {}
_FAILURE_COOLDOWN = 60.0  # a network blip must not become a process-lifetime outage
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
                 half_precision: bool = True, load_timeout: float = DEFAULT_LOAD_TIMEOUT) -> None:
        self.model_name = model
        self.half_precision = half_precision
        self.load_timeout = load_timeout
        self._model = None

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
        cache_key = (self.model_name, self.half_precision)
        cached = _MODEL_CACHE.get(cache_key)
        if cached is not None:
            self._model = cached
            return self._model
        # Fail FAST on a remembered recent failure -- checked before touching
        # the network at all -- instead of re-attempting a load that just
        # timed out. See _FAILURE_CACHE's module-level comment for the bug
        # this closes.
        failed_at = _FAILURE_CACHE.get(cache_key)
        if failed_at is not None and time.monotonic() - failed_at < _FAILURE_COOLDOWN:
            remaining = _FAILURE_COOLDOWN - (time.monotonic() - failed_at)
            raise ModelLoadTimeout(
                f"reranker model {self.model_name!r} failed to load "
                f"{time.monotonic() - failed_at:.0f}s ago and is in a "
                f"{remaining:.0f}s cooldown before retrying -- not re-attempting "
                f"a load that just failed")
        try:
            from sentence_transformers import CrossEncoder
        except ImportError as exc:  # pragma: no cover - exercised in installed runtime
            raise RuntimeError("cross-encoder reranking requires sentence-transformers") from exc
        # #44: CrossEncoder(...) issues several sequential, individually-bounded
        # HTTP requests with no bound on the TOTAL -- a slow (not absent)
        # network hangs the caller's first query for minutes. Bounding it here
        # turns that hang into a raised ModelLoadTimeout, which propagates
        # through rerank() unswallowed so search.py's existing try/except
        # (already correct for any reranker failure) catches it and degrades to
        # fusion order -- this function does not duplicate that fallback.
        try:
            model = load_with_timeout(
                lambda: CrossEncoder(self.model_name),
                timeout=self.load_timeout,
                description=f"reranker model {self.model_name!r}")
        except BaseException:
            _FAILURE_CACHE[cache_key] = time.monotonic()
            raise
        _FAILURE_CACHE.pop(cache_key, None)  # a later success clears any stale failure
        if self.half_precision:
            try:
                model.model.half()
            except Exception:  # pragma: no cover - fp16 unsupported on this backend
                pass           # fp32 is correct, just slower -- never fail a query
        self._model = model
        _MODEL_CACHE[cache_key] = model
        return self._model
