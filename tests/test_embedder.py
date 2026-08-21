"""Embedders are deterministic offline in tests and cache model work by content."""

import json
import sqlite3
import sys
import threading

import pytest
from pathlib import Path

from alexandria.model_load import clear_failure_cache


@pytest.fixture(autouse=True)
def _clear_model_load_cache():
    """The shared keyed failure cache in model_load.py must not leak one
    test's cached failure/success into the next (Red review, 2026-08-20)."""
    clear_failure_cache()
    yield
    clear_failure_cache()

from alexandria.index.embedder import CachedEmbedder, HashEmbedder, LocalEmbedder, QUERY_PREFIX


class CountingEmbedder:
    name = "counting-v1"
    dim = 3

    def __init__(self):
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [[float(len(text)), 0.0, 1.0] for text in texts]


def test_hash_embedder_is_deterministic_and_dependency_free():
    embedder = HashEmbedder(dim=16)
    assert embedder.embed(["alpha", "beta", "alpha"])[0] == embedder.embed(["alpha"])[0]
    assert len(embedder.embed(["alpha"])[0]) == 16
    assert embedder.embed(["alpha"])[0] != embedder.embed(["beta"])[0]


def test_cached_embedder_only_calls_provider_for_new_content(tmp_path: Path):
    provider = CountingEmbedder()
    embedder = CachedEmbedder(provider, tmp_path / "cache" / "embeddings.sqlite")

    first = embedder.embed(["same", "other", "same"])
    second = embedder.embed(["other", "same"])

    assert provider.calls == [["same", "other"]]
    assert first[0] == second[1]
    assert embedder.last_cache_stats == {"hits": 2, "misses": 0}


def test_embedding_cache_key_includes_model_name(tmp_path: Path):
    first_provider = CountingEmbedder()
    CachedEmbedder(first_provider, tmp_path / "embeddings.sqlite").embed(["same"])

    second_provider = CountingEmbedder()
    second_provider.name = "counting-v2"
    CachedEmbedder(second_provider, tmp_path / "embeddings.sqlite").embed(["same"])

    assert len(first_provider.calls) == 1
    assert len(second_provider.calls) == 1


def test_corrupt_cache_value_is_recomputed(tmp_path: Path):
    provider = CountingEmbedder()
    cache_path = tmp_path / "embeddings.sqlite"
    embedder = CachedEmbedder(provider, cache_path)
    embedder.embed(["same"])
    embedder._connection.execute("UPDATE embeddings SET vector = 'not json'")
    embedder._connection.commit()

    embedder.embed(["same"])

    assert provider.calls == [["same"], ["same"]]


class FakeSentenceTransformer:
    """Records exactly what LocalEmbedder asks it to do -- no real model needed."""

    def __init__(self):
        self.encode_calls: list[dict] = []

    def encode(self, texts, **kwargs):
        self.encode_calls.append({"texts": list(texts), **kwargs})
        return [[0.0, 1.0] for _ in texts]

    def get_sentence_embedding_dimension(self):
        return 2


def test_local_embedder_pads_to_a_fixed_length():
    """PyTorch's MPS backend compiles and permanently caches a new GPU execution
    graph per distinct input shape (pytorch/pytorch#154329, confirmed open). Variable
    per-batch padding means nearly every batch is a new shape; this was the measured
    cause of tonight's swap growth. Fixed-length padding keeps the shape constant so
    only one graph is ever compiled, no matter how batch content varies."""
    fake = FakeSentenceTransformer()
    embedder = LocalEmbedder(max_length=640)
    embedder._model = fake

    embedder.embed(["short", "a much longer piece of text than the other one"])

    assert len(fake.encode_calls) == 1
    kwargs = fake.encode_calls[0]["processing_kwargs"]["text"]
    assert kwargs["padding"] == "max_length"
    assert kwargs["max_length"] == 640
    assert kwargs["truncation"] is True


def test_local_embedder_max_length_covers_the_real_corpus_max():
    """Grounded in measurement, not a guess: 603 tokens is the largest chunk this
    corpus's chunker ever produced. The fixed length must exceed it or content
    silently truncates at embed time -- a worse bug than the one being fixed."""
    assert LocalEmbedder().max_length >= 603


def test_local_embedder_default_still_normalizes_and_batches():
    fake = FakeSentenceTransformer()
    embedder = LocalEmbedder(batch_size=7)
    embedder._model = fake

    embedder.embed(["x"])

    call = fake.encode_calls[0]
    assert call["batch_size"] == 7
    assert call["normalize_embeddings"] is True


