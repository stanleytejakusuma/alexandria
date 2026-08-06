#!/usr/bin/env python3
"""Compare two persisted fact-recall reports (baseline vs current).

Pure offline: reads two report JSONs produced by eval-synthesis-fact-recall.py,
prints per-cluster and pooled deltas, and flags aggregation-version mismatches
so reports from different scoring rules are never silently compared.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def compare_reports(baseline: dict, current: dict) -> dict:
    """Return a delta summary. Pure, no I/O beyond what the caller passed."""
    base_ver = (baseline.get("manifest") or {}).get("aggregation_version")
    curr_ver = (current.get("manifest") or {}).get("aggregation_version")
    by_id = lambda rep: {c["cluster_id"]: c for c in rep.get("clusters", [])}
    base, curr = by_id(baseline), by_id(current)

    rows = []
    for cid in sorted(set(base) | set(curr)):
        b, c = base.get(cid), curr.get(cid)
        rows.append({
            "cluster_id": cid,
            "base_status": b["status"] if b else "absent",
            "curr_status": c["status"] if c else "absent",
            "base_consensus": b["consensus_recall"] if b else None,
            "curr_consensus": c["consensus_recall"] if c else None,
            "base_contested": len(b["contested_ids"]) if b else 0,
            "curr_contested": len(c["contested_ids"]) if c else 0,
        })

    def pooled(rep, key):
        return rep.get(key)

    return {
        "aggregation_version_match": base_ver == curr_ver and base_ver is not None,
        "base_aggregation_version": base_ver,
        "current_aggregation_version": curr_ver,
        "rows": rows,
        "base": {k: pooled(baseline, k) for k in
                 ("pooled_consensus_recall", "pooled_union_recall", "pooled_recall_a",
                  "pooled_recall_b", "macro_consensus_recall", "contested_count",
                  "verdict", "scored_fact_count")},
        "current": {k: pooled(current, k) for k in
                    ("pooled_consensus_recall", "pooled_union_recall", "pooled_recall_a",
                     "pooled_recall_b", "macro_consensus_recall", "contested_count",
                     "verdict", "scored_fact_count")},
    }


def _fmt(v, width=6):
    return f"{v:>{width}.3f}" if isinstance(v, (int, float)) else f"{str(v):>{width}}"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--baseline", type=Path, required=True)
    p.add_argument("--current", type=Path, required=True)
    args = p.parse_args(argv)

    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    current = json.loads(args.current.read_text(encoding="utf-8"))
    d = compare_reports(baseline, current)

    if d["base_aggregation_version"] != d["current_aggregation_version"]:
        print(f"WARNING: aggregation versions differ: "
              f"{d['base_aggregation_version']} -> {d['current_aggregation_version']} "
              f"(numbers are NOT directly comparable)")
    b, c = d["base"], d["current"]
    print("\npooled deltas:")
    for k in ("pooled_consensus_recall", "pooled_union_recall", "pooled_recall_a",
              "pooled_recall_b", "macro_consensus_recall", "contested_count",
              "scored_fact_count"):
        delta = ""
        if isinstance(b[k], (int, float)) and isinstance(c[k], (int, float)):
            delta = f"  (delta {c[k] - b[k]:+.3f})" if isinstance(b[k], float) else f"  (delta {c[k] - b[k]:+d})"
        print(f"  {k:<28} {b[k]} -> {c[k]}{delta}")
    print(f"  {'verdict':<28} {b['verdict']} -> {c['verdict']}")

    print("\nper cluster:")
    print(f"  {'cluster':<40} {'status':<19} {'consensus':>9} {'contested':>9}")
    for row in d["rows"]:
        cons = "n/a" if row["base_consensus"] is None else f"{row['base_consensus']:.2f}"
        cons2 = "n/a" if row["curr_consensus"] is None else f"{row['curr_consensus']:.2f}"
        print(f"  {row['cluster_id']:<40} {row['base_status'] + ' -> ' + row['curr_status']:<19} "
              f"{cons + ' -> ' + cons2:>13} {row['base_contested']} -> {row['curr_contested']:>4}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
