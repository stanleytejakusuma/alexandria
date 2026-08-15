#!/usr/bin/env python3
"""Full-corpus sweep driver (WORK-ORDER-phase2-full-sweep.md §2.2).

Loads the corpus, enumerates topic clusters once, runs the serial fold
(sweep.py), prints `n/total` progress per topic (the project's calibration
progress convention) and a final accounting summary. Long-running: resumes
from the checkpoint on re-invocation. Cost-cautious: serial, bounded
repair, one LLM pipeline per topic -- run only when the single-page gate
passes; for a small bounded run use --limit-docs.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from alexandria.cli import _build_search_engine, _cached_embedder, load_config
from alexandria.index.chunker import chunk_document
from alexandria.llm import LLMClient
from alexandria.synthesis.sweep import run_sweep

DEFAULT_CORPUS = Path.home() / "alexandria-corpus"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    p.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    p.add_argument("--limit-docs", type=int, default=None,
                   help="cap the corpus scan (bounded runs; the accounting check "
                        "then covers the scanned subset only)")
    p.add_argument("--checkpoint", type=Path, default=None)
    p.add_argument("--no-resume", action="store_true")
    p.add_argument("--topic-threshold", type=float, default=0.75)
    p.add_argument("--seed-k", type=int, default=8)
    p.add_argument("--retries", type=int, default=2)
    p.add_argument("--base-url", default="http://127.0.0.1:20128/v1")
    p.add_argument("--api-key-env", default="ALEXANDRIA_LLM_KEY")
    p.add_argument("--gather-model", default="deepseek-v4-pro")
    p.add_argument("--writer-model", default="deepseek-v4-pro")
    p.add_argument("--repair-model", default="deepseek-v4-pro")
    p.add_argument("--audit-model", default="deepseek-v4-flash")
    p.add_argument("--coverage-a", default="deepseek-v4-flash")
    p.add_argument("--coverage-b", default="deepseek-v4-pro")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    corpus = Path(args.corpus).expanduser()

    config = load_config(corpus_override=corpus)
    engine = _build_search_engine(config, corpus)
    embedder = _cached_embedder(config, corpus)

    paths = sorted(corpus.rglob("*.md"))
    if args.limit_docs:
        paths = paths[: args.limit_docs]
    chunks = []
    for path in paths:
        rel = str(path.relative_to(corpus))[:-3]
        doc = path.read_text(encoding="utf-8", errors="replace")
        chunks.extend(chunk_document(rel, doc))
    print(f"sweep: {len(chunks)} chunks from {len(paths)} documents", flush=True)

    clients = {
        "gather": LLMClient(args.gather_model, base_url=args.base_url, api_key_env=args.api_key_env),
        "writer": LLMClient(args.writer_model, base_url=args.base_url, api_key_env=args.api_key_env),
        "repair": LLMClient(args.repair_model, base_url=args.base_url, api_key_env=args.api_key_env),
        "audit": LLMClient(args.audit_model, base_url=args.base_url, api_key_env=args.api_key_env),
        "coverage_a": LLMClient(args.coverage_a, base_url=args.base_url, api_key_env=args.api_key_env),
        "coverage_b": LLMClient(args.coverage_b, base_url=args.base_url, api_key_env=args.api_key_env),
    }

    # progress shim around the sweep: re-invoke with a progress-aware
    # pipeline wrapper so n/total lines stream as topics complete.
    total_holder = {"n": 0}

    def progress_pipeline(engine_, topic_query, **kw):
        started = time.monotonic()
        from alexandria.synthesis.pipeline import run_pipeline
        result = run_pipeline(
            engine_, topic_query,
            gather_llm=clients["gather"], writer_llm=clients["writer"],
            repair_llm=clients["repair"], audit_llm=clients["audit"],
            coverage_llm_a=clients["coverage_a"], coverage_llm_b=clients["coverage_b"],
            corpus_root=corpus, seed_k=args.seed_k,
            writer_model=args.writer_model, prompt_version="v1",
        )
        total_holder["n"] += 1
        status = "emitted" if result.emitted else "FAILED"
        print(f"{total_holder['n']}/{total_holder['total']} {status} "
              f"({time.monotonic() - started:.0f}s) {topic_query[:60]!r}", flush=True)
        return result

    result = run_sweep(
        chunks, engine,
        gather_llm=clients["gather"], writer_llm=clients["writer"],
        repair_llm=clients["repair"], audit_llm=clients["audit"],
        coverage_llm_a=clients["coverage_a"], coverage_llm_b=clients["coverage_b"],
        topic_threshold=args.topic_threshold,
        embedder=embedder,
        corpus_root=corpus,
        checkpoint_path=args.checkpoint or corpus / ".alexandria" / "sweep.json",
        resume=not args.no_resume,
        seed_k=args.seed_k,
        writer_model=args.writer_model,
        prompt_version="v1",
        pipeline_impl=progress_pipeline,
    )

    print(f"\nsweep complete: {len(result.pages)} page(s) emitted, "
          f"{len(result.failed_topics)} topic(s) failed, "
          f"{len(result.linked_topics)} linked to prior coverage, "
          f"{len(result.excluded_docs)} document(s) excluded "
          f"(no_cluster_match: {sum(1 for r in result.excluded_docs.values() if r == 'no_cluster_match')})")
    for cid in result.failed_topics:
        print(f"  FAILED topic: {cid}", file=sys.stderr)
    return 1 if result.failed_topics else 0


if __name__ == "__main__":
    sys.exit(main())
