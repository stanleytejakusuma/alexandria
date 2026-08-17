"""Thin deterministic composition for one gather → write → judge → repair page."""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ..corpus import render, slugify
from .gather import GatherResult, gather
from .repair import RepairResult, repair_until_done
from .write import SynthesisPage, write_page

__all__ = ["PipelineResult", "run_pipeline"]


@dataclass(frozen=True)
class PipelineResult:
    gathered: GatherResult
    repair: RepairResult
    emitted: bool
    page_path: Path | None
    skip_log_path: Path | None
    timings_ms: dict[str, int] = field(default_factory=dict)


# Stage mapping for the audit trail: the user-visible names are
# retrieve (gather: query + pool building over the index),
# augment (write: prompt+page assembly),
# generate (repair: judge + repair + coverage loops).
_STAGES = ("retrieve", "augment", "generate")


def _timed(fn, *args, **kwargs) -> tuple[object, int]:
    t0 = time.perf_counter()
    out = fn(*args, **kwargs)
    return out, int((time.perf_counter() - t0) * 1000)


def run_pipeline(engine, topic_query: str, *, gather_llm, writer_llm, repair_llm, audit_llm,
                 coverage_llm_a, coverage_llm_b, corpus_root: str | Path = "~/alexandria-corpus",
                 seed_k: int = 8, writer_model: str | None = None,
                 prompt_version: str = "v1", seed_chunks: Sequence = (),
                 max_follow_up_queries: int = 2, audit_concurrency: int = 4) -> PipelineResult:
    """Run a single page without any full-corpus scheduling or persistent gather state.

    seed_chunks: known-relevant chunks for the gather pool (a topic cluster's
    member docs in the sweep / golden driver). Passed through to gather()."""
    if writer_llm is audit_llm:
        raise ValueError("writer and entailment grader must be different clients")
    if coverage_llm_a is coverage_llm_b:
        raise ValueError("coverage grading requires two independent clients")
    timings: dict[str, int] = {}
    gathered, timings["retrieve"] = _timed(
        gather, engine, topic_query, llm=gather_llm, seed_k=seed_k,
        seed_chunks=seed_chunks, max_follow_up_queries=max_follow_up_queries)
    page, timings["augment"] = _timed(
        write_page, gathered, topic_query, llm=writer_llm, model=writer_model,
        prompt_version=prompt_version)
    repair, timings["generate"] = _timed(
        repair_until_done, gathered, page, repair_llm=repair_llm,
        audit_llm=audit_llm, coverage_llm_a=coverage_llm_a,
        coverage_llm_b=coverage_llm_b, audit_concurrency=audit_concurrency)
    if not repair.passed:
        return PipelineResult(gathered, repair, False, None, None, timings)
    page_path, skip_log_path = _emit(repair.page, corpus_root)
    return PipelineResult(gathered, repair, True, page_path, skip_log_path, timings)


def _emit(page: SynthesisPage, corpus_root: str | Path) -> tuple[Path, Path]:
    wiki = Path(corpus_root).expanduser() / "wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    filename = slugify(page.topic_query)
    page_path = wiki / f"{filename}.md"
    skip_log_path = wiki / f"{filename}.skip-log.json"

    sources = []
    seen_doc_ids: set[str] = set()
    for claim in page.claims:
        for citation in claim.citations:
            if citation.doc_id not in seen_doc_ids:
                seen_doc_ids.add(citation.doc_id)
                sources.append({"id": citation.doc_id, "resource": citation.doc_id})
    frontmatter: dict[str, object] = {
        "type": "concept",
        "title": page.topic_query,
        "author": page.author,
        "sources": sources,
    }
    if page.visibility is not None:
        frontmatter["visibility"] = page.visibility
    body = _render_page_body(page)
    page_path.write_text(render(frontmatter, body), encoding="utf-8")

    skip_payload: dict[str, object] = {
        "author": page.author,
        "topic_query": page.topic_query,
        "skips": list(page.skip_log),
    }
    if page.visibility is not None:
        skip_payload["visibility"] = page.visibility
    skip_log_path.write_text(
        json.dumps(skip_payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return page_path, skip_log_path


def _render_page_body(page: SynthesisPage) -> str:
    lines = [page.text.rstrip(), "", "## Claims", ""]
    for claim in page.claims:
        citations = ", ".join(
            citation.chunk_id or citation.doc_id for citation in claim.citations
        )
        lines.append(f"- {claim.text} [{citations}]")
    return "\n".join(lines).rstrip() + "\n"
