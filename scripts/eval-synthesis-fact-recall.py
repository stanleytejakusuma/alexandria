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

from alexandria.eval.synthesis_fact_recall import (
    BAND_LOW,
    GATE_THRESHOLD,
    VERDICT_FINAL_FAIL,
    VERDICT_INVALID,
    VERDICT_PASS,
    VERDICT_PROVISIONAL_FAIL,
    _verdict,
    run_fact_recall_eval,
    verify_manifest,
)
from alexandria.eval.synthesis_golden import load_synthesis_golden
from alexandria.llm import LLMClient

DEFAULT_GOLDEN = Path.home() / "alexandria-corpus" / ".alexandria" / "golden" / "synthesis-clusters-v1.jsonl"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--pages", type=Path, default=None, help="dir with <cluster-id>.md (+ .skip-log.json)")
    p.add_argument("--gather", type=Path, default=None, help="dir with <cluster-id>.gather.json sidecars")
    p.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN)
    p.add_argument("--model-a", default="claude-fable-5")
    p.add_argument("--model-b", default="deepseek-v4-pro")
    p.add_argument("--base-url", default="http://127.0.0.1:20128/v1",
                   help="OpenAI-compatible gateway base URL (remote unattended gateway "
                        "for long runs)")
    p.add_argument("--api-key-env", default="ALEXANDRIA_LLM_KEY",
                   help="env var holding the gateway API key")
    p.add_argument("--adjudication", type=Path, default=None,
                   help="JSONL of {\"fact_id\": \"<cluster>::<fact>\", \"covered\": bool} "
                        "overrides applied to both graders before scoring")
    p.add_argument("--output", type=Path, default=None,
                   help="JSON destination (default docs/calibration/synthesis-fact-recall-v1-<UTC>.json)")
    p.add_argument("--verify", type=Path, default=None, metavar="REPORT.json",
                   help="verify a persisted report's manifest against current disk state and exit")
    p.add_argument("--replay", type=Path, default=None, metavar="REPORT.json",
                   help="apply --adjudication to a persisted report WITHOUT re-grading "
                        "(no LLM calls) and print the recomputed verdict")
    return p


def _load_adjudications(path: Path | None) -> dict[str, bool]:
    if path is None:
        return {}
    out: dict[str, bool] = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        fact_id, covered = row.get("fact_id"), row.get("covered")
        if not isinstance(fact_id, str) or not isinstance(covered, bool):
            raise ValueError(f"adjudication line {line_number}: need fact_id and covered")
        out[fact_id] = covered
    return out


