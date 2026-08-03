"""Golden-set retrieval evaluation tools."""

from .golden import GoldenEntry, load_golden, verify_targets
from .metrics import EvalResult, EvalSummary, mrr, recall_at_k, reciprocal_rank, summarize
from .runner import EvalReport, run_eval

__all__ = [
    "EvalReport",
    "EvalResult",
    "EvalSummary",
    "GoldenEntry",
    "load_golden",
    "mrr",
    "recall_at_k",
    "reciprocal_rank",
    "run_eval",
    "summarize",
    "verify_targets",
]
