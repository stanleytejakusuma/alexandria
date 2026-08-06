"""Full-corpus sweep: the serial map-reduce fold over topic clusters.

Implements `docs/DECISIONS-phase2-execution-model.md`: exhaustive,
cluster-based enumeration, processed as a STRICTLY SERIAL fold with
side-effect-free nodes. This is not a performance default -- serial is a
direct response to a confirmed gateway request-cross-contamination bug;
re-introducing concurrency here is a spec violation.

Accounting discipline, one altitude above Judge 2's chunk version: every
document either lands in a processed topic cluster or is logged with a
deterministic exclusion reason (`no_cluster_match` for singleton clusters).
An unaccounted document raises -- never warns.

Cross-page redundancy: the fold state accumulates a covered map
(chunk_id -> page path). A topic whose gathered chunks are ALL already
covered by earlier pages links to the prior page instead of re-synthesizing
(the same near-duplicate failure class that hit ground-truth construction
three separate times).

Resumability: the fold state is persisted after every topic (atomic
temp+rename write); an interrupted sweep resumes from its checkpoint.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ..index.chunker import Chunk
from .clustering import TOPIC_THRESHOLD, TopicCluster, find_topic_clusters
from .pipeline import run_pipeline

__all__ = ["SweepResult", "run_sweep"]


@dataclass(frozen=True)
class SweepResult:
    topics: tuple[TopicCluster, ...]  # multi-member topics, processing order
    excluded_docs: dict[str, str]  # doc_id -> exclusion reason
    pages: tuple[str, ...]  # emitted page paths (relative to corpus root)
    failed_topics: tuple[str, ...]  # cluster ids that never emitted
    linked_topics: tuple[str, ...]  # cluster ids that linked to prior pages


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, sort_keys=True)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def run_sweep(
    chunks: list[Chunk],
    engine,
    *,
    gather_llm=None,
    writer_llm=None,
    repair_llm=None,
    audit_llm=None,
    coverage_llm_a=None,
    coverage_llm_b=None,
    topic_threshold: float = TOPIC_THRESHOLD,
    embedder=None,
    corpus_root: str | Path = "~/alexandria-corpus",
    checkpoint_path: str | Path | None = None,
    resume: bool = False,
    seed_k: int = 8,
    writer_model: str | None = None,
    prompt_version: str = "v1",
    pipeline_impl=None,
    clustering_impl=None,
) -> SweepResult:
    """Run the sweep. `pipeline_impl`/`clustering_impl` are injectable for
    tests; defaults bind the real modules. Returns the final fold state."""
    root = Path(corpus_root).expanduser()
    checkpoint_path = Path(checkpoint_path) if checkpoint_path else root / "sweep.json"

    clustering_impl = clustering_impl or find_topic_clusters
    chunk_by_id = {c.chunk_id: c for c in chunks}
    doc_ids = sorted({c.doc_id for c in chunks})
    all_chunk_ids = {c.chunk_id for c in chunks}

    def default_pipeline(engine_, topic_query, **kw):
        if None in (gather_llm, writer_llm, repair_llm, audit_llm,
                    coverage_llm_a, coverage_llm_b):
            raise ValueError("run_sweep needs all six LLM clients "
                             "unless pipeline_impl is injected")
        return run_pipeline(
            engine_, topic_query,
            gather_llm=gather_llm, writer_llm=writer_llm, repair_llm=repair_llm,
            audit_llm=audit_llm, coverage_llm_a=coverage_llm_a,
            coverage_llm_b=coverage_llm_b,
            corpus_root=root, seed_k=seed_k, writer_model=writer_model,
            prompt_version=prompt_version, **kw,
        )

    pipeline_impl = pipeline_impl or default_pipeline

    # ---- enumeration: fixed once per run -------------------------------
    clusters = clustering_impl(chunks, threshold=topic_threshold, embedder=embedder)
    # Processing order: multi-member topics first, biggest first (the covered
    # map builds from the most informative pages), then cluster_id for
    # determinism. Singleton clusters are excluded, never processed.
    topics = sorted(
        (c for c in clusters if len(c.member_ids) > 1),
        key=lambda c: (-len(c.member_ids), c.cluster_id),
    )
    excluded_docs: dict[str, str] = {}
    for c in clusters:
        if len(c.member_ids) <= 1:
            for mid in c.member_ids:
                excluded_docs[chunk_by_id[mid].doc_id] = "no_cluster_match"

    # ---- resume state ----------------------------------------------------
    covered: dict[str, str] = {}
    completed: set[str] = set()
    pages: list[str] = []
    failed: list[str] = []
    linked: list[str] = []
    if resume and checkpoint_path.exists():
        state = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        covered = state.get("covered", {})
        completed = set(state.get("completed", []))
        pages = list(state.get("pages", []))
        failed = list(state.get("failed_topics", []))
        linked = list(state.get("linked_topics", []))

    # ---- the serial fold --------------------------------------------------
    for topic in topics:
        if topic.cluster_id in completed:
            continue
        result = pipeline_impl(
            engine, topic.representative_text,
            gather_llm=gather_llm, writer_llm=writer_llm, repair_llm=repair_llm,
            audit_llm=audit_llm, coverage_llm_a=coverage_llm_a,
            coverage_llm_b=coverage_llm_b, corpus_root=root, seed_k=seed_k,
            writer_model=writer_model, prompt_version=prompt_version,
        )
        gathered_ids = sorted({ch.chunk_id for ch in result.gathered.chunks})
        # Cross-page redundancy guard: all gathered chunks already covered?
        if gathered_ids and all(cid in covered for cid in gathered_ids):
            linked.append(topic.cluster_id)
            completed.add(topic.cluster_id)
        elif result.emitted and result.page_path is not None:
            rel = str(Path(result.page_path).resolve().relative_to(root.resolve()))
            pages.append(rel)
            for cid in gathered_ids:
                covered[cid] = rel
            completed.add(topic.cluster_id)
        else:
            failed.append(topic.cluster_id)
            completed.add(topic.cluster_id)
        _atomic_write(checkpoint_path, {
            "topics": [{"cluster_id": c.cluster_id, "member_ids": list(c.member_ids),
                        "representative_text": c.representative_text} for c in topics],
            "excluded_docs": excluded_docs,
            "covered": covered,
            "completed": sorted(completed),
            "pages": pages,
            "failed_topics": failed,
            "linked_topics": linked,
        })

    # ---- exhaustive accounting: every doc accounted, exactly once ---------
    processed_docs = {doc for c in topics
                      for doc in {chunk_by_id[m].doc_id for m in c.member_ids}}
    overlap = processed_docs & set(excluded_docs)
    if overlap:
        raise RuntimeError(f"sweep accounting FAILED: docs in both a processed "
                           f"cluster and the exclusion log: {sorted(overlap)[:5]}")
    unaccounted = sorted(set(doc_ids) - processed_docs - set(excluded_docs))
    if unaccounted:
        raise RuntimeError(
            f"sweep accounting FAILED: {len(unaccounted)} document(s) neither "
            f"processed nor excluded: {unaccounted[:5]}{'...' if len(unaccounted) > 5 else ''}"
        )

    return SweepResult(
        topics=tuple(topics),
        excluded_docs=excluded_docs,
        pages=tuple(pages),
        failed_topics=tuple(failed),
        linked_topics=tuple(linked),
    )
