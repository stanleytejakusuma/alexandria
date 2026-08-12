"""Golden-set retrieval evaluation tools."""

from .golden import GoldenEntry, load_golden, verify_targets
from .metrics import EvalResult, EvalSummary, mrr, recall_at_k, reciprocal_rank, summarize
from .negative import (
    NegativeEntry,
    SeparationReport,
    load_negative,
    run_negative,
    separation,
)
from .runner import EvalReport, run_eval

__all__ = [
    "EvalReport",
    "EvalResult",
    "EvalSummary",
    "GoldenEntry",
    "NegativeEntry",
    "SeparationReport",
    "load_golden",
    "load_negative",
    "run_negative",
    "separation",
    "mrr",
    "recall_at_k",
    "reciprocal_rank",
    "run_eval",
    "summarize",
    "verify_targets",
]
