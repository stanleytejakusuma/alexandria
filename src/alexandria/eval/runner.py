"""Execute a golden retrieval set and capture the context required to interpret it."""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .golden import GoldenEntry
from .metrics import EvalResult, EvalSummary, recall_at_k, reciprocal_rank, summarize

__all__ = ["EvalReport", "document_id", "run_eval", "score_of"]


@dataclass(frozen=True)
class EvalReport:
    results: list[EvalResult]
    summary: EvalSummary
    config: dict[str, Any]
    corpus_chunks: int | None
    timestamp: str
    git_sha: str
    negatives: list[EvalResult] = field(default_factory=list)
    """Results for queries the corpus cannot answer (BACKLOG #21). Optional so the
    ~1MB of history predating negative cases stays loadable."""
    separation: dict[str, Any] | None = None
    """SeparationReport.to_dict() when negatives ran, else None."""

    @property
    def config_fingerprint(self) -> dict[str, Any]:
        """Named alias that makes the report's comparison context explicit."""
        return self.config

    def to_dict(self) -> dict:
        return {
            "results": [result.to_dict() for result in self.results],
            "summary": self.summary.to_dict(),
            "config": self.config,
            "corpus_chunks": self.corpus_chunks,
            "timestamp": self.timestamp,
            "git_sha": self.git_sha,
            "negatives": [result.to_dict() for result in self.negatives],
            "separation": self.separation,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "EvalReport":
        return cls(
            results=[EvalResult.from_dict(value) for value in raw.get("results", [])],
            summary=EvalSummary.from_dict(raw["summary"]),
            config=dict(raw.get("config", {})),
            corpus_chunks=raw.get("corpus_chunks"),
            timestamp=str(raw["timestamp"]),
            git_sha=str(raw["git_sha"]),
            negatives=[EvalResult.from_dict(value) for value in raw.get("negatives", [])],
            separation=raw.get("separation"),
        )


def run_eval(engine, entries: list[GoldenEntry], *, k_override: int | None = None) -> EvalReport:
    """Run entries in their file order, preserving query failures as failed rows."""
    results: list[EvalResult] = []
    for entry in entries:
        k = entry.k if k_override is None else k_override
        started = time.perf_counter()
        try:
            raw_results = engine.search(entry.query, k=k)
            retrieved_ids = [document_id(result) for result in raw_results][:k] if k > 0 else []
            scores = tuple(score_of(result) for result in raw_results)[:k] if k > 0 else ()
            hit = recall_at_k(retrieved_ids, entry.must_retrieve, k)
            rank = _rank_at_k(retrieved_ids, entry.must_retrieve, k) if hit else 0
            error = None
        except Exception as exc:  # an eval must show a failed query, never drop it
            retrieved_ids = []
            scores = ()
            hit = False
            rank = 0
            error = f"{type(exc).__name__}: {exc}"
        latency_ms = round((time.perf_counter() - started) * 1000, 3)
        results.append(EvalResult(entry.id, entry.query, hit, rank, retrieved_ids, latency_ms, error,
                                  overlap_band=entry.overlap_band, scores=scores))

    return EvalReport(
        results=results,
        summary=summarize(results),
        config=_fingerprint(engine),
        corpus_chunks=_corpus_chunks(engine),
        timestamp=datetime.now(timezone.utc).isoformat(),
        git_sha=_git_sha(),
    )


def document_id(result: object) -> str:
    """SearchResult carries doc_id; accepting strings keeps fake engines minimal."""
    if isinstance(result, str):
        return result
    value = getattr(result, "doc_id", None)
    if value is None:
        raise TypeError("evaluation engine result is missing doc_id")
    return str(value)


def score_of(result: object) -> float:
    """Mirror of document_id for scores; a bare-string fake engine scores 0.0.

    Deliberately not raising when the attribute is absent: every existing fake
    engine in the suite yields strings, and forcing them all to grow a score would
    be a large diff in service of a field those tests do not exercise.
    """
    if isinstance(result, str):
        return 0.0
    return float(getattr(result, "score", 0.0))


def _rank_at_k(retrieved_ids: list[str], wanted_ids: tuple[str, ...], k: int) -> int:
    reciprocal = reciprocal_rank(retrieved_ids[:max(0, k)], wanted_ids)
    return round(1 / reciprocal) if reciprocal else 0


def _fingerprint(engine) -> dict[str, Any]:
    config = getattr(engine, "config", None)
    reranker = getattr(engine, "reranker", None)
    half_precision = getattr(reranker, "half_precision", None)
    precision = "fp16" if half_precision is True else "fp32" if half_precision is False else "unknown"
    return {
        "embedder": str(getattr(getattr(engine, "embedder", None), "name", "unknown")),
        "reranker": {
            "name": str(getattr(reranker, "model_name", type(reranker).__name__)),
            "precision": precision,
        },
        "prefetch": getattr(config, "prefetch", None),
        "top_k": getattr(config, "top_k", None),
        "rrf_k": getattr(config, "rrf_k", None),
        "wiki_boost": getattr(config, "wiki_boost", None),
    }


def _corpus_chunks(engine) -> int | None:
    store = getattr(engine, "store", None)
    count = getattr(store, "count", None)
    return int(count()) if callable(count) else None


def _git_sha() -> str:
    root = Path(__file__).resolve().parents[3]
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=False,
    )
    sha = completed.stdout.strip()
    return sha or "unknown"
