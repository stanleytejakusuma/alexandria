"""Negative retrieval cases: queries whose correct answer is *nothing*.

The golden retrieval set (`golden.py`) validates `must_retrieve` as non-empty, so
every entry asserts that some document must be found. That makes one whole class
of failure structurally invisible: an engine that returns confident, plausible,
wrong documents for a query the corpus cannot answer scores exactly as well as
one that correctly finds nothing, because no entry ever asks for nothing.

That gap is BACKLOG #21 ("zero negative cases, recall-only") and it blocks two
things beyond itself: offline policy tuning (BACKLOG #29), which would optimise
against a metric that cannot see precision, and the relevance floor in
SPEC-data-model-and-ambient-capture Q5, which asks whether a score threshold
separating "relevant" from "nothing relevant" can be specified at all.

This module does not answer Q5 by assertion. It builds the instrument that lets
the corpus answer it: run queries known to have no answer, run queries known to
have one, and compare the score distributions. If they separate, a floor exists
and `separation()` reports where. If they overlap, no floor is specifiable and
the honest response is to ship ambient injection off by default.

Design note: a negative entry is deliberately NOT expressed as a golden entry
with an empty `must_retrieve`. Mixing them would corrupt recall@k -- every
negative would score as a miss, dragging the headline number down for the crime
of being answered correctly. Negatives are a precision instrument and are
summarised separately.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from .jsonl_records import load_jsonl_records
from .metrics import EvalResult
from .runner import document_id, score_of

__all__ = [
    "NegativeEntry",
    "SeparationReport",
    "load_negative",
    "run_negative",
    "separation",
]


@dataclass(frozen=True)
class NegativeEntry:
    """A query with no correct answer anywhere in the corpus.

    `note` is required, not optional: a negative case is a claim about the
    *absence* of something across ~33k documents, which is far easier to get
    wrong than a positive one and cannot be verified by looking at a single file.
    The note records how absence was established, so a later reader can re-check
    the claim instead of trusting it. `verified_against` records the corpus size
    at verification time, because "nothing answers this" can silently stop being
    true the moment the corpus grows -- including as a direct result of this
    session being distilled into it.
    """

    id: str
    query: str
    note: str
    verified_against: int | None = None


_FIELDS = {"id", "query", "note", "verified_against"}
_REQUIRED_FIELDS = {"id", "query", "note"}


def load_negative(path: str | Path) -> list[NegativeEntry]:
    """Load a negative JSONL file, rejecting every malformed row with its line number."""
    return load_jsonl_records(path, _parse_entry, lambda e: e.id)


def run_negative(engine, entries: Sequence[NegativeEntry], *, k: int = 5) -> list[EvalResult]:
    """Run negative queries, recording what came back and how confidently.

    Returns EvalResult rows with `hit=False` throughout -- not because the engine
    failed, but because "hit" is a recall concept and does not apply here. What
    matters is `scores`. Reusing EvalResult rather than inventing a parallel row
    type keeps these results loadable by the same history and analysis code.

    A query that raises is preserved as a row with its error, never dropped --
    same discipline as run_eval. Silently skipping failures here would inflate
    separation by removing exactly the queries that behaved unusually.
    """
    results: list[EvalResult] = []
    for entry in entries:
        started = time.perf_counter()
        try:
            raw_results = engine.search(entry.query, k=k)
            retrieved_ids = [document_id(result) for result in raw_results][:k]
            scores = tuple(score_of(result) for result in raw_results)[:k]
            error = None
        except Exception as exc:
            retrieved_ids = []
            scores = ()
            error = f"{type(exc).__name__}: {exc}"
        latency_ms = round((time.perf_counter() - started) * 1000, 3)
        results.append(EvalResult(
            id=entry.id,
            query=entry.query,
            hit=False,
            rank=0,
            retrieved_ids=retrieved_ids,
            latency_ms=latency_ms,
            error=error,
            scores=scores,
        ))
    return results


@dataclass(frozen=True)
class SeparationReport:
    """Whether positive and negative score distributions can be told apart.

    `clean_floor_recall` is the load-bearing number and answers Q5 directly: the
    fraction of *answerable* queries whose top hit still outscores the best
    scoring unanswerable one. It is the recall an injection filter would retain
    if its threshold were set high enough to admit no known-bad query.

    Near 1.0, a floor exists and costs little. Near 0.0, the distributions
    overlap and no useful threshold exists -- in which case the correct product
    decision is to not filter on score at all, rather than to pick a number that
    looks principled and silently drops real context.
    """

    n_positive: int
    n_negative: int
    positive_top1_min: float
    positive_top1_median: float
    positive_top1_max: float
    negative_top1_min: float
    negative_top1_median: float
    negative_top1_max: float
    clean_floor: float
    clean_floor_recall: float

    @property
    def separable(self) -> bool:
        """True when a threshold retains most real queries and admits no known-bad one.

        0.8 is a deliberately stated convention, not a derived constant: it is the
        point past which a floor costs less than the garbage it excludes. It is
        named here so a future reader argues with a number they can see.
        """
        return self.clean_floor_recall >= 0.8

    def to_dict(self) -> dict:
        return {**asdict(self), "separable": self.separable}


def separation(positive: Sequence[EvalResult], negative: Sequence[EvalResult]) -> SeparationReport:
    """Compare top-1 score distributions between answerable and unanswerable queries.

    Only *hits* count as positives: a golden entry the engine missed says nothing
    about how confidently it scores a correct answer, and including its score
    would measure the wrong thing. Rows with no results at all are skipped on
    both sides -- an empty result set has no top-1 score to compare, and treating
    it as 0.0 would flatter the separation.

    A positive contributes the score of the *hit itself* (`scores[rank-1]`), not
    of the engine's top result. `hit` only means the target appeared somewhere in
    top-k, so for a hit at rank 3 the top-1 score belongs to a document that was
    wrong. Scores descend, so taking scores[0] silently inflates the positive
    distribution and overstates separation -- measured on the first real run:
    minimum positive fell 0.1190 -> 0.0274 once corrected, moving the retained
    fraction at a 0.12 floor from an apparent 100% to 90.3%. A negative has no
    correct answer by definition, so its top-1 score is the right measure: it is
    the engine's most confident wrong claim.
    """
    positive_scores = sorted(
        r.scores[r.rank - 1] for r in positive
        if r.hit and r.scores and 0 < r.rank <= len(r.scores)
    )
    negative_scores = sorted(r.scores[0] for r in negative if r.scores)
    if not positive_scores or not negative_scores:
        raise ValueError(
            "separation needs at least one scored positive hit and one scored negative "
            f"(got {len(positive_scores)} positive, {len(negative_scores)} negative)"
        )

    # The floor that admits no known-bad query: just above the best negative.
    clean_floor = negative_scores[-1]
    retained = sum(1 for score in positive_scores if score > clean_floor)
    return SeparationReport(
        n_positive=len(positive_scores),
        n_negative=len(negative_scores),
        positive_top1_min=positive_scores[0],
        positive_top1_median=_median(positive_scores),
        positive_top1_max=positive_scores[-1],
        negative_top1_min=negative_scores[0],
        negative_top1_median=_median(negative_scores),
        negative_top1_max=negative_scores[-1],
        clean_floor=clean_floor,
        clean_floor_recall=retained / len(positive_scores),
    )


def _median(sorted_values: list[float]) -> float:
    middle = len(sorted_values) // 2
    if len(sorted_values) % 2:
        return sorted_values[middle]
    return (sorted_values[middle - 1] + sorted_values[middle]) / 2


def _parse_entry(raw: object, line_number: int) -> NegativeEntry:
    if not isinstance(raw, dict):
        raise ValueError(f"negative line {line_number}: entry must be a JSON object")
    unknown = set(raw) - _FIELDS
    if unknown:
        raise ValueError(f"negative line {line_number}: unknown field(s): {', '.join(sorted(unknown))}")
    missing = _REQUIRED_FIELDS - set(raw)
    if missing:
        raise ValueError(f"negative line {line_number}: missing field(s): {', '.join(sorted(missing))}")

    entry_id = raw["id"]
    query = raw["query"]
    note = raw["note"]
    verified_against = raw.get("verified_against")
    if not isinstance(entry_id, str) or not entry_id:
        raise ValueError(f"negative line {line_number}: id must be a non-empty string")
    if not isinstance(query, str) or not query:
        raise ValueError(f"negative line {line_number}: query must be a non-empty string")
    if not isinstance(note, str) or not note:
        raise ValueError(
            f"negative line {line_number}: note must be a non-empty string recording how "
            "absence was established"
        )
    if verified_against is not None and (
        not isinstance(verified_against, int) or isinstance(verified_against, bool)
        or verified_against < 0
    ):
        raise ValueError(
            f"negative line {line_number}: verified_against must be a non-negative integer"
        )
    return NegativeEntry(entry_id, query, note, verified_against)
