"""End-to-end hybrid retrieval with traceable, failure-tolerant reranking."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..cache import QueryCache, normalize_query, read_index_generation
from ..index.bm25 import BM25Index, searchable_text
from ..index.embedder import Embedder
from ..index.filtering import normalize_filters
from ..index.store import VectorStore
from ..monitor import QueryLogger
from .fusion import apply_layer_boost, rrf
from .rerank import RerankCandidate, Reranker

__all__ = ["SearchConfig", "SearchEngine", "SearchResult"]


@dataclass(frozen=True)
class SearchConfig:
    # depth and prefetch are kept as separate knobs (see docstrings below) even
    # though depth is currently EQUAL to prefetch -- that equality is a measured
    # conclusion, not a coincidence of the code shape.
    #
    # depth=100 was tried and REVERTED. The theory was sound (a golden target at
    # dense-rank-42 contributes zero to fusion at depth=8) but measurement disagreed:
    # on the MLX+heading-fix index with the query instruct-prefix also enabled,
    # depth=100 crowded the rerank pool with more plausible distractors that beat
    # the true answer for the scarce prefetch=8 slots -- recall@5 78.6%->64.3%,
    # MRR 0.607 vs 0.714 at depth=8. Each change was safe ALONE; combined they hurt.
    # depth=8 matches or beats every other tested combination. If you re-attempt
    # deeper candidate pools, re-run golden-v1 with prefix on AND off before trusting it.
    depth: int = 8
    # 8: measured knee on golden-v1 -- 20/12/8 give identical recall and MRR,
    # p50 1071ms -> 437ms. See config.py.
    prefetch: int = 8

    def __post_init__(self):
        if self.depth < self.prefetch:
            object.__setattr__(self, "depth", self.prefetch)
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
                 client: str = "cli", query_cache: "QueryCache | None" = None,
                 corpus_root: str | Path | None = None) -> None:
        self.embedder = embedder
        self.store = store
        self.bm25 = bm25
        self.reranker = reranker
        self.config = config or SearchConfig()
        self.logger = logger
        self.client = client
        self.query_cache = query_cache
        self.last_trace: dict[str, Any] = {}
        self.last_cache_hit = 0
        # Corpus generation counter: cache keys are bound to it (Red
        # release change 1) so any reindex invalidates cached results.
        self._generation = (read_index_generation(corpus_root)
                            if corpus_root is not None else 0)

    def search(self, query: str, *, k: int | None = None, filters: Mapping[str, Any] | None = None,
               tier: str = "map") -> list[SearchResult]:
        started = time.perf_counter()
        # Execute the SAME normalized query that keys the cache (Red: the
        # first spelling must not control later results).
        query = normalize_query(query)
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

        # QUERY CACHE: exact (whitespace-normalized) query -> stored results.
        # Key covers k, config knobs and filters; TTL bounds staleness against
        # corpus drift (reindex changes embeddings, but 24h is a deliberate
        # ponytail ceiling: invalidation-by-index-sha is the upgrade path if
        # stale hits ever show up in the query-log review).
        cached: list[SearchResult] | None = None
        if self.query_cache is not None:
            ckey = self.query_cache.key(
                query, limit, self.config, metadata_filter,
                generation=self._generation)
            payload = self.query_cache.get(ckey)
            if payload is not None:
                cached = [
                    SearchResult(
                        c["chunk_id"], c["doc_id"], c["text"], c.get("heading_path", ""),
                        c.get("layer", ""), c.get("score", 0.0), rank, {
                            "cache_hit": True, "latency_ms": _elapsed_ms(started),
                        })
                    for rank, c in enumerate(payload, start=1)
                ]
        if cached is not None:
            self.last_cache_hit = 1  # query-cache hit (embedding never consulted)
            trace["cache_hit"] = True
            trace["cache_source"] = "query"
            trace["latency_ms"] = _elapsed_ms(started)
            self.last_trace = trace
            if self.logger is not None:
                self.logger.log(
                    query=query, filters=metadata_filter, tier=tier,
                    retrieved_ids=[r.chunk_id for r in cached],
                    scores=[r.score for r in cached], latency_ms=trace["latency_ms"],
                    cache_hit=1, client=self.client)
            return cached
        self.last_cache_hit = 0

        embed_started = time.perf_counter()
        query_vector = None
        try:
            # embed_queries applies the model's instruct prefix (queries only;
            # documents are embedded raw). Fall back for bare providers.
            if hasattr(self.embedder, "embed_queries"):
                query_vector = self.embedder.embed_queries([query])[0]
            else:
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
            lexical_future = pool.submit(self.bm25.search, query, self.config.depth, metadata_filter)
            dense_future = (pool.submit(self.store.search_vector, query_vector, self.config.depth,
                                        metadata_filter) if query_vector is not None else None)
            lexical, lexical_error = _future_value(lexical_future, [])
            dense, dense_error = _future_value(dense_future, []) if dense_future is not None else ([], None)
        trace["stages"]["bm25"] = {
            "in": self.config.depth,
            "out": len(lexical),
            "scores": dict(lexical),
            "timing_ms": _elapsed_ms(lexical_started),
            "error": lexical_error,
        }
        trace["stages"]["dense"] = {
            "in": self.config.depth,
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
            # Heading included: the reranker judges "does this passage answer the
            # query?", and judging that on text stripped of its own title is how a
            # document whose TITLE matches the query gets dropped. All three stages
            # (bm25, embedder, reranker) must see the same text.
            RerankCandidate(chunk_id, searchable_text(records[chunk_id]),
                            boosted_scores[chunk_id])
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
            # hit-source: 2 = embedding-cache only, 1 = query-cache (returned
            # earlier), 0 = neither. Separate semantics (Red release change 7).
            hit = 2 if cache_stats.get("hits") else self.last_cache_hit
            trace["logging"] = {"ok": self.logger.log(
                query=query, filters=metadata_filter, tier=tier,
                retrieved_ids=[result.chunk_id for result in results],
                scores=[result.score for result in results], latency_ms=trace["latency_ms"],
                cache_hit=hit, client=self.client,
            )}
        # QUERY CACHE write: only cacheable (complete) result sets.
        # Red release change 5: a partial failure (one retrieval leg down,
        # reranker degraded) must not be cached as a snapshot of health.
        cacheable = bool(results) and not trace.get("reranker", {}).get("degraded")
        if lexical_error is not None or dense_error is not None:
            cacheable = False
        if self.query_cache is not None and cacheable:
            self.query_cache.put(ckey, [{
                "chunk_id": r.chunk_id, "doc_id": r.doc_id, "text": r.text,
                "heading_path": r.heading_path, "layer": r.layer, "score": r.score,
            } for r in results])
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
