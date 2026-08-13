"""Append-only evaluation history and regression-focused report comparisons."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .metrics import mcnemar_exact
from .runner import EvalReport

__all__ = ["Delta", "append_run", "compare", "load_runs", "regressions"]


@dataclass(frozen=True)
class Delta:
    recall_at_k: float
    mrr: float
    hit_to_miss: list[str]
    miss_to_hit: list[str]
    negative_confidence_rose: list[str] = field(default_factory=list)
    """Unanswerable queries the engine became materially more confident about.

    The precision counterpart to hit_to_miss, at the same query-level granularity:
    an aggregate score can absorb one query going badly wrong, a named query
    cannot. Empty when either run carried no negatives.
    """
    clean_floor_recall: float = 0.0
    """Change in the fraction of answerable queries clearing the no-false-positive
    floor. Reported for visibility; the gate fires on the named list above."""
    p_value: float = 1.0
    """Exact two-sided McNemar p-value over the recall transitions.

    1.0 when nothing changed verdict, which is the honest reading: no discordant
    pairs is no evidence of a difference, not a confirmed absence of one.
    """

    @property
    def significant(self) -> bool:
        return self.p_value < SIGNIFICANCE_ALPHA

    @property
    def recall_delta(self) -> float:
        return self.recall_at_k

    @property
    def mrr_delta(self) -> float:
        return self.mrr

    def to_dict(self) -> dict:
        return asdict(self)


def append_run(path: str | Path, report: EvalReport) -> None:
    """Append exactly one JSON object without rewriting existing history."""
    history_path = Path(path)
    history_path.parent.mkdir(parents=True, exist_ok=True)
    with history_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True) + "\n")


def load_runs(path: str | Path) -> list[EvalReport]:
    history_path = Path(path)
    if not history_path.exists():
        return []
    reports: list[EvalReport] = []
    for line_number, line in enumerate(history_path.read_text(encoding="utf-8").splitlines(), start=1):
        try:
            reports.append(EvalReport.from_dict(json.loads(line)))
        except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise ValueError(f"eval history line {line_number}: invalid report ({exc})") from exc
    return reports


def compare(previous: EvalReport, current: EvalReport) -> Delta:
    """Compare aggregate scores and each shared query's pass/fail transition."""
    previous_hits = {result.id: result.hit for result in previous.results if not result.target_error}
    current_hits = {result.id: result.hit for result in current.results if not result.target_error}
    hit_to_miss = [
        result.id for result in previous.results
        if previous_hits.get(result.id) is True and current_hits.get(result.id) is False
    ]
    miss_to_hit = [
        result.id for result in current.results
        if previous_hits.get(result.id) is False and current_hits.get(result.id) is True
    ]
    return Delta(
        recall_at_k=current.summary.recall_at_k - previous.summary.recall_at_k,
        mrr=current.summary.mrr - previous.summary.mrr,
        hit_to_miss=hit_to_miss,
        miss_to_hit=miss_to_hit,
        negative_confidence_rose=_negative_confidence_rose(previous, current),
        clean_floor_recall=_clean_floor_recall(current) - _clean_floor_recall(previous),
        p_value=mcnemar_exact(len(hit_to_miss), len(miss_to_hit)),
    )


# A negative rising by more than this much is treated as a real precision
# regression rather than run-to-run noise. Stated as a convention so a future
# reader argues with a number they can see; scores are bounded in [0, 1] and the
# measured negative median is 0.024, so 0.10 is several times typical spread.
CONFIDENCE_RISE_THRESHOLD = 0.10

# The bar a recall difference must clear before the gate is willing to call it
# real. Conventional 0.05; stated here so a reader argues with a visible number.
SIGNIFICANCE_ALPHA = 0.05


def _top_score(result) -> float:
    return result.scores[0] if result.scores else 0.0


def _negative_confidence_rose(previous: EvalReport, current: EvalReport) -> list[str]:
    before = {result.id: _top_score(result) for result in previous.negatives}
    return sorted(
        result.id for result in current.negatives
        if result.id in before
        and _top_score(result) - before[result.id] > CONFIDENCE_RISE_THRESHOLD
    )


def _clean_floor_recall(report: EvalReport) -> float:
    if not report.separation:
        return 0.0
    return float(report.separation.get("clean_floor_recall", 0.0))


def regressions(delta: Delta) -> list[str]:
    """Named queries that got worse: recall losses and precision losses alike.

    Precision entries are prefixed so a failing gate says which kind of damage it
    found -- "recall dropped" and "the engine grew confident about a question the
    corpus cannot answer" call for different responses.
    """
    return list(delta.hit_to_miss) + [
        f"negative:{entry_id}" for entry_id in delta.negative_confidence_rose
    ]
