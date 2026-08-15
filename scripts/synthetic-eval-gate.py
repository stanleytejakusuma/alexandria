#!/usr/bin/env python3
"""Run the lexical-harness gate against the in-repo synthetic corpus. No private data.

WHY THIS EXISTS (BACKLOG #20). Its sibling `eval-gate.py` scores retrieval against
a golden set stored in a private corpus repo, and on any machine without that
corpus it prints SKIPPED and returns 0. That is the correct behaviour for a
quality gate whose data cannot be published -- and it means the gate has never
once run in CI, and cannot run for anyone who clones this repo. A gate that skips
everywhere but one laptop is not reproducible, and its greenness is not evidence.

This gate runs anywhere, because everything it needs is committed here:
`tests/fixtures/synthetic-corpus/` (16 documents about a fictional public
library), plus a golden set and a negative set over them.

WHAT IT MEASURES: the LEXICAL harness. Chunking, FTS5/BM25, the vector store
(hydration), the manifest check, recall/MRR scoring, the Wilson interval, the
McNemar significance bar, and the negative/separation machinery.

WHAT IT DOES NOT MEASURE, AND WHY (BACKLOG #47): retrieval quality on real
knowledge, and -- deliberately -- the dense ranking leg. The reproducible embedder
is `HashEmbedder`, which is semantically empty, so its dense leg is a noise
channel that REWARDS deleting the vector store (measured: recall 0.950 -> 1.000
with the dense leg forced empty; only BM25 death was caught). The gate therefore
runs `dense=False` and is a lexical-harness check, not a quality measure. The
dense leg's actual value is instead guarded by the real-gate leg-ablation check
(`scripts/leg-ablation.py`). The reranker is `IdentityReranker`, because the
production cross-encoder requires a ~90MB model download and this gate must run
on a network-free box.

So: two gates, two purposes. `eval-gate.py` answers "did retrieval quality
move?". This one answers "does the lexical measuring instrument still work?". A
green run here is never evidence that retrieval is healthy.

Usage:  python3 scripts/synthetic-eval-gate.py [--json]
Exit 0 if the harness clears its floors, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from alexandria.eval.golden import load_golden, verify_targets  # noqa: E402
from alexandria.eval.negative import load_negative, run_negative, separation  # noqa: E402
from alexandria.eval.runner import run_eval  # noqa: E402
from alexandria.eval.synthetic import (  # noqa: E402
    GOLDEN_PATH,
    NEGATIVE_PATH,
    build_synthetic_engine,
)

# Kept identical to tests/test_synthetic_gate.py; see the rationale there. Floors,
# not targets: the lexical-harness configuration (dense leg disabled per BACKLOG
# #47) measures recall 1.000 / MRR 0.988. A broken BM25/scoring path drops recall
# to ~0.28 and MRR to ~0.10, so these sit well above "broken" while leaving room
# for the handful of genuinely discriminating cases to move without flaking.
RECALL_FLOOR = 0.95
MRR_FLOOR = 0.80


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit a machine-readable report")
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory(prefix="alexandria-synthetic-") as workspace:
        engine = build_synthetic_engine(workspace, dense=False)
        entries = load_golden(GOLDEN_PATH)

        target_errors = verify_targets(entries, engine._corpus_root)
        if target_errors:
            print(f"synthetic-eval-gate FAILED: unresolvable golden targets: "
                  f"{', '.join(target_errors)}", file=sys.stderr)
            return 1

        report = run_eval(engine, entries)
        negatives = run_negative(engine, load_negative(NEGATIVE_PATH), k=5)
        try:
            separation_report = separation(report.results, negatives).to_dict()
        except ValueError:
            separation_report = None

    summary = report.summary
    failures = []
    if summary.errors:
        failures.append(f"{summary.errors} query error(s): {summary.error_ids}")
    if summary.recall_at_k < RECALL_FLOOR:
        failures.append(f"recall@k {summary.recall_at_k:.3f} < floor {RECALL_FLOOR}")
    if summary.mrr < MRR_FLOOR:
        failures.append(f"MRR {summary.mrr:.3f} < floor {MRR_FLOOR}")

    if args.json:
        print(json.dumps({
            "summary": summary.to_dict(),
            "separation": separation_report,
            "n_negatives": len(negatives),
            "failures": failures,
            "measures": "lexical harness correctness only, not retrieval quality",
        }, ensure_ascii=False, sort_keys=True))
    else:
        low, high = summary.recall_ci
        print(f"synthetic corpus: {report.corpus_chunks} chunks")
        print(f"golden:   n={summary.n} recall@k={summary.recall_at_k:.3f} "
              f"[{low:.3f}, {high:.3f}] MRR={summary.mrr:.3f} errors={summary.errors}")
        if summary.misses:
            print(f"misses:   {', '.join(summary.misses)}")
        # Observable, NOT an enforced check (BACKLOG #46): a score floor on
        # negatives was disproven as the wrong instrument (spec §9 Q5), so
        # `separable` is reported for visibility and must not be read as a pass.
        print(f"negative: n={len(negatives)} separable="
              f"{separation_report['separable'] if separation_report else 'n/a'} (observable, not gated)")
        print("measures the lexical harness, NOT retrieval quality on real knowledge.")

    if failures:
        print("synthetic-eval-gate FAILED: " + "; ".join(failures), file=sys.stderr)
        return 1
    if not args.json:
        print("synthetic-eval-gate: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
