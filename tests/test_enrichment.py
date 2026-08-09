"""Enrichment tests: store replay semantics, failures, synthetic routing."""

import json

from tests.test_search import record

from alexandria.enrich import (
    ENRICH_SYSTEM,
    EnrichmentStore,
    doc_fingerprint,
    enrich_doc,
    enrich_docs_for_index,
    recipe_signature,
    synthetic_records,
)

class ScriptedClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, system, user, temperature=0.0):
        self.calls.append((system, user, temperature))
        return self.responses.pop(0)


class ScriptedEmbedder:
    dim = 4

    def __init__(self, dim=4):
        self.dim = dim

    def embed(self, texts):
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    def embed_queries(self, texts):
        return [[0.0, 0.0, 0.0, 1.0] for _ in texts]


def _records(*texts, doc_id="sources/doc"):
    return [{"chunk_id": f"{doc_id}#{i}", "doc_id": doc_id,
             "text": t, "heading_path": "H", "layer": "notes"}
            for i, t in enumerate(texts)]


GOOD = json.dumps({
    "summary": "A short summary of the document.",
    "keywords": ["alpha", "beta"],
    "hypotheticals": ["How does the alpha work?", "What is beta?"],
})


def test_enrich_doc_parses_and_fails_clean(tmp_path):
    llm = ScriptedClient([GOOD])
    payload = enrich_doc(llm, "doc", "text")
    assert payload["summary"].startswith("A short summary")
    assert payload["keywords"] == ["alpha", "beta"]
    assert len(payload["hypotheticals"]) == 2
    assert llm.calls[0][1] == "DOCUMENT id: doc\n\ntext"
    assert llm.calls[0][2] == 0.1  # fast-tier guard: never temperature 0

    llm = ScriptedClient(["not json at all"])
    assert "error" in enrich_doc(llm, "doc", "text")

    llm = ScriptedClient(["```json\n{\"summary\": \"ok\"}\n```"])
    payload = enrich_doc(llm, "doc", "text")
    assert payload["summary"] == "ok"  # fenced JSON tolerated


def test_enrichment_store_replay_semantics(tmp_path):
    store = EnrichmentStore(tmp_path)
    sha = doc_fingerprint(_records("a", "b"))
    recipe = recipe_signature("model-x", "v1")
    assert store.get("doc", sha, recipe) is None  # nothing stored yet
    store.put("doc", sha, recipe, {"summary": "s"})
    assert store.get("doc", sha, recipe) == {"summary": "s"}
    # content change -> fingerprint mismatch -> re-enrich needed
    assert store.get("doc", doc_fingerprint(_records("a", "c")), recipe) is None
    # recipe change (model or prompt version) -> re-enrich needed
    assert store.get("doc", sha, recipe_signature("model-x", "v2")) is None
    assert store.get("doc", sha, recipe_signature("model-y", "v1")) is None
    assert store.count() == 1


def test_enrich_docs_for_index_reattach_and_limit(tmp_path):
    store = EnrichmentStore(tmp_path)
    records = _records("alpha body", "beta body")
    llm = ScriptedClient([GOOD])
    stats = enrich_docs_for_index(records, llm=llm, embedder=ScriptedEmbedder(),
                                  store=store, recipe=recipe_signature("m", "v1"))
    assert stats["enriched"] == 1 and stats["reattached"] == 0
    assert stats["synthetic"] == 2
    assert len(records) == 2 + 2  # two synthetic records appended
    syn = [r for r in records if r.get("kind") == "synthetic"]
    assert len(syn) == 2
    assert syn[0]["target_chunk"] == "sources/doc#0"
    assert syn[0]["parent_doc"] == "sources/doc"
    assert syn[0]["layer"] == "synthetic"
    assert syn[0]["text"] == "How does the alpha work?"
    assert syn[0]["vector"] == [0.0, 0.0, 0.0, 1.0]  # query-space embedding
    assert all(r["enrichment"] is not None for r in records[:2])
    # real chunks keep their own vectors
    assert "vector" not in records[0]

    # replay: same docs, no LLM call, payloads reattached from the store
    records2 = _records("alpha body", "beta body")
    llm2 = ScriptedClient([])  # any call would IndexError
    stats2 = enrich_docs_for_index(records2, llm=llm2, embedder=ScriptedEmbedder(),
                                   store=store, recipe=recipe_signature("m", "v1"))
    assert stats2["enriched"] == 0 and stats2["reattached"] == 1
    assert stats2["synthetic"] == 2
    assert records2[0]["enrichment"] is not None


