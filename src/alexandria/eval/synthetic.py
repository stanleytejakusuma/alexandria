"""Build a throwaway index over the in-repo synthetic corpus, offline.

WHY THIS EXISTS (BACKLOG #20). The certification gate runs against a golden set
that lives in a private corpus repo, so a third party who clones this engine
cannot run it at all -- `scripts/eval-gate.py` detects the missing corpus and
SKIPS. A gate that skips on every machine but one is not a gate anyone else can
trust, and CI has never once executed it.

WHAT THIS MEASURES, AND WHAT IT DOES NOT. This builds a real index over
documents committed to this repo and drives the real SearchEngine, so it
exercises chunking, the vector store, BM25, RRF fusion, layer boost, the
manifest check, and every scoring path in `metrics.py` / `negative.py` --
including the Wilson interval and the McNemar significance bar. It verifies THE
HARNESS.

It does NOT measure retrieval quality on real knowledge, and a green run here is
not evidence that retrieval is good:

- The embedder is `HashEmbedder`. It is deterministic and needs no model
  download, which is exactly what makes this reproducible -- and it carries no
  semantics whatsoever. The dense leg of the hybrid is therefore a noise channel
  by construction. Recall here is earned by BM25 and by fusion surviving that
  noise.
- Because there is no semantic channel, the synthetic golden set carries no
  `zero` overlap band. A zero-overlap entry would measure nothing but hash luck,
  and reporting its recall would be reporting noise as a finding. The real
  golden set keeps the zero band; this one deliberately cannot.
- The reranker is `IdentityReranker`. The production cross-encoder needs a model
  download, so the rerank stage here is plumbing only.

Two gates, two purposes. The private-corpus gate answers "did retrieval quality
move?". This one answers "does the measuring instrument still work?". Neither
substitutes for the other, and a green synthetic gate must never be reported as
retrieval being healthy.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from ..config import AppConfig
from ..index.bm25 import BM25Index
from ..index.chunker import chunk_doc_records, is_indexable_source
from ..index.embedder import HashEmbedder
from ..index.manifest import verify_manifest, write_manifest
from ..index.store import VectorStore
from ..retrieval.rerank import IdentityReranker
from ..retrieval.search import SearchConfig, SearchEngine

__all__ = [
    "FIXTURES", "GOLDEN_PATH", "NEGATIVE_PATH", "SYNTHETIC_CORPUS",
    "build_synthetic_engine", "copy_synthetic_corpus",
]

FIXTURES = Path(__file__).resolve().parents[3] / "tests" / "fixtures"
SYNTHETIC_CORPUS = FIXTURES / "synthetic-corpus"
GOLDEN_PATH = FIXTURES / "synthetic-golden-v1.jsonl"
NEGATIVE_PATH = FIXTURES / "synthetic-negative-v1.jsonl"

EMBED_PROVIDER = "hash"


def copy_synthetic_corpus(destination: str | Path) -> Path:
    """Copy the fixture documents into `destination` and return that path.

    Copied, never indexed in place: indexing writes `.alexandria/` beside the
    documents, and a gate that leaves build artefacts in `tests/fixtures/` would
    dirty the working tree on every run and eventually get committed.
    """
    corpus = Path(destination)
    for source in sorted(SYNTHETIC_CORPUS.rglob("*.md")):
        relative = source.relative_to(SYNTHETIC_CORPUS)
        target = corpus / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    return corpus


def build_synthetic_engine(destination: str | Path) -> SearchEngine:
    """Index the synthetic corpus under `destination` and return a live engine.

    No query logger and no query cache: a cache would let a second identical
    query be answered from the first one's result, which turns a repeated
    measurement into a replay of itself.
    """
    corpus = copy_synthetic_corpus(destination)
    config = replace(AppConfig(corpus_path=corpus), embed_provider=EMBED_PROVIDER)

    records: list[dict] = []
    errors: list[str] = []
    for path in sorted(corpus.rglob("*.md")):
        if not is_indexable_source(path.relative_to(corpus)):
            continue
        chunk_records, error = chunk_doc_records(path, corpus, config)
        records.extend(chunk_records)
        if error:
            errors.append(error)
    if errors:
        raise RuntimeError(f"synthetic corpus failed to chunk: {errors}")
    if not records:
        raise RuntimeError(f"synthetic corpus produced no chunks under {corpus}")

    embedder = HashEmbedder()
    store = VectorStore(corpus / ".alexandria" / "index")
    lexical = BM25Index(corpus / ".alexandria" / "index" / "fts.sqlite")
    vectors = embedder.embed([record["text"] for record in records])
    for record, vector in zip(records, vectors, strict=True):
        record["vector"] = vector
    store.upsert(records)
    lexical.index(records)

    # The manifest is part of what this gate covers: `_build_search_engine`
    # refuses to serve an index whose manifest does not match the configured
    # provider, and that refusal is a retrieval-correctness guard, not a
    # formality. Writing then verifying exercises both halves.
    write_manifest(corpus, embedder, EMBED_PROVIDER)
    verify_manifest(corpus, embedder, EMBED_PROVIDER)

    return SearchEngine(
        embedder,
        store,
        lexical,
        IdentityReranker(),
        SearchConfig(prefetch=config.rerank_prefetch, top_k=config.rerank_top_k,
                     wiki_boost=config.wiki_boost, rrf_k=config.rrf_k),
        corpus_root=corpus,
    )
