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
        "enrichment": meta.pop("enrichment", None),
        "kind": meta.pop("kind", None), "parent_doc": meta.pop("parent_doc", None),
        "target_chunk": meta.pop("target_chunk", None),
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


def test_retrieval_depth_is_decoupled_from_rerank_width(tmp_path: Path):
    """prefetch conflated two different knobs: how DEEP each retriever's candidate
    list goes (arithmetic, ~free) and how many candidates the cross-encoder scores
    (~100ms each). A target measured at dense rank 42 contributes ZERO to fusion at
    depth 8 -- RRF can only surface mid-ranked candidates from lists deep enough to
    contain them."""
    captured = {}

    class CapturingBM25:
        def search(self, query, k, where=None):
            captured["bm25_k"] = k
            return []

    class CapturingStore:
        def search_vector(self, vec, k, where=None):
            captured["dense_k"] = k
            return []
        def get_many(self, ids):
            return {}

    class CountingReranker:
        def rerank(self, query, candidates, k):
            captured["rerank_in"] = len(candidates)
            return list(candidates[:k])

    class OneVec:
        name, dim = "fake", 2
        def embed(self, texts): return [[1.0, 0.0] for _ in texts]

    engine = SearchEngine(OneVec(), CapturingStore(), CapturingBM25(), CountingReranker(),
                          SearchConfig(depth=50, prefetch=8, top_k=5))
    engine.search("anything")
    assert captured["bm25_k"] == 50
    assert captured["dense_k"] == 50


def test_depth_defaults_to_at_least_prefetch(tmp_path: Path):
    cfg = SearchConfig(prefetch=8)
    assert cfg.depth >= cfg.prefetch


def test_depth_default_is_the_measured_safe_value():
    """depth=100 was tried and reverted: sound in isolation, but combined with the
    query instruct-prefix it crowded the rerank pool with distractors and dropped
    recall@5 78.6%->64.3% on golden-v1 (MRR 0.714->0.607). depth=8 matched or beat
    every tested combination."""
    assert SearchConfig().depth == 8


def test_search_uses_the_query_prefixed_embedding_path(tmp_path: Path):
    """Queries must go through embed_queries (instruct-prefixed), never the raw
    document path -- the model was trained on asymmetric query/document encoding."""
    calls = {}

    class RecordingEmbedder:
        name, dim = "fake", 2
        def embed(self, texts):
            calls["embed"] = list(texts); return [[1.0, 0.0] for _ in texts]
        def embed_queries(self, texts):
            calls["embed_queries"] = list(texts); return [[1.0, 0.0] for _ in texts]

    class EmptyStore:
        def search_vector(self, v, k, where=None): return []
        def get_many(self, ids): return {}

    class EmptyBM25:
        def search(self, q, k, where=None): return []

    from alexandria.retrieval.rerank import IdentityReranker
    SearchEngine(RecordingEmbedder(), EmptyStore(), EmptyBM25(),
                 IdentityReranker()).search("my question")
    assert calls.get("embed_queries") == ["my question"]
    assert "embed" not in calls


def test_normalise_record_never_stores_none_in_new_columns():
    """LanceDB repro: a table created with NULL new-columns crashes later
    merge_insert (Spill error); the store must coerce to empty strings."""
    from alexandria.index.store import _normalise_record

    record = _normalise_record({
        "chunk_id": "c", "doc_id": "d", "text": "t",
        "heading_path": "h", "vector": [1.0],
        "tags": [], "entities": [],
    })
    for field in ("enrichment", "kind", "parent_doc", "target_chunk"):
        assert record[field] == ""
    # enrichment JSON and synthetic routing values pass through
    enriched = _normalise_record(dict(record, enrichment='{"s":1}',
                                      kind="synthetic",
                                      parent_doc="d", target_chunk="c"))
    assert enriched["kind"] == "synthetic"
    assert enriched["enrichment"] == '{"s":1}'
    assert enriched["target_chunk"] == "c"


def test_reindex_invalidates_cache_for_a_long_lived_engine(tmp_path: Path):
    """A warm `alexandria serve` process must notice a reindex.

    The generation counter keys every query-cache entry. When it was captured
    in ``__init__`` the CLI was unaffected -- each invocation built a fresh
    engine -- but a long-lived server would keep serving pre-reindex cached
    results for the life of the process, with no error and no cache miss.
    Mutation check: pin ``_generation`` back to a construction-time attribute
    and this test fails on the final assertion.
    """
    from alexandria.cache import QueryCache, write_index_generation

    engine = build_engine(tmp_path)
    engine.query_cache = QueryCache(tmp_path)
    engine._corpus_root = tmp_path

    gen_before = engine._generation
    engine.search("sweep page fails lint")
    assert engine.last_cache_hit == 0, "first query must be a miss"

    engine.search("sweep page fails lint")
    assert engine.last_cache_hit == 1, "premise: identical query must hit the cache"

    # A reindex happened underneath the running process.
    write_index_generation(tmp_path)
    assert engine._generation > gen_before, "engine must observe the new generation"

    engine.search("sweep page fails lint")
    assert engine.last_cache_hit == 0, (
        "after a reindex the warm engine served a stale cached result"
    )


class _RecordingLogger:
    def __init__(self):
        self.calls = []

    def log(self, **kwargs):
        self.calls.append(kwargs)
        return True


def test_client_attribution_distinguishes_search_from_answer_retrieval(tmp_path: Path):
    """SPEC F3: cache_hit=1 rows must be splittable by which caller retrieved --
    a genuine sub-10ms search-cache hit vs. a retrieval leg inside /answer.
    Previously every caller shared the SearchEngine default client="cli", a dead
    discriminator (queries.sqlite showed 100% client='cli' with 2,377 rows).
    Mutation check: drop the client= kwarg passed to SearchEngine's constructor
    (or the logger.log call) and this test fails on the second assertion.
    """
    from alexandria.cache import QueryCache

    embedder = HashEmbedder(dim=24)
    vectors = embedder.embed(
        ["sweep page fails lint", "sweep page retry lint", "unrelated notes"])
    rows = [
        record("sources/a", "sources/a", "sweep page fails lint", vectors[0], project="core"),
        record("wiki/a", "wiki/a", "sweep page retry lint", vectors[1], project="core"),
        record("sources/b", "sources/b", "unrelated notes", vectors[2], project="other"),
    ]
    store = VectorStore(tmp_path / "index")
    store.upsert(rows)
    lexical = BM25Index(tmp_path / "fts.sqlite")
    lexical.index(rows)
    recording_logger = _RecordingLogger()
    engine = SearchEngine(embedder, store, lexical, IdentityReranker(),
                          SearchConfig(prefetch=5, top_k=2, wiki_boost=1.25),
                          logger=recording_logger,
                          query_cache=QueryCache(tmp_path), client="answer",
                          corpus_root=tmp_path)

    engine.search("sweep page fails lint")

    assert recording_logger.calls, "logger.log must be invoked"
    assert all(call["client"] == "answer" for call in recording_logger.calls), (
        "a SearchEngine built with client='answer' must attribute every logged "
        "query to 'answer', not the old dead-default 'cli'"
    )
