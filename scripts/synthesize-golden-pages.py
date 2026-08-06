#!/usr/bin/env python3
"""Measurement driver: produce the frozen per-cluster page set for the
golden fact-recall evaluator (WORK-ORDER-phase2-fact-recall-eval.md).

Serial, side-effect-free, isolated outputs. Explicitly NOT the full-sweep
orchestrator (that is a separate work order): no scheduling, no clustering,
no dedup, no persistent gather state.

The pipeline's own native checks (chunk accounting, entailment, sampled
skip-log coverage, anti-gutting repair) run inside run_pipeline; this driver
only freezes the outputs into a deterministic layout the evaluator consumes:

    <out>/pages/<cluster-id>.md
    <out>/pages/<cluster-id>.skip-log.json
    <out>/gather/<cluster-id>.gather.json

A cluster whose pipeline fails (or raises) is DATA, not an exception: its
gather sidecar records emitted=false and the driver continues. Live LLM calls
are made when this script runs -- it is a measurement tool, not part of CI.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from alexandria.cli import _build_search_engine
from alexandria.config import load_config
from alexandria.eval.synthesis_golden import SynthesisClusterEntry, load_synthesis_golden
from alexandria.llm import LLMClient, LLMError
from alexandria.synthesis.judge import ChunkAccountingError
from alexandria.synthesis.pipeline import run_pipeline

DEFAULT_GOLDEN = Path.home() / "alexandria-corpus" / ".alexandria" / "golden" / "synthesis-clusters-v1.jsonl"
CORPUS = Path.home() / "alexandria-corpus"


def _pipeline_page_failure(*args, **kwargs):
    """run_pipeline raises ValueError when an LLM emits structurally-wrong page
    JSON (write.py parse_page_response) -- an expected pipeline failure, not a
    driver bug. Converting at this boundary keeps the driver's catch scoped to
    (LLMError, ChunkAccountingError); any other ValueError propagates and aborts
    the batch (Red round 2: generic ValueError catching is too broad)."""
    try:
        return run_pipeline(*args, **kwargs)
    except ValueError as exc:
        raise LLMError(f"pipeline page-validation failure: {exc}") from exc


def synthesize_golden_pages(entries: Sequence[SynthesisClusterEntry], out_dir: Path,
                            engine, clients: dict[str, Any], *, seed_k: int = 8,
                            retries: int = 0) -> list[dict]:
    """Run the single-page pipeline for every cluster in order, freezing outputs.

    clients must provide gather/writer/repair/audit/coverage_a/coverage_b LLM
    clients (LLMClient or a test double). Returns one sidecar dict per cluster;
    a failed cluster yields emitted=false plus the error, never an abort.

    retries: emission is stochastic (measured 2026-08-06: 2 of 3 v1 emission
    failures emitted cleanly on re-run), so each cluster gets up to 1+retries
    attempts; per-attempt outcomes are recorded in the sidecar so the emission
    RATE is measurable rather than a single draw.
    """
    pages_dir = out_dir / "pages"
    gather_dir = out_dir / "gather"
    pages_dir.mkdir(parents=True, exist_ok=True)
    gather_dir.mkdir(parents=True, exist_ok=True)

    results: list[dict] = []
    for entry in entries:
        started = time.monotonic()
        row: dict[str, object] = {
            "cluster_id": entry.id,
            "topic": entry.topic,
            "emitted": False,
            "native_passed": False,
            "error": None,
            "gathered_doc_ids": [],
            "gathered_chunk_ids": [],
            "gathered_chunk_count": 0,
            "round_one_count": 0,
            "round_two_count": 0,
            "follow_up_queries": [],
            "repair_iterations": None,
            "failed_claim_details": [],
            "page_sha256": None,
            "attempt_count": 0,
            "attempts": [],
            "duration_seconds": None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        for attempt in range(1 + max(0, int(retries))):
            attempt_started = time.monotonic()
            row["error"] = None
            try:
                result = _pipeline_page_failure(
                    engine, entry.topic,
                    gather_llm=clients["gather"],
                    writer_llm=clients["writer"],
                    repair_llm=clients["repair"],
                    audit_llm=clients["audit"],
                    coverage_llm_a=clients["coverage_a"],
                    coverage_llm_b=clients["coverage_b"],
                    corpus_root=out_dir,
                    seed_k=seed_k,
                    writer_model=str(getattr(clients["writer"], "model", "scripted")),
                    prompt_version="v1",
                )
                if result.emitted and result.page_path is not None:
                    _copy_outputs(result.page_path, result.skip_log_path, pages_dir, entry.id)
                failed_ids = set(result.repair.verdict.failed_claim_ids)
                row["failed_claim_details"] = [
                    {"id": c.id, "text": c.text,
                     "citations": [{"doc_id": ct.doc_id, "chunk_id": ct.chunk_id}
                                    for ct in c.citations]}
                    for c in result.repair.page.claims if c.id in failed_ids
                ]
                row.update({
                    "emitted": result.emitted,
                    "native_passed": bool(result.repair.passed),
                    "gathered_doc_ids": sorted({chunk.doc_id for chunk in result.gathered.chunks}),
                    "gathered_chunk_ids": sorted({chunk.chunk_id for chunk in result.gathered.chunks}),
                    "gathered_chunk_count": len(result.gathered.chunks),
                    "round_one_count": len(result.gathered.round_one),
                    "round_two_count": len(result.gathered.round_two),
                    "follow_up_queries": list(result.gathered.follow_up_queries),
                    "repair_iterations": result.repair.iterations,
                    # final-state verdict details: why the loop stopped (diagnostic).
                    "repair_errors": list(result.repair.errors),
                    "final_claim_count": len(result.repair.page.claims),
                    "final_skip_log_count": len(result.repair.page.skip_log),
                    "chunk_accounted": bool(result.repair.verdict.chunk_accounted),
                    "entailment_passed": bool(result.repair.verdict.entailment_passed),
                    "coverage_passed": bool(result.repair.verdict.coverage_passed),
                    "failed_claim_ids": list(result.repair.verdict.failed_claim_ids),
                    "failing_skip_ids": list(result.repair.verdict.failing_skip_ids),
                    "borderline_skip_ids": list(result.repair.verdict.borderline_skip_ids),
                    "judge_errors": list(result.repair.verdict.errors),
                })
                attempt_error = None
            except (LLMError, ChunkAccountingError) as exc:
                # Expected pipeline failure modes -- a failed page is data, not an
                # abort. Anything else (a bug in the driver itself, I/O, assertion
                # failures) must propagate: recording programmer errors as pipeline
                # misses conflates "system failed" with "measurement invalid" (Red
                # review, 2026-08-05; the Path.copy bug was exactly this trap).
                row["error"] = f"{type(exc).__name__}: {exc}"
                attempt_error = row["error"]
            row["attempts"].append({
                "attempt": attempt + 1,
                "emitted": bool(row["emitted"]),
                "error": attempt_error,
                "duration_seconds": round(time.monotonic() - attempt_started, 2),
            })
            if row["emitted"]:
                break
        row["attempt_count"] = len(row["attempts"])
        row["duration_seconds"] = round(time.monotonic() - started, 2)
        if row["emitted"] and (pages_dir / f"{entry.id}.md").exists():
            import hashlib
            row["page_sha256"] = hashlib.sha256(
                (pages_dir / f"{entry.id}.md").read_bytes()).hexdigest()
        else:
            row["page_sha256"] = None
        (gather_dir / f"{entry.id}.gather.json").write_text(
            json.dumps(row, indent=2, sort_keys=True) + "\n", encoding="utf-8",
        )
        print(f"  {entry.id}: emitted={row['emitted']} native_passed={row['native_passed']} "
              f"chunks={row['gathered_chunk_count']} "
              f"repairs={row['repair_iterations']} {row['duration_seconds']}s"
              + (f"  ERROR: {row['error']}" if row["error"] else ""), flush=True)
        results.append(row)
    return results


def _copy_outputs(page_path, skip_log_path, pages_dir: Path, cluster_id: str) -> None:
    if page_path is not None and Path(page_path).exists():
        shutil.copyfile(Path(page_path), Path(pages_dir) / f"{cluster_id}.md")
    if skip_log_path is not None and Path(skip_log_path).exists():
        shutil.copyfile(Path(skip_log_path), Path(pages_dir) / f"{cluster_id}.skip-log.json")


def _client(model: str, base_url: str, api_key_env: str) -> LLMClient:
    return LLMClient(model=model, base_url=base_url, api_key_env=api_key_env,
                     timeout=180, max_retries=3, base_delay=2.0, min_interval=0.5)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN)
    p.add_argument("--out", type=Path, required=True, help="output dir (created if missing)")
    p.add_argument("--limit", type=int, default=None, help="cap the number of clusters (smoke runs)")
    p.add_argument("--only", type=str, default=None,
                   help="run a single cluster id (diagnostic re-runs)")
    p.add_argument("--seed-k", type=int, default=8)
    p.add_argument("--retries", type=int, default=0,
                   help="extra pipeline attempts per cluster (emission is stochastic; "
                        "measured 2026-08-06)")
    p.add_argument("--base-url", default="http://127.0.0.1:20128/v1",
                   help="OpenAI-compatible gateway base URL (the remote unattended "
                        "gateway for long/unattended runs)")
    p.add_argument("--api-key-env", default="ALEXANDRIA_LLM_KEY",
                   help="env var holding the gateway API key")
    p.add_argument("--gather-model", default="claude-sonnet-5")
    p.add_argument("--writer-model", default="claude-sonnet-5")
    p.add_argument("--repair-model", default="claude-sonnet-5")
    p.add_argument("--audit-model", default="claude-fable-5")
    p.add_argument("--coverage-a", default="claude-fable-5")
    p.add_argument("--coverage-b", default="deepseek-v4-pro")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.golden.exists():
        print(f"synthesize-golden-pages: golden file not found: {args.golden}", file=sys.stderr)
        return 2
    entries = load_synthesis_golden(args.golden)
    if args.only is not None:
        entries = [e for e in entries if e.id == args.only]
        if not entries:
            print(f"synthesize-golden-pages: no cluster with id {args.only!r}", file=sys.stderr)
            return 2
    elif args.limit is not None:
        entries = entries[: args.limit]
    if not entries:
        print("synthesize-golden-pages: no clusters to synthesize", file=sys.stderr)
        return 2

    print(f"loading search engine over {CORPUS} ...", flush=True)
    engine = _build_search_engine(load_config(corpus_override=CORPUS), CORPUS)
    engine.search("warmup")
    print(f"engine warmed; synthesizing {len(entries)} cluster(s) serially ...", flush=True)

    clients = {
        "gather": _client(args.gather_model, args.base_url, args.api_key_env),
        "writer": _client(args.writer_model, args.base_url, args.api_key_env),
        "repair": _client(args.repair_model, args.base_url, args.api_key_env),
        "audit": _client(args.audit_model, args.base_url, args.api_key_env),
        "coverage_a": _client(args.coverage_a, args.base_url, args.api_key_env),
        "coverage_b": _client(args.coverage_b, args.base_url, args.api_key_env),
    }
    started = time.monotonic()
    results = synthesize_golden_pages(entries, args.out, engine, clients,
                                      seed_k=args.seed_k, retries=args.retries)
    emitted = sum(1 for r in results if r["emitted"])
    print(f"\ndone: {emitted}/{len(results)} clusters emitted a page in "
          f"{round(time.monotonic() - started, 1)}s total", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