def replay_report(report: dict, adjudications: dict[str, bool]) -> dict:
    """Apply adjudication overrides to a PERSISTED report and recompute
    consensus/contested/pooled/verdict WITHOUT re-grading (no LLM calls --
    adjudication is a delta over the already-graded verdicts). The report's
    raw agreement data is preserved untouched for audit."""
    known = set()
    for c in report.get("clusters", []):
        agreement = c.get("agreement")
        if c.get("status") == "graded":
            if agreement:
                known.update(f"{c['cluster_id']}::{v['fact_id']}"
                             for v in agreement["result_a"]["verdicts"])
            else:
                # pre-agreement-persistence reports: only covered/contested ids
                # are stored; misses are unrecoverable by id -- the adjudicated
                # facts must be in one of these lists.
                known.update(f"{c['cluster_id']}::{f}"
                             for f in (c.get("consensus_covered") or [])
                             + (c.get("contested_ids") or []))
    unknown = sorted(set(adjudications) - known)
    if unknown:
        raise ValueError(f"adjudication references unknown fact(s): {unknown}")

    clusters = []
    consensus_delta = 0
    contested_delta = 0
    for c in report["clusters"]:
        if c.get("status") != "graded":
            clusters.append(c)
            continue
        agreement = c.get("agreement")
        if agreement is None:
            # Aggregate-level replay: prior status recovered from the flattened
            # lists instead of raw verdicts. miss_taxonomy is not mutated
            # (miss fact ids aren't recoverable here) -- pooled counts still
            # recompute exactly, since totals are fixed.
            consensus = list(c.get("consensus_covered") or [])
            contested = list(c.get("contested_ids") or [])
            adjudicated_here = 0
            for fid in list(consensus) + list(contested):
                adj = adjudications.get(f"{c['cluster_id']}::{fid}")
                if adj is None:
                    continue
                adjudicated_here += 1
                if adj and fid not in consensus:
                    consensus.append(fid)
                    consensus_delta += 1
                    if fid in contested:
                        contested.remove(fid)
                        contested_delta -= 1
                elif adj is False:
                    if fid in consensus:
                        consensus.remove(fid)
                        consensus_delta -= 1
                    if fid in contested:
                        contested.remove(fid)
                        contested_delta -= 1
            n = (len(consensus) + len(contested) + len(c.get("miss_taxonomy") or [])) or 1
            c = dict(c)
            c["consensus_fact_count"] = len(consensus)
            c["consensus_covered"] = consensus
            c["contested_ids"] = contested
            c["consensus_recall"] = len(consensus) / n
            c["union_recall"] = (len(consensus) + len(contested)) / n
            c["adjudicated_fact_count"] = c.get("adjudicated_fact_count", 0) + adjudicated_here
            clusters.append(c)
            continue
        va = {v["fact_id"]: v for v in agreement["result_a"]["verdicts"]}
        vb = {v["fact_id"]: v for v in agreement["result_b"]["verdicts"]}
        ids = [v["fact_id"] for v in agreement["result_a"]["verdicts"]]
        consensus, contested = [], []
        adjudicated_here = 0
        for fid in ids:
            adj = adjudications.get(f"{c['cluster_id']}::{fid}")
            both = va[fid]["covered"] and vb[fid]["covered"]
            if adj is True:
                consensus.append(fid)
                adjudicated_here += 1
                if not both:
                    consensus_delta += 1
                if va[fid]["covered"] != vb[fid]["covered"]:
                    contested_delta -= 1
            elif adj is False:
                adjudicated_here += 1
                if both:
                    consensus_delta -= 1
            elif both:
                consensus.append(fid)
            elif va[fid]["covered"] != vb[fid]["covered"]:
                contested.append(fid)
        n = len(ids) or 1
        c = dict(c)
        c["consensus_fact_count"] = len(consensus)
        c["consensus_covered"] = consensus
        c["contested_ids"] = contested
        c["consensus_recall"] = len(consensus) / n
        c["union_recall"] = (len(consensus) + len(contested)) / n
        c["adjudicated_fact_count"] = c.get("adjudicated_fact_count", 0) + adjudicated_here
        clusters.append(c)

    out = dict(report)
    scored = out.get("scored_fact_count", 0) or 1
    consensus_count = out.get("consensus_count", 0) + consensus_delta
    contested_count = out.get("contested_count", 0) + contested_delta
    consensus_recall = consensus_count / scored
    graded = [c for c in clusters if c.get("status") == "graded"]
    macro = (sum(c["consensus_recall"] for c in clusters if c.get("status") != "measurement_invalid")
             / max(1, sum(1 for c in clusters if c.get("status") != "measurement_invalid")))
    out.update({
        "clusters": clusters,
        "consensus_count": consensus_count,
        "contested_count": contested_count,
        "pooled_consensus_recall": consensus_recall,
        "pooled_union_recall": (consensus_count + contested_count) / scored,
        "macro_consensus_recall": macro,
        "adjudicated_count": sum(c.get("adjudicated_fact_count", 0) for c in graded),
        "verdict": _verdict(consensus_recall, contested_count,
                             bool(out.get("invalid_cluster_ids"))),
    })
    return out


