#!/usr/bin/env python3
"""CLI over the golden fact-recall evaluator (WORK-ORDER-phase2-fact-recall-eval.md).

Thin wrapper: arg parsing, printing, persistence. All logic lives in
alexandria.eval.synthesis_fact_recall. Grades frozen pages produced by
scripts/synthesize-golden-pages.py with two independent model-family graders,
reports per-cluster and pooled recall (individual + conservative consensus +
union), lists every disagreement for manual adjudication, and writes the full
report (with evidence spans and miss taxonomy) to --output. A number that
lives only in terminal output is not a measured number.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from alexandria.eval.synthesis_fact_recall import GATE_THRESHOLD, run_fact_recall_eval
from alexandria.eval.synthesis_golden import load_synthesis_golden
from alexandria.llm import LLMClient

DEFAULT_GOLDEN = Path.home() / "alexandria-corpus" / ".alexandria" / "golden" / "synthesis-clusters-v1.jsonl"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--pages", type=Path, required=True, help="dir with <cluster-id>.md (+ .skip-log.json)")
    p.add_argument("--gather", type=Path, required=True, help="dir with <cluster-id>.gather.json sidecars")
    p.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN)
    p.add_argument("--model-a", default="claude-fable-5")
    p.add_argument("--model-b", default="deepseek-v4-pro")
    p.add_argument("--output", type=Path, default=None,
                   help="JSON destination (default docs/calibration/synthesis-fact-recall-v1-<UTC>.json)")
    return p


def _as_dict(report) -> dict:
    data = asdict(report)
    for cluster in data["clusters"]:
        if cluster["agreement"] is not None:
            cluster["consensus_covered"] = list(cluster["agreement"]["consensus_covered"])
            cluster["contested_ids"] = list(cluster["agreement"]["contested_ids"])
        cluster["agreement"] = None  # flattened above; verdicts live in per-fact report
    return data


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.golden.exists() or not args.pages.is_dir() or not args.gather.is_dir():
        print("eval-synthesis-fact-recall: --golden must exist and --pages/--gather must be dirs",
              file=sys.stderr)
        return 2
    entries = load_synthesis_golden(args.golden)
    llm_a = LLMClient(model=args.model_a, timeout=180, max_retries=3, base_delay=2.0, min_interval=0.5)
    llm_b = LLMClient(model=args.model_b, timeout=240, max_retries=3, base_delay=2.0, min_interval=0.5)

    report = run_fact_recall_eval(entries, args.pages, args.gather, llm_a, llm_b,
                                  model_a=args.model_a, model_b=args.model_b)

    print(f"\nper-cluster (gate >= {int(GATE_THRESHOLD * 100)}%):")
    print(f"  {'cluster':<28} {'recall_a':>8} {'recall_b':>8} {'consensus':>9} "
          f"{'union':>6} {'contested':>9}  errors")
    for c in report.clusters:
        print(f"  {c.cluster_id:<28} {c.recall_a:>8.2f} {c.recall_b:>8.2f} "
              f"{c.consensus_recall:>9.2f} {c.union_recall:>6.2f} {len(c.contested_ids):>9}  "
              f"{'; '.join(c.errors) if c.errors else '-'}")
    print(f"\npooled (n={report.total_facts} facts): recall_a={report.pooled_recall_a:.3f} "
          f"recall_b={report.pooled_recall_b:.3f} "
          f"consensus={report.pooled_consensus_recall:.3f} union={report.pooled_union_recall:.3f} "
          f"contested={report.contested_count}")
    print(f"GATE: {'PASS' if report.gate else 'FAIL'} (consensus recall "
          f"{report.pooled_consensus_recall:.3f} vs {GATE_THRESHOLD:.2f})")
    if report.contested_count:
        print("\ncontested facts (manual adjudication required):")
        for c in report.clusters:
            for fid in c.contested_ids:
                print(f"  {c.cluster_id}::{fid}")
    for c in report.clusters:
        for row in c.miss_taxonomy:
            print(f"  MISS {c.cluster_id}::{row['fact_id']} "
                  f"[{row['classification']}]: {row['fact_text'][:120]}")

    output = args.output or (Path("docs") / "calibration" /
                             f"synthesis-fact-recall-v1-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = _as_dict(report)
    payload["command"] = " ".join(sys.argv)
    output.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"\nreport written: {output}")
    return 0 if report.gate else 1


if __name__ == "__main__":
    sys.exit(main())
