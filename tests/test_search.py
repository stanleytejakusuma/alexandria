"""The hybrid pipeline keeps metadata filtering first and degrades on reranker failure."""

import sys
from pathlib import Path

import pytest

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
                        SearchConfig(prefetch=5, top_k=2, wiki_boost=1.25),
                        corpus_root=tmp_path)


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


def test_a_degraded_reranker_prints_a_loud_warning_unconditionally(tmp_path: Path, capsys):
    """#44: the trace already recorded degraded=True, but nothing was ever
    PRINTED, so a plain `alexandria search` (no --trace flag) gave a user zero
    indication their results skipped reranking. This is the one place every
    caller (CLI search, CLI answer, serve's /search, /answer) funnels through,
    so the warning belongs here, not duplicated in each entry point."""
    build_engine(tmp_path, BrokenReranker()).search("sweep page lint")

    warning = capsys.readouterr().err
    assert warning, "degradation must be printed unconditionally, not only under --trace"
    assert "rerank" in warning.lower()
    assert "model unavailable" in warning
    assert "degrad" in warning.lower() or "fell back" in warning.lower() or "fallback" in warning.lower()


def test_a_healthy_reranker_prints_nothing(tmp_path: Path, capsys):
    """The warning must be conditional on ACTUAL degradation -- a healthy path
    must stay silent, or the signal is worthless."""
    build_engine(tmp_path).search("sweep page lint")

    assert capsys.readouterr().err == ""


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


def test_a_corrupt_generation_file_disables_caching_but_does_not_crash_search(tmp_path: Path, capsys):
    """SPEC F2: a corrupt generation.json must degrade retrieval (no cache
    read/write for this call, results still returned), never crash search().
    Mutation check: let the GenerationFileCorrupt propagate uncaught out of
    search() and this test fails with an unhandled exception instead of
    reaching the assertions.
    """
    from alexandria.cache import QueryCache

    engine = build_engine(tmp_path)
    engine.query_cache = QueryCache(tmp_path)
    engine._corpus_root = tmp_path
    gen_path = tmp_path / ".alexandria" / "index" / "generation.json"
    gen_path.parent.mkdir(parents=True, exist_ok=True)
    gen_path.write_text("{not valid json")

    results = engine.search("sweep page fails lint")

    assert {r.chunk_id for r in results} == {"sources/a", "wiki/a"}, (
        "retrieval must still work despite the corrupt generation file"
    )
    assert "corrupt" in capsys.readouterr().err.lower()

    # A second identical call must also skip the cache (not silently start
    # hitting a cache keyed by some fallback generation value).
    results2 = engine.search("sweep page fails lint")
    assert engine.last_cache_hit == 0


@pytest.mark.skipif(sys.platform == "win32", reason="flock contract is POSIX-only")
def test_search_refuses_a_live_writer_or_interrupted_rebuild_before_touching_retrieval(tmp_path):
    """The in-place rebuild marker/lock is a reader fence, not merely eval metadata."""
    from alexandria.writelock import IndexReadUnavailable, rebuild_marker, write_lock

    engine = build_engine(tmp_path)
    writer = write_lock(tmp_path)
    assert writer.acquire()
    try:
        with pytest.raises(IndexReadUnavailable, match="writer"):
            engine.search("sweep page")
    finally:
        writer.release()

    marker = rebuild_marker(tmp_path)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("interrupted rebuild\n")
    with pytest.raises(IndexReadUnavailable, match="rebuild"):
        engine.search("sweep page")
    marker.unlink()

    assert engine.search("sweep page"), "reader resumes only after a successful rebuild clears marker"