def test_query_embedding_gets_the_instruct_prefix(tmp_path: Path):
    """Qwen3-Embedding is instruction-aware: it ships a query prompt in its own
    config_sentence_transformers.json but default_prompt_name is null, so nothing
    applies it unless asked. The model card quantifies omitting it at a 1-5%
    retrieval drop -- and paraphrase queries, our measured weak spot, are the
    trained-for case. Documents are embedded RAW (the card is explicit); only
    queries carry the prefix."""
    from alexandria.index.embedder import QUERY_PREFIX

    provider = CountingEmbedder()
    embedder = CachedEmbedder(provider, tmp_path / "cache.sqlite")
    embedder.embed_queries(["what is the capital gate"])

    sent = provider.calls[0][0]
    assert sent.startswith("Instruct: ")
    assert sent.endswith("Query:what is the capital gate")   # no space after Query:
    assert QUERY_PREFIX.endswith("Query:")


def test_document_embedding_stays_raw(tmp_path: Path):
    provider = CountingEmbedder()
    embedder = CachedEmbedder(provider, tmp_path / "cache.sqlite")
    embedder.embed(["a document body"])
    assert provider.calls[0] == ["a document body"]


def test_query_and_document_cache_entries_never_collide(tmp_path: Path):
    """Same text as query and as document must produce distinct cache keys, or a
    prefixed query vector could be served for a raw document embed."""
    provider = CountingEmbedder()
    embedder = CachedEmbedder(provider, tmp_path / "cache.sqlite")
    embedder.embed(["same text"])
    embedder.embed_queries(["same text"])
    assert len(provider.calls) == 2          # both computed, no false cache hit


def test_read_only_embedding_cache_miss_does_not_create_a_missing_cache(tmp_path: Path):
    """A read-only evaluation may calculate a query miss but must leave no cache.

    This is the ablation query path (``embed_queries``), not merely a document
    embedding unit test: the intentionally absent parent directory proves that
    opening the cache did not create its directory, database, or SQLite sidecars.
    """
    provider = CountingEmbedder()
    cache_path = tmp_path / "corpus" / ".alexandria" / "cache" / "embeddings.sqlite"

    embedder = CachedEmbedder(provider, cache_path, read_only=True)
    vectors = embedder.embed_queries(["uncached ablation query"])

    assert len(vectors) == 1
    assert vectors[0][1] == 0.0
    assert sum(value * value for value in vectors[0]) == pytest.approx(1.0)
    assert provider.calls == [[f"{QUERY_PREFIX}uncached ablation query"]]
    assert embedder.last_cache_stats == {"hits": 0, "misses": 1}
    assert not cache_path.exists()
    assert not cache_path.parent.exists()
    assert list(cache_path.parent.parent.glob("embeddings.sqlite-*")) == []


def test_read_only_embedding_cache_without_a_table_computes_without_repairing_it(tmp_path: Path):
    """A malformed/old cache database is a miss, not an invitation to create its table."""
    cache_path = tmp_path / "cache" / "embeddings.sqlite"
    cache_path.parent.mkdir()
    with sqlite3.connect(cache_path) as connection:
        connection.execute("CREATE TABLE unrelated (value TEXT)")
        connection.execute("INSERT INTO unrelated VALUES ('keep')")
    before_bytes = cache_path.read_bytes()
    before_mtime_ns = cache_path.stat().st_mtime_ns

    provider = CountingEmbedder()
    embedder = CachedEmbedder(provider, cache_path, read_only=True)
    assert embedder.embed_queries(["missing schema query"])

    assert provider.calls == [[f"{QUERY_PREFIX}missing schema query"]]
    assert cache_path.read_bytes() == before_bytes
    assert cache_path.stat().st_mtime_ns == before_mtime_ns
    with sqlite3.connect(f"{cache_path.resolve().as_uri()}?mode=ro&immutable=1", uri=True) as connection:
        assert connection.execute("SELECT value FROM unrelated").fetchall() == [("keep",)]
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='embeddings'"
        ).fetchone()[0] == 0


