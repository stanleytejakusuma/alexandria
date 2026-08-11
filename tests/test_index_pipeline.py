"""Indexing pipeline: overlap embed (GPU-bound) with store writes (I/O-bound).

The synchronous version blocks the embedder idle during every LanceDB/FTS5 write.
Overlapping them should make wall time track max(embed, write) per batch instead of
their sum -- classic double-buffering. Proven with sleep-based fakes and a timing
tolerance, not by trusting a real model's variance.
"""

import threading
import time

from alexandria.cli import _run_index_pipeline


class SlowEmbedder:
    """Records (start, end) of every embed() call and its cache stats snapshot."""

    def __init__(self, delay=0.05):
        self.delay = delay
        self.calls = []
        self.last_cache_stats = {"hits": 0, "misses": 0}

    def embed(self, texts):
        start = time.monotonic()
        time.sleep(self.delay)
        self.last_cache_stats = {"hits": 0, "misses": len(texts)}
        self.calls.append((start, time.monotonic()))
        return [[0.0] * 4 for _ in texts]


class SlowStore:
    def __init__(self, delay=0.05):
        self.delay = delay
        self.calls = []
        self.appended = []
        self._lock = threading.Lock()

    def upsert(self, records):
        start = time.monotonic()
        time.sleep(self.delay)
        with self._lock:
            self.calls.append((start, time.monotonic(), len(records)))

    def append(self, records):
        with self._lock:
            self.appended.append(len(records))
        self.upsert(records)


class SlowLexical:
    def __init__(self):
        self.indexed = []
        self._lock = threading.Lock()

    def index(self, records, *, append_only=False):
        with self._lock:
            self.indexed.extend(r["chunk_id"] for r in records)


def _records(n):
    return [{"chunk_id": f"c{i}", "doc_id": "d", "text": f"t{i}"} for i in range(n)]


def test_pipeline_overlaps_embed_and_write():
    """The pipeline must run embed (GPU-bound) concurrently with store writes
    (I/O-bound): some embed interval must overlap some write interval. Asserted
    via recorded intervals, not wall time -- the old budget (elapsed < 0.75x
    sequential) failed on GitHub's shared macOS runner where sleeps oversleep
    (parallel 0.659s vs sequential 0.500s) despite correct concurrency."""
    embedder, store, lexical = SlowEmbedder(0.05), SlowStore(0.05), SlowLexical()
    _run_index_pipeline(_records(20), embedder, store, lexical, batch_size=4,
                        progress_every=1_000_000, progress_stream=None)

    overlap = any(
        e_start < w_end and w_start < e_end
        for e_start, e_end in embedder.calls
        for w_start, w_end, _ in store.calls
    )
    assert overlap, (
        "no embed interval overlapped a store write: the pipeline ran sequentially"
    )


def test_pipeline_writes_every_record_exactly_once():
    embedder, store, lexical = SlowEmbedder(0.01), SlowStore(0.01), SlowLexical()
    stats = _run_index_pipeline(_records(37), embedder, store, lexical, batch_size=5,
                                progress_every=1_000_000, progress_stream=None)
    assert sum(c[2] for c in store.calls) == 37
    assert sorted(lexical.indexed) == sorted(f"c{i}" for i in range(37))
    assert stats.indexed == 37


def test_cache_stats_are_attributed_to_the_correct_batch_under_overlap():
    """last_cache_stats is a shared mutable attribute on the embedder. Reading it
    late (after the next embed() has already started) would attribute the wrong
    batch's hit/miss counts -- must be snapshotted at embed time, not write time."""
    embedder, store, lexical = SlowEmbedder(0.02), SlowStore(0.05), SlowLexical()
    stats = _run_index_pipeline(_records(12), embedder, store, lexical, batch_size=4,
                                progress_every=1_000_000, progress_stream=None)
    assert stats.cache_misses == 12          # every SlowEmbedder call reports miss=len(batch)
    assert stats.cache_hits == 0


def test_a_write_failure_is_not_silently_swallowed():
    class FailingStore(SlowStore):
        def upsert(self, records):
            raise RuntimeError("disk full")

    import pytest
    with pytest.raises(RuntimeError, match="disk full"):
        _run_index_pipeline(_records(8), SlowEmbedder(0.001), FailingStore(), SlowLexical(),
                            batch_size=4, progress_every=1_000_000, progress_stream=None)


def test_an_embed_failure_is_not_silently_swallowed():
    class FailingEmbedder(SlowEmbedder):
        def embed(self, texts):
            raise RuntimeError("model crashed")

    import pytest
    with pytest.raises(RuntimeError, match="model crashed"):
        _run_index_pipeline(_records(8), FailingEmbedder(), SlowStore(0.001), SlowLexical(),
                            batch_size=4, progress_every=1_000_000, progress_stream=None)


def test_empty_records_is_a_noop():
    embedder, store, lexical = SlowEmbedder(), SlowStore(), SlowLexical()
    stats = _run_index_pipeline([], embedder, store, lexical, batch_size=4,
                                progress_every=1_000_000, progress_stream=None)
    assert stats.indexed == 0
    assert embedder.calls == []


