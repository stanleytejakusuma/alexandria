#!/usr/bin/env python3
"""Calibrate coverage.py's LLM grader against coverage-calibration-v1.jsonl.

WHY THIS EXISTS: same reason calibrate-audit.py exists for the entailment grader --
coverage.py is about to become the mechanism phase 2's synthesis sweep uses to catch
silently-omitted load-bearing facts, and nobody has measured whether the grader itself
is accurate before trusting its verdicts. Ground truth here is
coverage-calibration-v1.jsonl (private corpus repo): 71 real (page_claims,
skipped_chunk, true_label) cases across 10 strata, built and independently
double-labeled per docs/RUBRIC-skip-log-audit.md -- see that document's Appendix D for
the full adjudication record of how those 71 cases got their labels.

REPORTING DISCIPLINE, per the rubric's own section 5 (do not violate it here just
because it would be convenient to print a single number):
  - Gate on POOLED metrics only: overall LB-recall (across strata 1,2,3,8) and overall
    SS-false-positive-rate (across strata 5,6,7,9,10).
  - Per-stratum numbers are RED-FLAG DETECTORS, reported as raw counts with Wilson 95%
    intervals, never bare percentages -- small-n strata (some as low as n=1 after
    adjudication) cannot support a precision claim and must not be allowed to look like
    one.
  - Stratum 4 (borderline) is reported separately: it measures grader/human agreement
    on the genuinely-contested boundary, not grader "accuracy" against a single right
    answer -- a borderline case has no single right answer by design.
"""

from __future__ import annotations

import argparse
import math
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from alexandria.coverage import grade_skip  # noqa: E402
from alexandria.eval.calibration_cases import STRATA, load_calibration_cases  # noqa: E402
from alexandria.llm import LLMClient  # noqa: E402

DEFAULT_PATH = Path.home() / "alexandria-corpus" / ".alexandria" / "golden" / "coverage-calibration-v1.jsonl"

LB_STRATA = {1, 2, 3, 8}
SS_STRATA = {5, 6, 7, 9, 10}
BORDERLINE_STRATA = {4}


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = successes / n
    denom = 1 + z**2 / n
    center = (p + z**2 / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2)) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def outcome_correct(case, verdict) -> bool:
    """Grader is 'correct' iff its LB/SS side matches the case's true side -- exact
    label_code match is not required (multiple codes can be defensible for one pair),
    matching the rubric's own falsifiability standard (a triple, not a fixed taxonomy
    slot) rather than a stricter bar the rubric itself doesn't set."""
    return verdict.label == case.true_label


def run(args) -> int:
    cases = load_calibration_cases(args.path)
    print(f"loaded {len(cases)} calibration cases from {args.path}", file=sys.stderr)

    grader = LLMClient(model=args.model, timeout=120, max_retries=4, base_delay=2.0, min_interval=0.5)

    results: list[tuple[object, object | None, str | None]] = []  # (case, verdict, error)

    def grade(case):
        try:
            v = grade_skip(grader, case.page_claims, case.skipped_chunk, case.id)
            return case, v, None
        except Exception as exc:  # one bad case must not crash the batch
            return case, None, f"{type(exc).__name__}: {str(exc)[:180]}"

    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(grade, case): case for case in cases}
        for fut in as_completed(futures):
            results.append(fut.result())
            done += 1
            if done % 10 == 0 or done == len(cases):
                print(f"  {done}/{len(cases)}", file=sys.stderr, flush=True)

    report(results)
    return 0


def report(results) -> None:
    errors = [r for r in results if r[2] is not None]
    graded = [(case, v) for case, v, err in results if err is None]

    print("\n" + "=" * 70)
    print("coverage.py calibration against coverage-calibration-v1 (real ground truth)")
    print("=" * 70)
    print(f"  graded: {len(graded)}   errors: {len(errors)}")

    by_stratum: dict[int, list[bool]] = defaultdict(list)
    for case, v in graded:
        by_stratum[case.stratum].append(outcome_correct(case, v))

    print(f"\n  {'#':>3} {'stratum':<52} {'n':>4} {'correct':>8} {'wilson 95% CI':>18}")
    print("  " + "-" * 90)
    for n, meta in STRATA.items():
        bools = by_stratum.get(n, [])
        if not bools:
            continue
        lo, hi = wilson_interval(sum(bools), len(bools))
        flag = "  <-- red flag" if bools and (sum(bools) / len(bools)) < 0.6 else ""
        print(f"  {n:>3} {meta['name']:<52} {len(bools):>4} {sum(bools):>4}/{len(bools):<3} "
              f"[{lo*100:>5.1f}%,{hi*100:>5.1f}%]{flag}")

    lb_bools = [b for s in LB_STRATA for b in by_stratum.get(s, [])]
    ss_bools = [b for s in SS_STRATA for b in by_stratum.get(s, [])]
    border_bools = [b for s in BORDERLINE_STRATA for b in by_stratum.get(s, [])]

    print("\n  POOLED (gate on these, not per-stratum numbers):")
    if lb_bools:
        recall = sum(lb_bools) / len(lb_bools)
        lo, hi = wilson_interval(sum(lb_bools), len(lb_bools))
        print(f"    LB recall (strata 1,2,3,8):  {recall*100:.1f}%  n={len(lb_bools)}  "
              f"95% CI [{lo*100:.1f}%,{hi*100:.1f}%]")
    if ss_bools:
        fp_rate = 1 - sum(ss_bools) / len(ss_bools)
        lo, hi = wilson_interval(len(ss_bools) - sum(ss_bools), len(ss_bools))
        print(f"    SS false-positive rate (strata 5,6,7,9,10): {fp_rate*100:.1f}%  "
              f"n={len(ss_bools)}  95% CI [{lo*100:.1f}%,{hi*100:.1f}%]")
    if border_bools:
        agree = sum(border_bools) / len(border_bools)
        print(f"    Stratum 4 (borderline) grader-agrees-with-construction rate: "
              f"{agree*100:.1f}%  n={len(border_bools)} "
              f"-- NOT an accuracy claim, borderline cases have no single right answer")

    if errors:
        print(f"\n  {len(errors)} grader error(s), first 5:")
        for case, _, err in errors[:5]:
            print(f"    {case.id}: {err}")

    print("\n  Reminder: this set is small (n={}) and single-labeled beyond the double-".format(len(results)))
    print("  labeling already recorded in RUBRIC-skip-log-audit.md Appendix D -- treat")
    print("  per-stratum numbers as red-flag detectors, not precision estimates.")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--path", type=Path, default=DEFAULT_PATH)
    p.add_argument("--model", default="claude-sonnet-5")
    p.add_argument("--workers", type=int, default=4)
    return p


if __name__ == "__main__":
    sys.exit(run(build_parser().parse_args()))
