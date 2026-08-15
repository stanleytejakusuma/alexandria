#!/usr/bin/env python3
"""Leg-ablation invariant for the REAL retrieval gate (BACKLOG #47/#48).

WHY THIS EXISTS. The synthetic gate is a lexical-harness check: its embedder is
semantically empty, so its dense leg is noise and the gate deliberately runs
dense=False (BACKLOG #47). That leaves one question no in-repo check answers: on
the REAL corpus, is either retrieval leg dead weight? A leg is dead weight if
removing it IMPROVES recall or MRR -- which is exactly what #47 measured the
synthetic dense leg doing (recall 0.950 -> 1.000, MRR 0.514 -> 0.988).

WHAT IT DOES. Builds the real engine once and scores the private golden set three
times: both legs, dense-only (lexical amputated), and lexical-only (dense
amputated). It fails if removing either leg produces a SIGNIFICANT recall/MRR
improvement, using the same McNemar significance bar the real eval gate uses.
This is what finally makes that significance machinery gate something (#48: it
was print-only decoration before -- mcnemar_exact was never called under
scripts/).

SIGNIFICANCE SEMANTICS. The bar is McNemar p<0.05 over recall transitions. MRR is
checked alongside recall but only fails when the recall change is already
significant: a leg whose removal moves MRR without moving recall cannot be
distinguished from noise at n=49, so that case is reported, not gated.

READ-ONLY. Builds nothing, appends nothing to eval history, and disables the
query logger so the corpus's demand-report query log is not polluted. It skips
(exit 0) when the private corpus/golden/index is absent, exactly like
eval-gate.py, so a clean clone or CI box is never blocked.

WEEKLY, NOT PRE-COMMIT. Each amputated pass is a full golden-set scoring
(~60-90s), so this belongs in the weekly loop, not .git/hooks/pre-commit.

Usage: python3 scripts/leg-ablation.py [--corpus ~/alexandria-corpus] [--json]
Exit 0 if neither leg is dead weight (or skipped), 1 if a leg's removal
improves recall/MRR, 2 on setup error.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from alexandria.cli import _build_search_engine  # noqa: E402  (private, same package)
from alexandria.config import load_config  # noqa: E402
from alexandria.eval.golden import load_golden, verify_targets  # noqa: E402
from alexandria.eval.history import compare  # noqa: E402
from alexandria.eval.runner import run_eval  # noqa: E402
from alexandria.index.bm25 import BM25Index  # noqa: E402

DEFAULT_CORPUS = Path("~/alexandria-corpus").expanduser()


class _NullQueryEmbedder:
    """Delegates to the real embedder but returns None from embed_queries, so
    SearchEngine skips the dense leg (it only submits a dense future when the
    query vector is not None). Used to amputate the dense leg for the ablation."""

    def __init__(self, wrapped) -> None:
        self._wrapped = wrapped

    def embed_queries(self, queries):
        return [None for _ in queries]

    def __getattr__(self, name):
        return getattr(self._wrapped, name)


def _score(engine, entries):
    """Score the golden set once. Amputations mutate the engine in place and are
    restored by the caller, so a single engine (and single model load) serves all
    three passes."""
    engine.logger = None  # never pollute the demand-report query log
    return run_eval(engine, entries)


def dead_weight_verdict(baseline, variants: dict) -> tuple[list[str], dict]:
    """Decide whether removing a leg is dead weight.

    A leg is dead weight if amputating it SIGNIFICANTLY IMPROVES recall or MRR.
    Significance is McNemar p<0.05 over recall transitions (the same bar the real
    eval gate uses). An improvement that is not significant is reported, not
    gated -- at n=49 it cannot be distinguished from noise.

    Returns (failures, observations) where observations maps each variant name to
    the Delta dict plus an optional "note".
    """
    failures: list[str] = []
    observations: dict = {}
    for name, ablated in variants.items():
        delta = compare(baseline, ablated)
        observations[name] = delta.to_dict()
        improved = delta.recall_at_k > 0 or delta.mrr > 0
        if delta.significant and improved:
            failures.append(
                f"removing the {name} leg IMPROVED retrieval "
                f"(recall {delta.recall_at_k:+.3f}, MRR {delta.mrr:+.3f}, p={delta.p_value:.3f}) "
                f"-- that leg is dead weight"
            )
        elif improved:
            observations[name]["note"] = (
                "improved but not significant (n=49); reported, not gated"
            )
    return failures, observations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    corpus = args.corpus.expanduser()
    golden_path = corpus / ".alexandria" / "golden" / "golden-v1.jsonl"
    index = corpus / ".alexandria" / "index"
    if not golden_path.exists() or not index.exists():
        print("leg-ablation: SKIPPED (no private corpus/index on this machine)")
        return 0

    try:
        entries = load_golden(golden_path)
    except ValueError as exc:
        print(f"leg-ablation: {exc}", file=sys.stderr)
        return 2
    target_errors = verify_targets(entries, corpus)
    if target_errors:
        print("leg-ablation: unusable golden set: " + ", ".join(target_errors), file=sys.stderr)
        return 2

    config = load_config(corpus_override=str(corpus))
    engine = _build_search_engine(config, corpus, query_cache=False, client="leg-ablation")

    baseline = _score(engine, entries)

    # Amputate the dense leg (lexical-only).
    original_embedder = engine.embedder
    engine.embedder = _NullQueryEmbedder(original_embedder)
    lexical_only = _score(engine, entries)
    engine.embedder = original_embedder

    # Amputate the lexical leg (dense-only).
    with tempfile.TemporaryDirectory() as tmp:
        engine.bm25 = BM25Index(Path(tmp) / "empty-fts.sqlite")
        dense_only = _score(engine, entries)

    failures, observations = dead_weight_verdict(
        baseline, {"dense": dense_only, "lexical": lexical_only})

    if args.json:
        print(json.dumps({
            "baseline": baseline.summary.to_dict(),
            "dense_only": dense_only.summary.to_dict(),
            "lexical_only": lexical_only.summary.to_dict(),
            "comparisons": observations,
            "failures": failures,
        }, ensure_ascii=False, sort_keys=True))
    else:
        for name, report in (("both", baseline), ("dense-only", dense_only), ("lexical-only", lexical_only)):
            print(f"{name:12s} recall@k={report.summary.recall_at_k:.3f} MRR={report.summary.mrr:.3f}")
        for name, obs in observations.items():
            extra = f"  [{obs.get('note')}]" if "note" in obs else ""
            print(f"remove {name:6s}: recall {obs['recall_at_k']:+.3f} MRR {obs['mrr']:+.3f} p={obs['p_value']:.3f}{extra}")

    if failures:
        print("leg-ablation FAILED: " + "; ".join(failures), file=sys.stderr)
        return 1
    print("leg-ablation: neither retrieval leg is dead weight")
    return 0


if __name__ == "__main__":
    sys.exit(main())
