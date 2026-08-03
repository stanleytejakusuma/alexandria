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
    """Lazy sentence-transformers cross encoder for top-N reranking."""

    def __init__(self, model: str = "BAAI/bge-reranker-v2-m3") -> None:
        self.model_name = model
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
        self._model = CrossEncoder(self.model_name)
        return self._model