@pytest.mark.skipif(sys.platform == "win32", reason="flock contract is POSIX-only")
def test_both_retrieval_legs_execute_while_the_shared_epoch_is_actually_held(tmp_path):
    """Probe the span, do not narrate it.

    Red round 2, condition 1: asserting only that both legs *ran* proves
    nothing about whether they ran *under* the shared epoch -- a refactor
    moving either leg (or hydration) outside the `with` block would keep the
    suite green while un-fencing the exact interleaving this design exists to
    prevent. So each leg actively probes the corpus write lock and must find it
    unavailable, which is the mirror of the rerank probe that must find it free.
    """
    from alexandria.writelock import write_lock

    seen: dict[str, bool] = {}
    engine = build_engine(tmp_path)
    real_dense, real_lexical = engine.store.search_vector, engine.bm25.search

    def probe(name, fn):
        def wrapper(*args, **kwargs):
            # Explicitly try-once: if WriteLock's default ever became blocking,
            # an implicit call here would turn each probe into a 30s stall that
            # still passed, and this test would be blamed rather than fixed.
            writer = write_lock(tmp_path)
            admitted = writer.acquire(blocking=False)
            if admitted:
                writer.release()
            seen[name] = admitted
            return fn(*args, **kwargs)
        return wrapper

    # Hydration is a real index read too (store.get_many), so it is probed
    # alongside both legs -- Red round 3 correctly noted that instrumenting only
    # the legs would leave a refactor free to move hydration after the epoch and
    # resolve stale ids against a dropped-and-refilled table.
    real_hydrate = engine.store.get_many
    engine.store.search_vector = probe("dense", real_dense)
    engine.bm25.search = probe("lexical", real_lexical)
    engine.store.get_many = probe("hydrate", real_hydrate)

    assert engine.search("sweep page lint")
    assert seen == {"dense": False, "lexical": False, "hydrate": False}, (
        f"a retrieval/hydration step ran OUTSIDE the shared epoch "
        f"(the writer lock was obtainable during it): {seen}")


@pytest.mark.skipif(sys.platform == "win32", reason="flock contract is POSIX-only")
def test_warm_engine_still_sees_new_writes_after_an_external_epoch_completes(tmp_path):
    """Coherence must come from the epoch, never from pinning stale handles.

    Red round 1 proposed refusing a warm engine whose bound generation drifted.
    That premise is false here and the "fix" would be a serve-killing
    regression: ``VectorStore.search_vector`` re-opens its table per query and
    BM25 reads a live connection, which is exactly why the long-lived server
    sees externally indexed content (test_serve.py S4, regression 500cd9e).
    """
    from alexandria.cache import write_index_generation

    engine = build_engine(tmp_path)
    assert engine.search("sweep page lint")
    write_index_generation(tmp_path)
    assert engine.search("sweep page lint"), (
        "a warm engine must keep serving after an external index run completes -- "
        "refusing on generation drift would break the S4 warm-server contract")


def test_warm_engine_query_cache_cannot_replay_a_superseded_generation(tmp_path):
    """Red round 2, condition 2: the surviving epoch-fusion channel.

    If the generation used to key the query cache were captured at construction
    rather than re-read per call, a warm engine would take a clean shared epoch
    after a completed rebuild, see no marker, rebuild the SAME pre-rebuild cache
    key, and replay stale results against the new corpus for the cache TTL --
    fully "inside a coherent epoch" and invisible to every other test here.
    """
    from alexandria.cache import QueryCache, write_index_generation

    engine = build_engine(tmp_path)
    engine.query_cache = QueryCache(tmp_path)
    first = engine.search("sweep page lint")
    assert first
    assert engine.search("sweep page lint")[0].trace.get("cache_hit") is True

    write_index_generation(tmp_path)  # an external rebuild completed

    replayed = engine.search("sweep page lint")
    assert replayed[0].trace.get("cache_hit") is not True, (
        "the warm engine replayed a cache entry keyed to a superseded generation")

    # ...and the cache re-seeds under the new generation rather than being
    # permanently bypassed after any rebuild.
    assert engine.search("sweep page lint")[0].trace.get("cache_hit") is True


@pytest.mark.skipif(sys.platform == "win32", reason="flock contract is POSIX-only")
def test_reranking_happens_outside_the_shared_epoch_so_readers_cannot_starve_writers(tmp_path):
    """The epoch must cover index reads only -- not the cross-encoder.

    flock has no writer priority: while any reader holds SH, a queued EX waiter
    keeps losing to newly arriving readers. Reranking is a ~100ms-per-candidate
    model call over already-hydrated records that touches no index state, so
    holding SH across it inflates the starvation window by the slowest stage in
    the pipeline and can push `index` past DEFAULT_LOCK_TIMEOUT on ordinary
    query traffic. Retrieval must be inside the epoch; reranking must not be.
    """
    from alexandria.writelock import write_lock

    observed = {}

    class LockProbingReranker:
        def rerank(self, query, candidates, k):
            # A writer must be able to get in the moment retrieval is done.
            probe = write_lock(tmp_path)
            observed["writer_admitted_during_rerank"] = probe.acquire(blocking=False)
            if observed["writer_admitted_during_rerank"]:
                probe.release()
            return list(candidates[:k])

    engine = build_engine(tmp_path, reranker=LockProbingReranker())
    assert engine.search("sweep page lint")
    assert observed["writer_admitted_during_rerank"] is True, (
        "the shared epoch was still held during reranking -- query traffic can "
        "starve `index` for the full cross-encoder latency")
