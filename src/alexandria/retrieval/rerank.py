"""Cross-encoder reranking with an identity implementation for offline tests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

__all__ = ["CrossEncoderReranker", "IdentityReranker", "RerankCandidate", "Reranker"]


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
                 half_precision: bool = True) -> None:
        self.model_name = model
        self.half_precision = half_precision
        self._model = None

    def rerank(self, query: str, candidates: list[RerankCandidate], k: int) -> list[RerankCandidate]:
        if not candidates or k < 1:
            return []
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
        try:
            from sentence_transformers import CrossEncoder
        except ImportError as exc:  # pragma: no cover - exercised in installed runtime
            raise RuntimeError("cross-encoder reranking requires sentence-transformers") from exc
        model = CrossEncoder(self.model_name)
        if self.half_precision:
            try:
                model.model.half()
            except Exception:  # pragma: no cover - fp16 unsupported on this backend
                pass           # fp32 is correct, just slower -- never fail a query
        self._model = model
        return self._model
