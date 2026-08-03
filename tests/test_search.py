"""The hybrid pipeline keeps metadata filtering first and degrades on reranker failure."""

from pathlib import Path

from alexandria.index.bm25 import BM25Index
from alexandria.index.embedder import HashEmbedder
from alexandria.index.store import VectorStore
from alexandria.retrieval.rerank import IdentityReranker
from alexandria.retrieval.search import SearchConfig, SearchEngine


def record(chunk_id: str, doc_id: str, text: str, vector: list[float], **meta) -> dict:
    return {
        "chunk_id": chunk_id, "doc_id": doc_id, "text": text, "heading_path": "Heading",
        "vector": vector, "type": meta.pop("type", "observation"),
        "project": meta.pop("project", None), "status": meta.pop("status", "active"),
        "source": meta.pop("source", "test"), "tags": meta.pop("tags", []),
        "entities": meta.pop("entities", []), "layer": meta.pop("layer", None),
        "generated_at": meta.pop("generated_at", None),
    }


def build_engine(tmp_path: Path, reranker=None) -> SearchEngine:
    embedder = HashEmbedder(dim=24)
    vectors = embedder.embed(["sweep page fails lint", "sweep page retry lint", "unrelated notes"])
    rows = [
        record("sources/a", "sources/a", "sweep page fails lint", vectors[0], project="core"),
        record("wiki/a", "wiki/a", "sweep page retry lint", vectors[1], project="core"),
        record("sources/b", "sources/b", "unrelated notes", vectors[2], project="other"),
    ]
    store = VectorStore(tmp_path / "index")
    store.upsert(rows)
    lexical = BM25Index(tmp_path / "fts.sqlite")
    lexical.index(rows)
    return SearchEngine(embedder, store, lexical, reranker or IdentityReranker(),
                        SearchConfig(prefetch=5, top_k=2, wiki_boost=1.25))


def test_search_runs_hybrid_pipeline_and_records_trace(tmp_path: Path):
    results = build_engine(tmp_path).search("sweep page lint", filters={"project": "core"})

    assert {result.chunk_id for result in results} == {"sources/a", "wiki/a"}
    assert results[0].rank == 1
    assert results[0].trace["metadata_filter"] == {"project": "core"}
    assert results[0].trace["stages"]["bm25"]["out"] == 2
    assert results[0].trace["stages"]["dense"]["out"] == 2


class BrokenReranker:
    def rerank(self, query, candidates, k):
        raise RuntimeError("model unavailable")


def test_search_degrades_to_fusion_order_when_reranking_fails(tmp_path: Path):
    results = build_engine(tmp_path, BrokenReranker()).search("sweep page lint")

    assert results
    assert results[0].trace["reranker"]["degraded"] is True
    assert "model unavailable" in results[0].trace["reranker"]["error"]


class BrokenLexicalIndex:
    def search(self, query, k, where):
        raise RuntimeError("fts unavailable")


def test_search_keeps_dense_candidates_when_the_lexical_stage_fails(tmp_path: Path):
    engine = build_engine(tmp_path)
    engine.bm25 = BrokenLexicalIndex()

    results = engine.search("sweep page lint", k=5)

    assert results
    assert "fts unavailable" in results[0].trace["stages"]["bm25"]["error"]
    assert results[0].trace["shortfall"]["requested"] == 5


class LookupFailureStore:
    def __init__(self, store):
        self.store = store

    def search_vector(self, *args, **kwargs):
        return self.store.search_vector(*args, **kwargs)

    def get(self, chunk_id):
        raise RuntimeError(f"missing record for {chunk_id}")


def test_search_returns_an_empty_trace_instead_of_crashing_when_records_cannot_be_loaded(tmp_path: Path):
    engine = build_engine(tmp_path)
    engine.store = LookupFailureStore(engine.store)

    assert engine.search("sweep page lint") == []
    assert engine.last_trace["stages"]["fusion"]["lookup_errors"]


def test_reranker_sees_the_heading_not_just_the_body(tmp_path: Path):
    """A document whose TITLE matched the query survived fusion and was then dropped
    by the reranker, because the reranker judged relevance on text stripped of that
    title. All three stages (bm25, embedder, reranker) must see the same text."""
    seen = {}

    class CapturingReranker:
        def rerank(self, query, candidates, k):
            seen["texts"] = [c.text for c in candidates]
            return list(candidates[:k])

    build_engine(tmp_path, reranker=CapturingReranker()).search("sweep page lint")

    assert seen.get("texts"), "reranker received no candidates"
    assert any("Heading" in text for text in seen["texts"]), seen["texts"]