def test_batch_smaller_than_batch_size_still_processes():
    embedder, store, lexical = SlowEmbedder(0.001), SlowStore(0.001), SlowLexical()
    stats = _run_index_pipeline(_records(3), embedder, store, lexical, batch_size=10,
                                progress_every=1_000_000, progress_stream=None)
    assert stats.indexed == 3


def test_embedded_text_includes_the_heading_breadcrumb():
    """BM25 and the embedder must see the SAME text, or a chunk is findable by one
    retriever and invisible to the other."""
    from alexandria.index.bm25 import searchable_text

    chunk = {"chunk_id": "c1", "doc_id": "d",
             "heading_path": "Payments service > Retry behaviour",
             "text": "The guard retries three times."}
    combined = searchable_text(chunk)
    assert "Payments service" in combined
    assert "retries three times" in combined


def test_searchable_text_handles_a_missing_heading():
    from alexandria.index.bm25 import searchable_text
    assert searchable_text({"text": "body only"}) == "body only"


def test_write_batch_commits_far_less_often_than_it_embeds():
    """Each store write is one LanceDB commit, and a commit rewrites a manifest
    listing every prior fragment -- so committing per 32-row embed batch made a full
    rebuild O(n^2). Measured on the real corpus: 3,970 fragments, 561MB of manifest
    churn against 683MB of data, throughput halved 480 -> 256 chunks/min. Commit
    granularity must therefore be independent of embed granularity."""
    embedder, store, lexical = SlowEmbedder(0.001), SlowStore(0.001), SlowLexical()
    stats = _run_index_pipeline(_records(250), embedder, store, lexical, batch_size=10,
                                progress_every=1_000_000, progress_stream=None,
                                write_batch=100)

    assert len(embedder.calls) == 25                      # embed granularity unchanged
    assert [c[2] for c in store.calls] == [100, 100, 50]  # 3 commits, not 25
    assert stats.indexed == 250
    assert sorted(lexical.indexed) == sorted(f"c{i}" for i in range(250))


def test_the_final_partial_buffer_is_never_dropped():
    """Records short of a full write_batch still have to reach the store: the bug
    this guards against loses the tail of every corpus that is not an exact multiple
    of the batch size."""
    embedder, store, lexical = SlowEmbedder(0.001), SlowStore(0.001), SlowLexical()
    stats = _run_index_pipeline(_records(7), embedder, store, lexical, batch_size=2,
                                progress_every=1_000_000, progress_stream=None,
                                write_batch=1000)

    assert sum(c[2] for c in store.calls) == 7
    assert stats.indexed == 7


def test_default_write_batch_preserves_commit_per_embed_batch():
    """write_batch=0 is the compatibility default -- existing callers must keep
    committing exactly as before."""
    embedder, store, lexical = SlowEmbedder(0.001), SlowStore(0.001), SlowLexical()
    _run_index_pipeline(_records(20), embedder, store, lexical, batch_size=5,
                        progress_every=1_000_000, progress_stream=None)

    assert [c[2] for c in store.calls] == [5, 5, 5, 5]


def test_rebuild_routes_writes_to_append_not_merge():
    """After store.drop() every row is new, so merge_insert's match scan can only
    ever find nothing. The rebuild path must take append(), or it pays that scan on
    every batch for no result."""
    embedder, store, lexical = SlowEmbedder(0.001), SlowStore(0.001), SlowLexical()
    _run_index_pipeline(_records(30), embedder, store, lexical, batch_size=10,
                        progress_every=1_000_000, progress_stream=None,
                        write_batch=10, append_only=True)

    assert store.appended == [10, 10, 10]

    store2 = SlowStore(0.001)
    _run_index_pipeline(_records(30), SlowEmbedder(0.001), store2, SlowLexical(),
                        batch_size=10, progress_every=1_000_000, progress_stream=None,
                        write_batch=10)
    assert store2.appended == []      # incremental indexing still merges


def test_precomputed_batches_do_not_recount_the_previous_batch_cache_stats():
    """`last_cache_stats` is only refreshed BY an embed() call.

    Enrichment's synthetic records carry precomputed query-space vectors and skip
    the embedder entirely, so a batch made only of those never calls embed() --
    and the old code snapshotted the stale stats from the previous batch and
    added them again. Observed on the real corpus: a 124,751-chunk rebuild
    reported `cache 89902 hit/0 miss` when only 38,963 chunks were ever embedded,
    which is a number that cannot be reconciled with the corpus and sent me
    hunting a data-loss bug that did not exist.
    """
    records = _records(40)
    for i in range(10, 30):                       # middle two batches are precomputed
        records[i]["vector"] = [0.5, 0.5, 0.5, 0.5]

    embedder, store, lexical = SlowEmbedder(0.001), SlowStore(0.001), SlowLexical()
    stats = _run_index_pipeline(records, embedder, store, lexical, batch_size=10,
                                progress_every=1_000_000, progress_stream=None)

    assert len(embedder.calls) == 2, "only the two non-precomputed batches embed"
    assert stats.cache_misses == 20, (
        f"expected 20 embedded chunks, got {stats.cache_misses} -- stale stats "
        "from a previous batch were counted again")
    assert stats.indexed == 40, "every record must still be indexed"
