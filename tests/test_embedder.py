"""Embedders are deterministic offline in tests and cache model work by content."""

import sqlite3

import pytest
from pathlib import Path

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
