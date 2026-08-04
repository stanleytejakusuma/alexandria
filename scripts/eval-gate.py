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


def staged_files() -> list[str]:
    r = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
        cwd=REPO, capture_output=True, text=True, check=False,
    )
    return [f for f in r.stdout.splitlines() if f.strip()]


def main() -> int:
    changed = staged_files()
    if not any(f.startswith(WATCHED) for f in changed):
        return 0  # nothing retrieval-relevant staged -- don't pay the cost

    corpus = Path.home() / "alexandria-corpus"
    golden = corpus / ".alexandria" / "golden" / "golden-v1.jsonl"
    index = corpus / ".alexandria" / "index"
    if not golden.exists() or not index.exists():
        print("eval-gate: SKIPPED (no private corpus/index on this machine -- "
              "expected on a fresh clone or CI box)", file=sys.stderr)
        return 0

    print("eval-gate: retrieval-relevant files changed, running "
          "`alexandria eval --fail-on-regression` ...", file=sys.stderr)
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
