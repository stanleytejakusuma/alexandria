"""End-to-end hybrid retrieval with traceable, failure-tolerant reranking."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..index.bm25 import BM25Index
from ..index.embedder import Embedder
from ..index.filtering import normalize_filters
from ..index.store import VectorStore
from ..monitor import QueryLogger
from .fusion import apply_layer_boost, rrf
from .rerank import RerankCandidate, Reranker

__all__ = ["SearchConfig", "SearchEngine", "SearchResult"]


@dataclass(frozen=True)
class SearchConfig:
    prefetch: int = 20
    top_k: int = 5
    wiki_boost: float = 1.25
    rrf_k: int = 60


@dataclass(frozen=True)
class SearchResult:
    chunk_id: str
    doc_id: str
    text: str
    heading_path: str
    layer: str
    score: float
    rank: int
    trace: dict[str, Any]


class SearchEngine:
    def __init__(self, embedder: Embedder, store: VectorStore, bm25: BM25Index, reranker: Reranker,
                 config: SearchConfig | None = None, logger: QueryLogger | None = None,
                 client: str = "cli") -> None:
        self.embedder = embedder
        self.store = store
        self.bm25 = bm25
        self.reranker = reranker
        self.config = config or SearchConfig()
        self.logger = logger
        self.client = client
        self.last_trace: dict[str, Any] = {}

    def search(self, query: str, *, k: int | None = None, filters: Mapping[str, Any] | None = None,
               tier: str = "map") -> list[SearchResult]:
        started = time.perf_counter()
        limit = self.config.top_k if k is None else max(0, k)
        metadata_filter = normalize_filters(filters)
        trace: dict[str, Any] = {
            "query": query,
            "tier": tier,
            "metadata_filter": metadata_filter,
            "stages": {},
        }
        if not query.strip() or limit == 0:
            trace["reason"] = "empty query or zero result limit"
            self.last_trace = trace
            return []

        embed_started = time.perf_counter()
        query_vector = None
        try:
            query_vector = self.embedder.embed([query])[0]
            cache_stats = getattr(self.embedder, "last_cache_stats", {"hits": 0, "misses": 1})
            trace["stages"]["embed"] = {
                "out": 1,
                "timing_ms": _elapsed_ms(embed_started),
                "cache": dict(cache_stats),
                "error": None,
            }
        except Exception as exc:
            cache_stats = {"hits": 0, "misses": 0}
            trace["stages"]["embed"] = {
                "out": 0,
                "timing_ms": _elapsed_ms(embed_started),
                "cache": cache_stats,
                "error": f"{type(exc).__name__}: {exc}",
            }

        lexical_started = time.perf_counter()
        dense_started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=2) as pool:
            lexical_future = pool.submit(self.bm25.search, query, self.config.prefetch, metadata_filter)
            dense_future = (pool.submit(self.store.search_vector, query_vector, self.config.prefetch,
                                        metadata_filter) if query_vector is not None else None)
            lexical, lexical_error = _future_value(lexical_future, [])
            dense, dense_error = _future_value(dense_future, []) if dense_future is not None else ([], None)
        trace["stages"]["bm25"] = {
            "in": self.config.prefetch,
            "out": len(lexical),
            "scores": dict(lexical),
            "timing_ms": _elapsed_ms(lexical_started),
            "error": lexical_error,
        }
        trace["stages"]["dense"] = {
            "in": self.config.prefetch,
            "out": len(dense),
            "scores": {row["chunk_id"]: -float(row.get("_distance", 0.0)) for row in dense},
            "timing_ms": _elapsed_ms(dense_started),
            "error": dense_error,
        }

        fusion_started = time.perf_counter()
        base_scores = rrf([[chunk_id for chunk_id, _ in lexical],
                           [row["chunk_id"] for row in dense]], self.config.rrf_k)
        # ONE batched lookup, not one per candidate: the per-candidate version cost
        # ~494ms of pure overhead per query (up to 40 full table scans).
        records: dict[str, dict] = {}
        lookup_errors: dict[str, str] = {}
        try:
            records = self.store.get_many(list(base_scores))
        except Exception as exc:
            lookup_errors["*"] = f"{type(exc).__name__}: {exc}"
        base_scores = {chunk_id: score for chunk_id, score in base_scores.items() if chunk_id in records}
        layers = {chunk_id: record["layer"] for chunk_id, record in records.items()}
        before = _ordered(base_scores)
        boosted_scores = apply_layer_boost(base_scores, layers, wiki_boost=self.config.wiki_boost)
        after = _ordered(boosted_scores)
        trace["stages"]["fusion"] = {
            "in": {"bm25": len(lexical), "dense": len(dense)},
            "out": len(after),
            "scores_before_boost": base_scores,
            "scores": boosted_scores,
            "boost_changed_order": before != after,
            "lookup_errors": lookup_errors,
            "timing_ms": _elapsed_ms(fusion_started),
        }
        candidates = [
            RerankCandidate(chunk_id, records[chunk_id]["text"], boosted_scores[chunk_id])
            for chunk_id in after[:self.config.prefetch] if chunk_id in records
        ]

        rerank_started = time.perf_counter()
        degraded = False
        error = None
        try:
            reranked = self.reranker.rerank(query, candidates, limit)
        except Exception as exc:  # model process/network failure must not take down query
            reranked = candidates[:limit]
            degraded = True
            error = f"{type(exc).__name__}: {exc}"
        trace["reranker"] = {
            "in": len(candidates), "out": len(reranked), "degraded": degraded, "error": error,
            "timing_ms": _elapsed_ms(rerank_started),
        }
        trace["stages"]["rerank"] = {"scores": {item.chunk_id: item.score for item in reranked}}
        if len(reranked) < limit:
            trace["shortfall"] = {
                "requested": limit,
                "returned": len(reranked),
                "reason": "fewer filtered candidates than requested",
            }

        results = [
            SearchResult(candidate.chunk_id, records[candidate.chunk_id]["doc_id"],
                         records[candidate.chunk_id]["text"], records[candidate.chunk_id]["heading_path"],
                         records[candidate.chunk_id]["layer"], candidate.score, rank, trace)
            for rank, candidate in enumerate(reranked, start=1)
        ]
        trace["latency_ms"] = _elapsed_ms(started)
        self.last_trace = trace
        if self.logger is not None:
            trace["logging"] = {"ok": self.logger.log(
                query=query, filters=metadata_filter, tier=tier,
                retrieved_ids=[result.chunk_id for result in results],
                scores=[result.score for result in results], latency_ms=trace["latency_ms"],
                cache_hit=bool(cache_stats.get("hits")), client=self.client,
            )}
        return results


def _ordered(scores: Mapping[str, float]) -> list[str]:
    return [chunk_id for chunk_id, _ in sorted(scores.items(), key=lambda item: (-item[1], item[0]))]


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)


def _future_value(future, fallback):
    try:
        return future.result(), None
    except Exception as exc:
        return fallback, f"{type(exc).__name__}: {exc}"