def test_read_only_embedding_cache_uses_hits_without_mutating_existing_cache(tmp_path: Path):
    """A hit and a miss leave the existing cache byte-for-byte unchanged.

    The immutable SQLite URI is important here: a plain ``mode=ro`` connection
    can create ``-wal``/``-shm`` sidecars merely by reading a WAL database.
    """
    cache_path = tmp_path / "corpus" / ".alexandria" / "cache" / "embeddings.sqlite"
    seed = CachedEmbedder(CountingEmbedder(), cache_path)
    seeded_vector = seed.embed_queries(["cached ablation query"])[0]
    seed._connection.close()
    assert list(cache_path.parent.glob("embeddings.sqlite-*")) == []

    before_bytes = cache_path.read_bytes()
    before_mtime_ns = cache_path.stat().st_mtime_ns
    cache_uri = f"{cache_path.resolve().as_uri()}?mode=ro&immutable=1"
    with sqlite3.connect(cache_uri, uri=True) as connection:
        before_rows = connection.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]

    provider = CountingEmbedder()
    embedder = CachedEmbedder(provider, cache_path, read_only=True)
    vectors = embedder.embed_queries(["cached ablation query", "uncached ablation query"])

    assert vectors[0] == seeded_vector
    assert provider.calls == [[f"{QUERY_PREFIX}uncached ablation query"]]
    assert embedder.last_cache_stats == {"hits": 1, "misses": 1}
    assert cache_path.read_bytes() == before_bytes
    assert cache_path.stat().st_mtime_ns == before_mtime_ns
    with sqlite3.connect(cache_uri, uri=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == before_rows
    assert list(cache_path.parent.glob("embeddings.sqlite-*")) == []



def test_read_only_cache_survives_an_ablation_like_search_query_flow(tmp_path: Path):
    """Hybrid query flow computes an uncached query without touching cache bytes.

    The synthetic vector and lexical indexes are prepared before the snapshot;
    the only cache this query path receives is a read-only ``CachedEmbedder``.
    """
    from alexandria.index.bm25 import BM25Index
    from alexandria.index.store import VectorStore
    from alexandria.retrieval.rerank import IdentityReranker
    from alexandria.retrieval.search import SearchConfig, SearchEngine

    corpus = tmp_path / "corpus"
    cache_path = corpus / ".alexandria" / "cache" / "embeddings.sqlite"
    writer = CachedEmbedder(CountingEmbedder(), cache_path)
    writer.embed_queries(["cached evaluation query"])
    writer._connection.close()
    before_bytes = cache_path.read_bytes()
    before_mtime_ns = cache_path.stat().st_mtime_ns
    with sqlite3.connect(f"{cache_path.resolve().as_uri()}?mode=ro&immutable=1", uri=True) as connection:
        before_rows = connection.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]

    document_embedder = HashEmbedder(dim=3)
    rows = [{
        "chunk_id": "sources/a", "doc_id": "sources/a", "text": "evaluation query evidence",
        "heading_path": "Evidence", "vector": document_embedder.embed(["evidence"])[0],
        "type": "observation", "project": None, "status": "active", "source": "test",
        "tags": [], "entities": [], "layer": "sources", "generated_at": None,
        "enrichment": None, "kind": None, "parent_doc": None, "target_chunk": None,
    }]
    store = VectorStore(corpus / ".alexandria" / "index")
    store.upsert(rows)
    bm25 = BM25Index(corpus / ".alexandria" / "index" / "fts.sqlite")
    bm25.index(rows)

    provider = CountingEmbedder()
    engine = SearchEngine(
        CachedEmbedder(provider, cache_path, read_only=True), store, bm25,
        IdentityReranker(), SearchConfig(prefetch=1, top_k=1), logger=None, query_cache=None,
    )
    engine.search("cached evaluation query")
    engine.search("uncached evaluation query")

    assert provider.calls == [[f"{QUERY_PREFIX}uncached evaluation query"]]
    assert cache_path.read_bytes() == before_bytes
    assert cache_path.stat().st_mtime_ns == before_mtime_ns
    with sqlite3.connect(f"{cache_path.resolve().as_uri()}?mode=ro&immutable=1", uri=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == before_rows
    assert list(cache_path.parent.glob("embeddings.sqlite-*")) == []


class _VectorsEmbedder:
    name = "vectors-v1"

    def __init__(self, vectors: list[list[float]], *, dim: int | None = None) -> None:
        self.vectors = vectors
        self.dim = dim if dim is not None else len(vectors[0])
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return self.vectors[:len(texts)]


def test_cached_embedder_normalizes_extreme_finite_vectors_without_zeroing_them(tmp_path: Path):
    """Finite provider values at the limits of binary64 remain finite unit vectors.

    A naive ``sqrt(sum(x*x))`` overflows for the large case and underflows for
    the small case. Both would silently turn a valid nonzero provider vector
    into an unusable cached zero vector.
    """
    vectors = [[1e308, 1e308], [1e-308, 1e-308]]
    provider = _VectorsEmbedder(vectors)
    embedder = CachedEmbedder(provider, tmp_path / "embeddings.sqlite")

    normalized = embedder.embed(["large", "small"])

    assert len(normalized) == 2
    for vector in normalized:
        assert vector == pytest.approx([2 ** -0.5, 2 ** -0.5])
        assert all(value != 0.0 for value in vector)
        assert all(abs(value) < float("inf") for value in vector)
        assert sum(value * value for value in vector) == pytest.approx(1.0)


def test_wrong_width_cached_vector_is_recomputed_not_returned(tmp_path: Path):
    """A numeric legacy row is not trustworthy if it does not match provider.dim."""
    provider = _VectorsEmbedder([[3.0, 4.0, 0.0]], dim=3)
    cache_path = tmp_path / "embeddings.sqlite"
    embedder = CachedEmbedder(provider, cache_path)
    key = embedder._key("same")
    embedder._connection.execute(
        "INSERT INTO embeddings(cache_key, vector) VALUES(?, ?)",
        (key, "[3.0, 4.0]"),
    )
    embedder._connection.commit()

    assert embedder.embed(["same"])[0] == pytest.approx([0.6, 0.8, 0.0])
    assert provider.calls == [["same"]]
    stored = embedder._connection.execute(
        "SELECT vector FROM embeddings WHERE cache_key = ?", (key,)
    ).fetchone()[0]
    assert stored == "[0.6,0.8,0.0]"


def test_wrong_width_cached_vector_recomputes_all_duplicate_positions(tmp_path: Path):
    """A bad legacy hit for repeated text is one miss, not a dropped result."""
    provider = _VectorsEmbedder([[3.0, 4.0, 0.0]], dim=3)
    embedder = CachedEmbedder(provider, tmp_path / "embeddings.sqlite")
    key = embedder._key("same")
    embedder._connection.execute(
        "INSERT INTO embeddings(cache_key, vector) VALUES(?, ?)", (key, "[3.0, 4.0]")
    )
    embedder._connection.commit()

    vectors = embedder.embed(["same", "same"])

    assert vectors == [pytest.approx([0.6, 0.8, 0.0])] * 2
    assert provider.calls == [["same"]]


def test_invalid_provider_batch_fails_before_any_cache_row_is_persisted(tmp_path: Path):
    """One bad provider vector must not leave a partially populated cache batch."""
    provider = _VectorsEmbedder([[3.0, 4.0], [1.0]], dim=2)
    embedder = CachedEmbedder(provider, tmp_path / "embeddings.sqlite")

    with pytest.raises(ValueError, match="dimension"):
        embedder.embed(["first", "bad"])

    assert embedder._connection.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == 0


@pytest.mark.skipif(sys.platform == "win32", reason="flock contract is POSIX-only")
def test_read_only_cache_uses_the_shared_writer_lock_before_opening_immutable_db(tmp_path: Path):
    """An immutable SQLite connection must never race a normal cache writer."""
    import fcntl

    corpus = tmp_path / "corpus"
    cache_path = corpus / ".alexandria" / "cache" / "embeddings.sqlite"
    seed = CachedEmbedder(CountingEmbedder(), cache_path)
    seed.embed_queries(["cached query"])
    seed.close()
    lock_path = cache_path.with_name(f"{cache_path.name}.lock")
    assert lock_path.is_file()

    reader = CachedEmbedder(CountingEmbedder(), cache_path, read_only=True)
    assert reader._connection is not None
    reader.close()

    with lock_path.open("r") as writer_lock:
        fcntl.flock(writer_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        blocked_reader = CachedEmbedder(CountingEmbedder(), cache_path, read_only=True)
        assert blocked_reader._connection is None
        blocked_reader.close()


@pytest.mark.skipif(sys.platform == "win32", reason="flock contract is POSIX-only")
def test_normal_cache_writer_waits_until_immutable_reader_releases_shared_lock(tmp_path: Path):
    """The normal mutation path honors an active read-only cache snapshot."""
    corpus = tmp_path / "corpus"
    cache_path = corpus / ".alexandria" / "cache" / "embeddings.sqlite"
    seed = CachedEmbedder(CountingEmbedder(), cache_path)
    seed.embed_queries(["cached query"])
    seed.close()
    reader = CachedEmbedder(CountingEmbedder(), cache_path, read_only=True)
    assert reader._connection is not None

    started, completed = threading.Event(), threading.Event()
    writers = []

    def open_writer():
        started.set()
        writers.append(CachedEmbedder(CountingEmbedder(), cache_path))
        completed.set()

    thread = threading.Thread(target=open_writer)
    thread.start()
    assert started.wait(timeout=1)
    assert not completed.wait(timeout=0.1), "writer bypassed immutable-reader lock"
    reader.close()
    assert completed.wait(timeout=1), "writer did not resume after reader closed"
    thread.join(timeout=1)
    assert not thread.is_alive()
    writers[0].close()


@pytest.mark.parametrize("invalid", [[0.0, 0.0], [float("nan"), 1.0], [float("inf"), 1.0]])
def test_invalid_provider_vector_never_reaches_the_embedding_cache(tmp_path: Path, invalid):
    """Zero, NaN, and infinity must fail before an SQL row is committed."""
    provider = _VectorsEmbedder([invalid], dim=2)
    embedder = CachedEmbedder(provider, tmp_path / "embeddings.sqlite")

    with pytest.raises(ValueError):
        embedder.embed(["bad"])

    assert embedder._connection.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == 0


def test_nonunit_legacy_cache_hit_is_normalized_before_returning(tmp_path: Path):
    """A numeric pre-policy cache row cannot bypass the wrapper's L2 boundary."""
    provider = _VectorsEmbedder([[1.0, 0.0, 0.0]], dim=3)
    embedder = CachedEmbedder(provider, tmp_path / "embeddings.sqlite")
    key = embedder._key("legacy")
    embedder._connection.execute(
        "INSERT INTO embeddings(cache_key, vector) VALUES(?, ?)", (key, "[3.0,4.0,0.0]")
    )
    embedder._connection.commit()

    assert embedder.embed(["legacy"])[0] == pytest.approx([0.6, 0.8, 0.0])
    assert provider.calls == []


@pytest.mark.parametrize("invalid_json", ["[0.0,0.0,0.0]", "[NaN,0.0,1.0]", "[Infinity,0.0,1.0]"])
def test_invalid_legacy_cache_row_is_recomputed_before_returning(tmp_path: Path, invalid_json: str):
    """Existing zero/non-finite rows are cache misses, never query vectors."""
    provider = _VectorsEmbedder([[3.0, 4.0, 0.0]], dim=3)
    embedder = CachedEmbedder(provider, tmp_path / "embeddings.sqlite")
    key = embedder._key("legacy")
    embedder._connection.execute(
        "INSERT INTO embeddings(cache_key, vector) VALUES(?, ?)", (key, invalid_json)
    )
    embedder._connection.commit()

    assert embedder.embed(["legacy"])[0] == pytest.approx([0.6, 0.8, 0.0])
    assert provider.calls == [["legacy"]]


def test_cache_identity_includes_declared_output_dimension(tmp_path: Path):
    """Same backend name but a changed output width cannot reuse old entries."""
    cache_path = tmp_path / "embeddings.sqlite"
    first = _VectorsEmbedder([[1.0, 0.0]], dim=2)
    second = _VectorsEmbedder([[1.0, 0.0, 0.0]], dim=3)
    second.name = first.name

    CachedEmbedder(first, cache_path).embed(["same"])
    CachedEmbedder(second, cache_path).embed(["same"])

    assert first.calls == [["same"]]
    assert second.calls == [["same"]]


@pytest.mark.parametrize("invalid", [[10 ** 400, 0.0], [0.0, 10 ** 400]])
def test_provider_conversion_overflow_fails_before_any_cache_write(tmp_path: Path, invalid):
    """Finite Python ints that overflow binary64 are controlled invalid output."""
    provider = _VectorsEmbedder([invalid], dim=2)
    embedder = CachedEmbedder(provider, tmp_path / "embeddings.sqlite")

    with pytest.raises(ValueError, match="non-numeric"):
        embedder.embed(["overflow"])

    assert embedder._connection.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == 0


def test_overflowing_legacy_cache_row_recomputes_all_duplicate_positions(tmp_path: Path):
    """A numeric JSON integer too large for float is a cache miss, not a crash."""
    provider = _VectorsEmbedder([[3.0, 4.0, 0.0]], dim=3)
    embedder = CachedEmbedder(provider, tmp_path / "embeddings.sqlite")
    key = embedder._key("legacy")
    embedder._connection.execute(
        "INSERT INTO embeddings(cache_key, vector) VALUES(?, ?)",
        (key, json.dumps([10 ** 400, 0, 0])),
    )
    embedder._connection.commit()

    assert embedder.embed(["legacy", "legacy"]) == [pytest.approx([0.6, 0.8, 0.0])] * 2
    assert provider.calls == [["legacy"]]


class _FailSecondCacheWrite:
    """Fault-injection proxy: one cache INSERT succeeds, the second fails."""

    def __init__(self, connection):
        self.connection = connection
        self.insert_calls = 0
        self.fail = True

    def execute(self, sql, *args):
        if sql.startswith("INSERT INTO embeddings"):
            self.insert_calls += 1
            if self.fail and self.insert_calls == 2:
                raise sqlite3.OperationalError("injected second write failure")
        return self.connection.execute(sql, *args)

    def commit(self):
        return self.connection.commit()

    def rollback(self):
        return self.connection.rollback()

    def close(self):
        return self.connection.close()


def test_sql_failure_mid_batch_rolls_back_before_a_later_call_can_commit_it(tmp_path: Path):
    """A failed cache batch cannot leak its first row into a later transaction."""
    provider = _VectorsEmbedder([[3.0, 4.0], [5.0, 12.0]], dim=2)
    embedder = CachedEmbedder(provider, tmp_path / "embeddings.sqlite")
    raw_connection = embedder._connection
    faulty = _FailSecondCacheWrite(raw_connection)
    embedder._connection = faulty

    with pytest.raises(sqlite3.OperationalError, match="injected"):
        embedder.embed(["first", "second"])

    assert not raw_connection.in_transaction
    assert raw_connection.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == 0

    faulty.fail = False
    assert embedder.embed(["later"])[0] == pytest.approx([0.6, 0.8])
    assert raw_connection.execute("SELECT COUNT(*) FROM embeddings").fetchall() == [(1,)]


@pytest.mark.skipif(sys.platform == "win32", reason="flock contract is POSIX-only")
def test_normal_cache_close_waits_for_an_active_immutable_reader(tmp_path: Path):
    """Normal SQLite close stays in the exclusive lifecycle lock boundary."""
    cache_path = tmp_path / "embeddings.sqlite"
    writer = CachedEmbedder(CountingEmbedder(), cache_path)
    writer.embed(["seed"])
    reader = CachedEmbedder(CountingEmbedder(), cache_path, read_only=True)
    assert reader._connection is not None

    started, closed = threading.Event(), threading.Event()

    def close_writer():
        started.set()
        writer.close()
        closed.set()

    thread = threading.Thread(target=close_writer)
    thread.start()
    assert started.wait(timeout=1)
    assert not closed.wait(timeout=0.1), "writer close bypassed immutable-reader lock"
    reader.close()
    assert closed.wait(timeout=1), "writer close did not resume after reader closed"
    thread.join(timeout=1)
    assert not thread.is_alive()


class _FailCacheCommit:
    """Fault-injection proxy that raises before the first real commit."""

    def __init__(self, connection):
        self.connection = connection
        self.fail = True

    def execute(self, *args, **kwargs):
        return self.connection.execute(*args, **kwargs)

    def commit(self):
        if self.fail:
            raise sqlite3.OperationalError("injected commit failure")
        return self.connection.commit()

    def rollback(self):
        return self.connection.rollback()

    def close(self):
        return self.connection.close()


def test_sql_commit_failure_rolls_back_the_entire_cache_batch(tmp_path: Path):
    """A raised commit cannot leave a transaction for a later embed to persist."""
    provider = _VectorsEmbedder([[3.0, 4.0], [5.0, 12.0]], dim=2)
    embedder = CachedEmbedder(provider, tmp_path / "embeddings.sqlite")
    raw_connection = embedder._connection
    faulty = _FailCacheCommit(raw_connection)
    embedder._connection = faulty

    with pytest.raises(sqlite3.OperationalError, match="injected commit"):
        embedder.embed(["first", "second"])

    assert not raw_connection.in_transaction
    assert raw_connection.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == 0

    faulty.fail = False
    assert embedder.embed(["later"])[0] == pytest.approx([0.6, 0.8])
    assert raw_connection.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == 1


@pytest.mark.skipif(sys.platform == "win32", reason="flock contract is POSIX-only")
def test_cache_writer_fails_loudly_instead_of_hanging_on_a_long_lived_reader(tmp_path: Path):
    """An evaluator that holds its snapshot must not wedge indexing forever.

    A read-only leg-ablation run holds its shared lock for its whole lifetime
    (many minutes over a golden set). An unbounded LOCK_EX would block every
    subsequent index run with no diagnostic -- the same silent-stall class this
    project already rejected for the corpus write lock.
    """
    from alexandria.index import embedder as embedder_mod

    cache_path = tmp_path / "embeddings.sqlite"
    seed = CachedEmbedder(CountingEmbedder(), cache_path)
    seed.embed(["seed"])
    seed.close()

    reader = CachedEmbedder(CountingEmbedder(), cache_path, read_only=True)
    assert reader._connection is not None
    try:
        with pytest.raises(embedder_mod.EmbeddingCacheBusy, match="read-only"):
            CachedEmbedder(CountingEmbedder(), cache_path, lock_timeout=0.2)
    finally:
        reader.close()

    # The bound is a fail-loudly deadline, not a permanent refusal.
    late = CachedEmbedder(CountingEmbedder(), cache_path, lock_timeout=0.2)
    assert late.embed(["seed"])
    late.close()


def test_cache_identity_key_keeps_both_widths_addressable(tmp_path: Path):
    """Row coexistence, not just a recompute, proves dim is part of the key."""
    cache_path = tmp_path / "embeddings.sqlite"
    first = _VectorsEmbedder([[1.0, 0.0]], dim=2)
    second = _VectorsEmbedder([[1.0, 0.0, 0.0]], dim=3)
    second.name = first.name

    a = CachedEmbedder(first, cache_path)
    a.embed(["same"])
    a.close()
    b = CachedEmbedder(second, cache_path)
    b.embed(["same"])

    rows = b._connection.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    assert rows == 2, "a width change overwrote the other width's entry instead of keying apart"
    b.close()


# ---------------------------------------------------------------------------
# #44: offline-degradation -- LocalEmbedder must FAIL FAST AND LOUD on a
# hung/unreachable network, never silently degrade (unlike the reranker,
# there is no safe substitute for a real vector: a garbage embedding would
# poison the index or a query invisibly).
# ---------------------------------------------------------------------------

def test_local_embedder_load_timeout_is_configurable_and_bounded_by_default():
    default = LocalEmbedder()
    assert 0 < default.load_timeout <= 120.0


def test_local_embedder_raises_within_its_bound_on_a_hung_load(monkeypatch):
    import time
    import types

    from alexandria.model_load import ModelLoadTimeout

    class HangingST:
        def __init__(self, *args, **kwargs):
            time.sleep(30)

    fake_st = types.ModuleType("sentence_transformers")
    fake_st.SentenceTransformer = HangingST
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_st)

    embedder = LocalEmbedder(load_timeout=0.2)
    started = time.monotonic()
    with pytest.raises(ModelLoadTimeout, match="Qwen"):
        embedder.embed(["probe"])
    elapsed = time.monotonic() - started
    assert elapsed < 5.0, f"embed() blocked the caller for {elapsed:.1f}s past its bound"


def test_local_embedder_timeout_error_names_the_provider_and_model(monkeypatch):
    import time
    import types

    fake_st = types.ModuleType("sentence_transformers")

    class HangingST:
        def __init__(self, *args, **kwargs):
            time.sleep(30)

    fake_st.SentenceTransformer = HangingST
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_st)

    embedder = LocalEmbedder(model="test/probe-model-name", load_timeout=0.1)
    try:
        embedder.embed(["probe"])
        assert False, "must have raised"
    except Exception as exc:
        assert "test/probe-model-name" in str(exc)


