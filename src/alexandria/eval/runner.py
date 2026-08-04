"""Execute a golden retrieval set and capture the context required to interpret it."""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .golden import GoldenEntry
from .metrics import EvalResult, EvalSummary, recall_at_k, reciprocal_rank, summarize

__all__ = ["EvalReport", "run_eval"]


@dataclass(frozen=True)
class EvalReport:
    results: list[EvalResult]
    summary: EvalSummary
    config: dict[str, Any]
    corpus_chunks: int | None
    timestamp: str
    git_sha: str

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
        )


def run_eval(engine, entries: list[GoldenEntry], *, k_override: int | None = None) -> EvalReport:
    """Run entries in their file order, preserving query failures as failed rows."""
    results: list[EvalResult] = []
    for entry in entries:
        k = entry.k if k_override is None else k_override
        started = time.perf_counter()
        try:
            raw_results = engine.search(entry.query, k=k)
            retrieved_ids = [_document_id(result) for result in raw_results][:k] if k > 0 else []
            hit = recall_at_k(retrieved_ids, entry.must_retrieve, k)
            rank = _rank_at_k(retrieved_ids, entry.must_retrieve, k) if hit else 0
            error = None
        except Exception as exc:  # an eval must show a failed query, never drop it
            retrieved_ids = []
            hit = False
            rank = 0
            error = f"{type(exc).__name__}: {exc}"
        latency_ms = round((time.perf_counter() - started) * 1000, 3)
        results.append(EvalResult(entry.id, entry.query, hit, rank, retrieved_ids, latency_ms, error,
                                  overlap_band=entry.overlap_band))

    return EvalReport(
        results=results,
        summary=summarize(results),
        config=_fingerprint(engine),
        corpus_chunks=_corpus_chunks(engine),
        timestamp=datetime.now(timezone.utc).isoformat(),
        git_sha=_git_sha(),
    )


def _document_id(result: object) -> str:
    """SearchResult carries doc_id; accepting strings keeps fake engines minimal."""
    if isinstance(result, str):
        return result
    value = getattr(result, "doc_id", None)
    if value is None:
        raise TypeError("evaluation engine result is missing doc_id")
    return str(value)


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