def test_enrich_limit_counts_pending_docs(tmp_path):
    store = EnrichmentStore(tmp_path)
    records = (_records("one", doc_id="sources/d1")
               + _records("two", doc_id="sources/d2")
               + _records("three", doc_id="sources/d3"))
    llm = ScriptedClient([GOOD, GOOD])
    stats = enrich_docs_for_index(records, llm=llm, embedder=ScriptedEmbedder(),
                                  store=store, recipe=recipe_signature("m", "v1"),
                                  limit=2)
    assert stats["enriched"] == 2
    # the third doc was never called (limit applies to docs needing work)
    assert len(llm.calls) == 2
    # replay run with limit=0: third doc now enriched, two reattached
    records3 = (_records("one", doc_id="sources/d1")
                + _records("two", doc_id="sources/d2")
                + _records("three", doc_id="sources/d3"))
    llm3 = ScriptedClient([GOOD])
    stats3 = enrich_docs_for_index(records3, llm=llm3, embedder=ScriptedEmbedder(),
                                   store=store, recipe=recipe_signature("m", "v1"))
    assert stats3["enriched"] == 1 and stats3["reattached"] == 2


def test_failed_enrichment_not_stored_retryable(tmp_path):
    store = EnrichmentStore(tmp_path)
    records = _records("body")
    llm = ScriptedClient(["garbage"])  # unparseable -> error, never stored
    stats = enrich_docs_for_index(records, llm=llm, embedder=ScriptedEmbedder(),
                                  store=store, recipe=recipe_signature("m", "v1"))
    assert stats["failed"] == 1 and stats["enriched"] == 0
    assert store.count() == 0
    # a retry re-calls the LLM (failure is not marked complete)
    llm2 = ScriptedClient([GOOD])
    stats2 = enrich_docs_for_index(records, llm=llm2, embedder=ScriptedEmbedder(),
                                   store=store, recipe=recipe_signature("m", "v1"))
    assert stats2["enriched"] == 1
    assert store.count() == 1


def test_synthetic_records_explicit_metadata():
    recs = _records("body", "body2")
    vectors = [[0.0, 0.0, 0.0, 1.0], [0.0, 0.0, 0.0, 1.0]]
    out = synthetic_records(recs, {"hypotheticals": ["Q1?", "Q2?"]},
                            "sources/doc#0", vectors)
    assert [r["kind"] for r in out] == ["synthetic", "synthetic"]
    assert out[0]["chunk_id"] == "sources/doc#0::hq1"
    assert out[0]["target_chunk"] == "sources/doc#0"
    assert out[0]["parent_doc"] == "sources/doc"
    assert out[0]["enrichment"] is None
    assert out[1]["chunk_id"] == "sources/doc#0::hq2"


def test_search_routes_synthetic_hit_to_real_chunk(tmp_path):
    """Red: a query that matches a document's HYPOTHETICAL question must
    retrieve the real chunk -- and no synthetic id may ever be surfaced."""
    from alexandria.index.bm25 import BM25Index
    from alexandria.index.embedder import HashEmbedder
    from alexandria.index.store import VectorStore
    from alexandria.retrieval.rerank import IdentityReranker
    from alexandria.retrieval.search import SearchConfig, SearchEngine

    embedder = HashEmbedder(dim=24)
    real = record("sources/doc#0", "sources/doc",
                  "deploy pipeline runs the lint gate",
                  embedder.embed(["deploy pipeline runs the lint gate"])[0])
    syn = record("sources/doc#0::hq1", "sources/doc",
                 "what happens when the retry budget runs out?",
                 embedder.embed(["what happens when the retry budget runs out?"])[0],
                 layer="synthetic", kind="synthetic", parent_doc="sources/doc",
                 target_chunk="sources/doc#0")
    rows = [
        real,
        syn,
        record("sources/other", "sources/other", "unrelated note about cooking",
               embedder.embed(["unrelated note about cooking"])[0]),
    ]
    store = VectorStore(tmp_path / "index")
    store.upsert(rows)
    lexical = BM25Index(tmp_path / "fts.sqlite")
    lexical.index(rows)
    engine = SearchEngine(embedder, store, lexical, IdentityReranker(),
                          SearchConfig(prefetch=5, top_k=2, wiki_boost=1.25))
    results = engine.search("what happens when the retry budget runs out")
    # the hypothetical hit routed to the real chunk; no ::hq anywhere
    assert all("::hq" not in r.chunk_id for r in results)
    assert results[0].chunk_id == "sources/doc#0"
    assert engine.last_trace["stages"]["fusion"]["enriched_hits"] == 1