def test_local_embedder_a_real_exception_is_not_masked_as_a_timeout(monkeypatch):
    """load_with_timeout must propagate a genuine failure (bad model id, no
    network at all under HF_HUB_OFFLINE) AS ITSELF, not disguise it as a
    generic timeout -- the cause (offline vs slow) must stay distinguishable."""
    import types

    class BoomST:
        def __init__(self, *args, **kwargs):
            raise OSError("couldn't connect to huggingface.co and nothing is cached")

    fake_st = types.ModuleType("sentence_transformers")
    fake_st.SentenceTransformer = BoomST
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_st)

    embedder = LocalEmbedder(load_timeout=5.0)
    with pytest.raises(OSError, match="cached"):
        embedder.embed(["probe"])


def test_local_embedder_does_not_retry_the_full_timeout_on_a_second_call(monkeypatch):
    """The CI-hang bug class, through the shared keyed cooldown: a failed load
    must not be re-attempted by a second call within the cooldown window --
    .dim and .embed both call _load() independently, and under the OLD
    design every such call re-paid the full timeout (36+ call sites = 18
    minutes, observed live). Now the first failure is remembered keyed by
    model+device, and the second call fails in microseconds."""
    import time
    import types

    load_calls = []

    class HangingST:
        def __init__(self, *args, **kwargs):
            load_calls.append(time.monotonic())
            time.sleep(30)

    fake_st = types.ModuleType("sentence_transformers")
    fake_st.SentenceTransformer = HangingST
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_st)

    embedder = LocalEmbedder(load_timeout=0.1)
    started = time.monotonic()
    with pytest.raises(Exception):
        embedder.embed(["probe one"])
    with pytest.raises(Exception):
        embedder.embed(["probe two"])
    elapsed = time.monotonic() - started

    assert elapsed < 0.5, (
        f"a second call on the same failed instance took {elapsed:.2f}s -- "
        f"it re-attempted the load instead of failing fast")
    assert len(load_calls) == 1, (
        f"the underlying SentenceTransformer constructor was invoked "
        f"{len(load_calls)} times for two calls on one instance -- only the "
        f"FIRST should ever attempt the network")


