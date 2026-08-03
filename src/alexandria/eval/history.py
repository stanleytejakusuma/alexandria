"""Append-only evaluation history and regression-focused report comparisons."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .runner import EvalReport

__all__ = ["Delta", "append_run", "compare", "load_runs", "regressions"]


@dataclass(frozen=True)
class Delta:
    recall_at_k: float
    mrr: float
    hit_to_miss: list[str]
    miss_to_hit: list[str]

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
    return Delta(
        recall_at_k=current.summary.recall_at_k - previous.summary.recall_at_k,
        mrr=current.summary.mrr - previous.summary.mrr,
        hit_to_miss=[
            result.id for result in previous.results
            if previous_hits.get(result.id) is True and current_hits.get(result.id) is False
        ],
        miss_to_hit=[
            result.id for result in current.results
            if previous_hits.get(result.id) is False and current_hits.get(result.id) is True
        ],
    )


def regressions(delta: Delta) -> list[str]:
    return list(delta.hit_to_miss)