def _as_dict(report) -> dict:
    """Persist the FULL report: cluster fields plus the complete agreement
    (both graders' per-fact verdicts, evidence spans, and raw responses) -- the
    audit trail Red required. The flattened consensus_covered/contested_ids are
    kept for convenient table rendering; agreement is retained, never dropped."""
    data = asdict(report)
    for cluster in data["clusters"]:
        if cluster["agreement"] is not None:
            cluster["consensus_covered"] = list(cluster["agreement"]["consensus_covered"])
            cluster["contested_ids"] = list(cluster["agreement"]["contested_ids"])
    return data


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.replay is not None:
        report = json.loads(args.replay.read_text(encoding="utf-8"))
        adjudications = _load_adjudications(args.adjudication)
        replayed = replay_report(report, adjudications)
        print(f"replayed {args.replay} with {len(adjudications)} adjudication(s):")
        print(f"  pooled_consensus: {replayed['pooled_consensus_recall']:.3f} "
              f"({replayed['consensus_count']}/{replayed['scored_fact_count']})")
        print(f"  contested: {replayed['contested_count']}  "
              f"adjudicated: {replayed['adjudicated_count']}")
        print(f"  VERDICT: {replayed['verdict']}")
        if args.output is not None:
            args.output.write_text(json.dumps(replayed, indent=2, ensure_ascii=False) + "\n",
                                   encoding="utf-8")
            print(f"replayed report written: {args.output}")
        return 0
    if not args.pages or not args.gather:
        raise SystemExit("--pages and --gather are required for a live run")
    if args.verify is not None:
        report = json.loads(args.verify.read_text(encoding="utf-8"))
        problems = verify_manifest(report.get("manifest") or {},
                                   golden_path=args.golden,
                                   page_dir=args.pages, gather_dir=args.gather)
        if not problems:
            print(f"manifest verifies clean: {args.verify}")
            return 0
        print("manifest MISMATCHES:")
        for p in problems:
            print(f"  - {p}")
        return 1
    if not args.golden.exists() or not args.pages.is_dir() or not args.gather.is_dir():
        print("eval-synthesis-fact-recall: --golden must exist and --pages/--gather must be dirs",
              file=sys.stderr)
        return 2
    entries = load_synthesis_golden(args.golden)
    llm_a = LLMClient(model=args.model_a, base_url=args.base_url, api_key_env=args.api_key_env,
                      timeout=180, max_retries=3, base_delay=2.0, min_interval=0.5)
    llm_b = LLMClient(model=args.model_b, base_url=args.base_url, api_key_env=args.api_key_env,
                      timeout=240, max_retries=3, base_delay=2.0, min_interval=0.5)
    adjudications = _load_adjudications(args.adjudication)

    report = run_fact_recall_eval(entries, args.pages, args.gather, llm_a, llm_b,
                                  model_a=args.model_a, model_b=args.model_b,
                                  adjudications=adjudications, golden_path=args.golden)

    print(f"\nper-cluster (gate >= {int(GATE_THRESHOLD * 100)}%):")
    print(f"  {'cluster':<28} {'status':<19} {'recall_a':>8} {'recall_b':>8} {'consensus':>9} "
          f"{'union':>6} {'contested':>9}  errors")
    for c in report.clusters:
        print(f"  {c.cluster_id:<28} {c.status:<19} {c.recall_a:>8.2f} {c.recall_b:>8.2f} "
              f"{c.consensus_recall:>9.2f} {c.union_recall:>6.2f} {len(c.contested_ids):>9}  "
              f"{'; '.join(c.errors) if c.errors else '-'}")
    print(f"\npooled (n={report.total_facts} facts, scored={report.scored_fact_count}): "
          f"recall_a={report.pooled_recall_a:.3f} recall_b={report.pooled_recall_b:.3f} "
          f"consensus={report.pooled_consensus_recall:.3f} ({report.consensus_count}/{report.scored_fact_count}) "
          f"union={report.pooled_union_recall:.3f} macro={report.macro_consensus_recall:.3f} "
          f"contested={report.contested_count} adjudicated={report.adjudicated_count}")
    print(f"VERDICT: {report.verdict} (gate >= {int(GATE_THRESHOLD * 100)}%, "
          f"band [{int(BAND_LOW * 100)}%, {int(GATE_THRESHOLD * 100)}%))")
    if report.invalid_cluster_ids:
        print(f"\nMEASUREMENT INVALID (verdict {report.verdict}, never FAIL): "
              f"{', '.join(report.invalid_cluster_ids)}")
    if report.pipeline_failure_cluster_ids:
        print(f"pipeline failures (facts counted as misses): "
              f"{', '.join(report.pipeline_failure_cluster_ids)}")
    if report.verdict == VERDICT_PROVISIONAL_FAIL:
        print("\nPROVISIONAL: adjudication required before a final verdict -- "
              "supply --adjudication with contested/near-threshold facts")
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
    if report.verdict == VERDICT_INVALID:
        return 3                      # infrastructure failure, distinct from a recall FAIL
    if report.verdict == VERDICT_PASS:
        return 0
    return 1                          # PROVISIONAL_FAIL and FINAL_FAIL both block


if __name__ == "__main__":
    sys.exit(main())