def test_purge_removes_a_cached_row_so_it_recomputes_on_next_embed(tmp_path: Path):
    """#6 erasure: purge() must actually delete the row, not just mark it --
    the next embed() call for the same text should hit the provider again,
    not silently reattach a purged vector from some soft-delete flag."""
    provider = CountingEmbedder()
    embedder = CachedEmbedder(provider, tmp_path / "embeddings.sqlite")

    embedder.embed(["erase me", "keep me"])
    assert len(provider.calls) == 1

    deleted = embedder.purge(["erase me"])
    assert deleted == 1

    embedder.embed(["erase me", "keep me"])
    # "erase me" recomputed (a real provider call), "keep me" still cached
    assert provider.calls == [["erase me", "keep me"], ["erase me"]]


def test_purge_of_never_cached_text_returns_zero_not_an_error(tmp_path: Path):
    """Matches EnrichmentStore.invalidate()'s contract: purging something
    that was never there is a normal outcome, honestly reported as zero."""
    embedder = CachedEmbedder(CountingEmbedder(), tmp_path / "embeddings.sqlite")
    assert embedder.purge(["never embedded"]) == 0


def test_purge_respects_the_mode_distinction(tmp_path: Path):
    """Document-space and query-space embeddings of the same text are
    DIFFERENT cache rows (mode is part of the key) -- purging the document
    mode must not touch the query-mode row for identical text."""
    embedder = CachedEmbedder(CountingEmbedder(), tmp_path / "embeddings.sqlite")
    embedder.embed(["shared text"], mode="d")
    embedder.embed(["shared text"], mode="q")

    deleted = embedder.purge(["shared text"], mode="d")
    assert deleted == 1

    row = embedder._connection.execute(
        "SELECT COUNT(*) FROM embeddings").fetchone()[0]
    assert row == 1  # the query-mode row survives


def test_purge_is_a_noop_in_read_only_mode(tmp_path: Path):
    """A read-only (evaluation-only) cache instance must never mutate the
    durable cache, regardless of what a caller asks it to purge -- matches
    this class's existing read_only contract for embed() itself."""
    cache_path = tmp_path / "embeddings.sqlite"
    writer = CachedEmbedder(CountingEmbedder(), cache_path)
    writer.embed(["persisted"])
    writer.close()

    reader = CachedEmbedder(CountingEmbedder(), cache_path, read_only=True)
    assert reader.purge(["persisted"]) == 0
    reader.close()  # release the shared lock before reopening normally below

    # confirm it genuinely was not touched
    writer2 = CachedEmbedder(CountingEmbedder(), cache_path)
    assert writer2.embed(["persisted"])[0] is not None
    assert writer2.last_cache_stats["hits"] == 1  # still cached
