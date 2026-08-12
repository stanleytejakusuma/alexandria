"""Pure, deterministic scoring functions for retrieval evaluation."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field

__all__ = ["EvalResult", "EvalSummary", "by_overlap_band", "mrr", "recall_at_k",
          "reciprocal_rank", "summarize"]


@dataclass(frozen=True)
class EvalResult:
    id: str
    query: str
    hit: bool
    rank: int
    retrieved_ids: list[str]
    latency_ms: float
    error: str | None = None
    target_error: bool = False
    overlap_band: str | None = None
    scores: tuple[float, ...] = ()
    """Retrieval scores, positionally aligned with retrieved_ids.

    Recorded because rank alone cannot say whether the engine was *confident*.
    Precision work (BACKLOG #21) and the relevance-floor question
    (SPEC-data-model-and-ambient-capture Q5) both need the score distribution, and
    an eval that discards it can only be re-run, never re-analysed. Defaults empty
    so the ~1MB of history written before this field stays loadable.
    """

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict) -> "EvalResult":
        return cls(
            id=str(raw["id"]),
            query=str(raw["query"]),
            hit=bool(raw["hit"]),
            rank=int(raw["rank"]),
            retrieved_ids=[str(value) for value in raw.get("retrieved_ids", [])],
            latency_ms=float(raw["latency_ms"]),
            error=raw.get("error"),
            target_error=bool(raw.get("target_error", False)),
            overlap_band=raw.get("overlap_band"),
            scores=tuple(float(value) for value in raw.get("scores", ())),
        )


@dataclass(frozen=True)
class EvalSummary:
    recall_at_k: float
    mrr: float
    n: int
    hits: int
    misses: list[str]
    target_errors: list[str]
    errors: int = 0
    error_ids: list[str] | None = None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["error_ids"] = self.error_ids or []
        return data

    @classmethod
    def from_dict(cls, raw: dict) -> "EvalSummary":
        return cls(
            recall_at_k=float(raw["recall_at_k"]),
            mrr=float(raw["mrr"]),
            n=int(raw["n"]),
            hits=int(raw["hits"]),
            misses=[str(value) for value in raw.get("misses", [])],
            target_errors=[str(value) for value in raw.get("target_errors", [])],
            errors=int(raw.get("errors", 0)),
            error_ids=[str(value) for value in raw.get("error_ids", [])],
        )


def recall_at_k(retrieved_ids: Sequence[str], want_ids: Sequence[str], k: int) -> bool:
    """Whether any accepted target appears among the first *k* retrieved ids."""
    if k <= 0:
        return False
    wanted = set(want_ids)
    return any(retrieved_id in wanted for retrieved_id in retrieved_ids[:k])


def reciprocal_rank(retrieved_ids: Sequence[str], want_ids: Sequence[str]) -> float:
    """Return the reciprocal position of the first accepted target, or zero."""
    wanted = set(want_ids)
    for rank, retrieved_id in enumerate(retrieved_ids, start=1):
        if retrieved_id in wanted:
            return 1.0 / rank
    return 0.0


def mrr(per_query_rr: Iterable[float]) -> float:
    """Mean reciprocal rank, defined as zero for an empty input."""
    values = list(per_query_rr)
    return sum(values) / len(values) if values else 0.0


def by_overlap_band(results: Sequence[EvalResult]) -> dict[str, EvalSummary]:
    """Slice results by NoLiMa-style lexical-overlap band and summarize each slice.

    Turns one aggregate recall number into a diagnostic one: "zero-overlap recall
    dropped" localizes a regression; "recall dropped" does not. Untagged entries
    (every pre-tagging golden entry) are excluded, not folded into a fake band.
    """
    bands: dict[str, list[EvalResult]] = {}
    for result in results:
        if result.overlap_band:
            bands.setdefault(result.overlap_band, []).append(result)
    return {band: summarize(rows) for band, rows in bands.items()}


def summarize(results: Sequence[EvalResult]) -> EvalSummary:
    """Summarize results without hiding target-validation or query errors.

    Missing corpus targets are excluded from the scored denominator: a caller must
    repair that golden set before treating the score as usable. Query errors remain
    in the denominator as misses, so an engine cannot gain recall by failing.
    """
    target_errors = [result.id for result in results if result.target_error]
    scored = [result for result in results if not result.target_error]
    hits = sum(result.hit for result in scored)
    errors = [result.id for result in scored if result.error is not None]
    return EvalSummary(
        recall_at_k=hits / len(scored) if scored else 0.0,
        mrr=mrr(1.0 / result.rank if result.hit and result.rank > 0 else 0.0 for result in scored),
        n=len(results),
        hits=hits,
        misses=[result.id for result in scored if not result.hit],
        target_errors=target_errors,
        errors=len(errors),
        error_ids=errors,
    )
