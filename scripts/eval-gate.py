#!/usr/bin/env python3
"""Regression gate for retrieval-tuning changes.

Exists because we broke this ourselves: a `depth=100` change was sound in isolation,
sound when tested against 20/12/8 prefetch sweeps -- and combined with a query-prefix
change it dropped recall@5 78.6%->64.3% before anyone measured the COMBINATION. RAGFlow
has the same failure class in its own history (PR #17774, a chunking-strategy revert;
PR #16104, a revert of a revert) -- "single-variable retrieval tuning looks good, then
has to be re-fought once combined with a second change" is a recognized, recurring
failure mode in this field, not a one-off mistake.

Fires ONLY on commits touching retrieval-relevant code -- an unrelated docs/CLI/connector
change should not pay eval wall-clock time. Requires a real corpus + built index; if
either is absent (a fresh clone, a CI box with no private corpus), this SKIPS rather
than blocks -- the leak scanner is unconditional because it's about safety, this is
about caution, and caution that blocks unrelated work stops being followed.

TWO GATES, TWO PURPOSES (BACKLOG #20). That skip is honest but it left the repo with
no gate at all for everyone else: on a fresh clone or in CI, a retrieval change was
measured by nothing. So a retrieval-relevant change now runs BOTH:

  1. scripts/synthetic-eval-gate.py -- unconditional, ~20s, over the synthetic corpus
     committed in tests/fixtures/. Verifies the LEXICAL harness: scoring, recall, the
     Wilson interval, the significance bar, manifest/FTS plumbing. Its dense leg is
     deliberately disabled (BACKLOG #47), so it can never tell you retrieval is GOOD
     and it says nothing about the dense leg -- that is the real gate's job.
  2. `alexandria eval --fail-on-regression` -- skipped without the private corpus.
     The only thing here that measures retrieval QUALITY.

A green (1) with (2) skipped means the instrument works and nothing has been said
about quality. Do not report it as the latter.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Paths whose change can move retrieval quality. Keep narrow -- broadening this list
# is how a "cheap, ~zero cost" gate becomes friction people route around.
WATCHED = (
    "src/alexandria/index/",
    "src/alexandria/retrieval/",
    "src/alexandria/config.py",
)

# Paths whose change can move SYNTHESIS quality (SPEC-phase2-eval.md: "eval-gate.py
# watched paths extend to src/alexandria/synthesis/"). The LLM-judge measurement
# itself (golden fact recall) needs the live gateway and is NOT run here -- a
# prompt change must re-measure via scripts/measure-synthesis.sh. This gate runs
# the fast offline regression net so a change that breaks parsing/accounting/
# verdict semantics fails immediately, locally.
SYNTHESIS_WATCHED = (
    "src/alexandria/synthesis/",
    "src/alexandria/audit.py",
    "src/alexandria/coverage.py",
    "src/alexandria/eval/synthesis_fact_recall.py",
    "src/alexandria/eval/gather_completeness.py",
)

SYNTHESIS_TESTS = (
    "tests/test_synthesis_pipeline.py",
    "tests/test_synthesis_fact_recall.py",
    "tests/test_synthesis_golden.py",
    "tests/test_synthesis_gather.py",
    "tests/test_audit.py",
    "tests/test_coverage.py",
    "tests/test_gather_completeness.py",
)

# The measuring instrument itself. Changing these cannot move retrieval quality,
# so they must NOT trigger the private quality gate -- that would spend 60-90s and
# append a history row for an edit that provably changed no retrieval behaviour,
# which is exactly the friction the WATCHED comment above warns about.
#
# They must, however, trigger the synthetic gate. Until this existed, editing the
# harness or its fixtures ran no gate at all: the one thing a change here can
# break is the only thing nothing was checking.
HARNESS_WATCHED = (
    "src/alexandria/eval/",
    "scripts/synthetic-eval-gate.py",
    "tests/test_synthetic_gate.py",
    "tests/fixtures/synthetic-",
)


def gates_to_run(changed: list[str]) -> set[str]:
    """Decide which gates a set of staged paths earns. Pure, so it can be tested.

    Returns any of: "synthesis", "synthetic", "quality".

    A retrieval change earns both the synthetic gate (is the instrument sound?)
    and the quality gate (did the number move?). A harness change earns only the
    former. A synthesis change earns its own offline net and, since 2026-08-13,
    no longer suppresses the others: the previous version returned immediately
    after the synthesis branch, so a commit touching synthesis AND retrieval was
    silently exempt from the retrieval gate entirely.
    """
    gates: set[str] = set()
    if any(path.startswith(SYNTHESIS_WATCHED) for path in changed):
        gates.add("synthesis")
    if any(path.startswith(WATCHED) for path in changed):
        gates.update({"synthetic", "quality"})
    if any(path.startswith(HARNESS_WATCHED) for path in changed):
        gates.add("synthetic")
    return gates


def staged_files() -> list[str]:
    r = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
        cwd=REPO, capture_output=True, text=True, check=False,
    )
    return [f for f in r.stdout.splitlines() if f.strip()]


def main() -> int:
    gates = gates_to_run(staged_files())
    if not gates:
        return 0  # nothing gate-relevant staged -- don't pay the cost

    if "synthesis" in gates:
        print("eval-gate: synthesis-relevant files changed, running offline "
              "regression net ...", file=sys.stderr)
        result = subprocess.run(
            [str(REPO / ".venv" / "bin" / "python"), "-m", "pytest",
             *SYNTHESIS_TESTS, "-q"],
            cwd=REPO, capture_output=True, text=True,
        )
        print(result.stdout)
        if result.returncode != 0:
            print("eval-gate FAILED -- synthesis regression net red. NOTE: prompt/"
                  "logic changes also require a LIVE re-measurement of the golden "
                  "fact-recall gate (scripts/measure-synthesis.sh); this offline "
                  "net only catches structural breaks.", file=sys.stderr)
            return 1
        print("eval-gate: synthesis regression net green (live re-measurement "
              "still required for prompt changes).", file=sys.stderr)

    # The reproducible half: runs everywhere, needs no private corpus and no
    # network. Deliberately BEFORE the skippable half, so the check that always
    # runs is also the one that always reports.
    if "synthetic" in gates:
        print("eval-gate: running the synthetic harness gate ...", file=sys.stderr)
        synthetic = subprocess.run(
            [sys.executable, str(REPO / "scripts" / "synthetic-eval-gate.py")],
            cwd=REPO, capture_output=True, text=True,
        )
        print(synthetic.stdout)
        if synthetic.returncode != 0:
            print(synthetic.stderr, file=sys.stderr)
            print("eval-gate FAILED -- the synthetic harness gate is red. This does "
                  "not mean retrieval quality dropped; it means the measuring "
                  "instrument itself is broken, which invalidates every number the "
                  "private gate would report next.", file=sys.stderr)
            return 1

    if "quality" not in gates:
        return 0

    corpus = Path.home() / "alexandria-corpus"
    golden = corpus / ".alexandria" / "golden" / "golden-v1.jsonl"
    index = corpus / ".alexandria" / "index"
    if not golden.exists() or not index.exists():
        print("eval-gate: private-corpus half SKIPPED (no private corpus/index on "
              "this machine -- expected on a fresh clone or CI box). The synthetic "
              "gate above passed: the harness works. Retrieval QUALITY was not "
              "measured by this commit.", file=sys.stderr)
        return 0

    print("eval-gate: running `alexandria eval --fail-on-regression` against the "
          "private corpus ...", file=sys.stderr)
    result = subprocess.run(
        [str(REPO / ".venv" / "bin" / "alexandria"), "eval", "--fail-on-regression"],
        cwd=REPO, capture_output=True, text=True,
    )
    print(result.stdout)
    if result.returncode != 0:
        print("eval-gate FAILED -- a golden-set query that was HIT in the last "
              "recorded run is now a MISS. This is the exact regression class that "
              "shipped depth=100 last time: fix the composition, or if the tradeoff "
              "is deliberate, record it explicitly rather than let the gate silence "
              "itself.", file=sys.stderr)
        return 1
    print("eval-gate: no regression vs the last recorded run.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
