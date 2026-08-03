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
        self._lock = threading.Lock()

    def upsert(self, records):
        start = time.monotonic()
        time.sleep(self.delay)
        with self._lock:
            self.calls.append((start, time.monotonic(), len(records)))


class SlowLexical:
    def __init__(self):
        self.indexed = []
        self._lock = threading.Lock()

    def index(self, records):
        with self._lock:
            self.indexed.extend(r["chunk_id"] for r in records)


def _records(n):
    return [{"chunk_id": f"c{i}", "doc_id": "d", "text": f"t{i}"} for i in range(n)]


def test_pipeline_overlaps_embed_and_write():
    """5 batches sequential would take ~5*(embed+write). Overlapped should be close
    to (5+1)*max(embed, write). Assert the overlapped number, not just 'is fast'."""
    embedder, store, lexical = SlowEmbedder(0.05), SlowStore(0.05), SlowLexical()
    started = time.monotonic()
    _run_index_pipeline(_records(20), embedder, store, lexical, batch_size=4,
                        progress_every=1_000_000, progress_stream=None)
    elapsed = time.monotonic() - started

    sequential_estimate = 5 * (0.05 + 0.05)
    assert elapsed < sequential_estimate * 0.75, (
        f"no measurable overlap: {elapsed:.3f}s vs sequential {sequential_estimate:.3f}s")


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
