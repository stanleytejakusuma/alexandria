"""Judge 3 -- gather-completeness for CONTRA-SCAN, per SPEC-phase2-eval.md.

CONTRA-SCAN cannot flag a contradiction its gather step never retrieved, so this
judge measures the gather stage itself, not the scan. Structurally this is a
retrieval-recall measurement against phase 1's real search engine, not an LLM
judgment call (contrast with Judge 2 / coverage.py, which needs a grader) -- given a
seeded contradiction pair's query, does the real hybrid search surface BOTH members
within top-k?

Deliberately BOTH, not ANY-OF (the retrieval golden set's semantics): either member
of a contradicting pair could end up as "the claim already cited" in a real synthesis
pass, so the query has to be able to surface the OTHER one regardless of which side
that turns out to be. ANY-OF would let a query that only ever finds claim_a pass,
which tells us nothing about whether the contradiction would ever actually surface.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .contradiction_golden import ContradictionPairEntry
from .runner import _corpus_chunks, _document_id, _fingerprint, _git_sha

__all__ = ["GatherResult", "GatherSummary", "GatherReport", "run_gather_completeness", "passes_gate"]

GATE_THRESHOLD = 0.90


@dataclass(frozen=True)
class GatherResult:
    id: str
    query: str
    claim_a_found: bool
    claim_b_found: bool
    both_found: bool
    retrieved_ids: list[str]
    latency_ms: float
    error: str | None


@dataclass(frozen=True)
class GatherSummary:
    n: int
    pair_recall: float
    error_ids: list[str]


@dataclass(frozen=True)
class GatherReport:
    results: list[GatherResult]
    summary: GatherSummary
    config: dict[str, Any]
    corpus_chunks: int | None
    timestamp: str
    git_sha: str


def passes_gate(pair_recall: float) -> bool:
    """Spec's gate: >= 90%. A bare >= comparison, named so the threshold lives in
    exactly one place rather than being re-typed at every call site."""
    return pair_recall >= GATE_THRESHOLD


def run_gather_completeness(engine, entries: list[ContradictionPairEntry], *,
                            k_override: int | None = None) -> GatherReport:
    """Run every seeded pair in file order. A search failure degrades loudly -- same
    discipline as run_eval: an eval that cannot fail correctly is worse than no eval.
    """
    results: list[GatherResult] = []
    for entry in entries:
        k = k_override if k_override is not None else 5
        started = time.perf_counter()
        try:
            raw_results = engine.search(entry.query, k=k)
            retrieved_ids = [_document_id(r) for r in raw_results][:k]
            claim_a_found = any(c in retrieved_ids for c in entry.claim_a)
            claim_b_found = any(c in retrieved_ids for c in entry.claim_b)
            both_found = claim_a_found and claim_b_found
            error = None
        except Exception as exc:  # a gather-stage miss must never be a silent pass
            retrieved_ids = []
            claim_a_found = claim_b_found = both_found = False
            error = f"{type(exc).__name__}: {exc}"
        latency_ms = round((time.perf_counter() - started) * 1000, 3)
        results.append(GatherResult(entry.id, entry.query, claim_a_found, claim_b_found,
                                    both_found, retrieved_ids, latency_ms, error))

    n = len(results)
    covered = sum(1 for r in results if r.both_found)
    error_ids = [r.id for r in results if r.error is not None]
    summary = GatherSummary(n=n, pair_recall=(covered / n if n else 0.0), error_ids=error_ids)

    return GatherReport(
        results=results,
        summary=summary,
        config=_fingerprint(engine),
        corpus_chunks=_corpus_chunks(engine),
        timestamp=datetime.now(timezone.utc).isoformat(),
        git_sha=_git_sha(),
    )
