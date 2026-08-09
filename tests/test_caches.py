"""Query + response cache tests (unit + SearchEngine integration)."""

import json
import time

from alexandria.cache import (
    QueryCache,
    ResponseCache,
    canonical,
    normalize_query,
    read_index_generation,
    write_index_generation,
)
from alexandria.index.bm25 import BM25Index
from alexandria.index.embedder import HashEmbedder
from alexandria.index.store import VectorStore
from alexandria.retrieval.rerank import IdentityReranker
from alexandria.retrieval.search import SearchConfig, SearchEngine
from tests.test_search import build_engine, record


class TinyTTLQueryCache(QueryCache):
    def __init__(self, corpus):
        super().__init__(corpus)
        self.ttl = 0  # everything is immediately stale


def test_query_cache_roundtrip_and_key_stability(tmp_path):
    c = QueryCache(tmp_path)
    k1 = c.key("  sweep   page ", 5, "cfg", "filters")
    k2 = c.key("sweep page", 5, "cfg", "filters")  # whitespace-normalized equal
    assert k1 == k2
    assert c.get(k1) is None
    c.put(k1, [{"chunk_id": "sources/a", "score": 0.9}])
    assert c.get(k1) == [{"chunk_id": "sources/a", "score": 0.9}]
    # different k or config => different key
    assert c.key("sweep page", 3, "cfg", "filters") != k1
    # case is preserved (BM25 exactness): "Sweep" is not the same query
    assert c.key("Sweep page", 5, "cfg", "filters") != k1
    st = c.stats()
    assert st.size == 1
    assert c.clear() == 1
    assert c.stats().size == 0


def test_query_cache_ttl_expiry(tmp_path):
    c = TinyTTLQueryCache(tmp_path)
    c.put("k", [1])
    assert c.get("k") is None  # ttl=0 => immediately stale


def test_response_cache_roundtrip(tmp_path):
    c = ResponseCache(tmp_path)
    k = c.key("What happened with the key?", "model-x", 8, "v1")
    c.put(k, {"text": "# page", "n_claims": 3})
    hit = c.get(k)
    assert hit == {"text": "# page", "n_claims": 3}
    # question normalization: trailing/leading whitespace is the same question
    k2 = c.key("  What happened with the key?  ", "model-x", 8, "v1")
    assert k2 == k
    # model/k/prompt_version changes invalidate
    assert c.key("What happened with the key?", "model-y", 8, "v1") != k
    assert c.key("What happened with the key?", "model-x", 3, "v1") != k
    assert c.key("What happened with the key?", "model-x", 8, "v2") != k


def test_cache_survives_corrupt_payload(tmp_path):
    c = QueryCache(tmp_path)
    c.con.execute("INSERT INTO cache (key, payload, ts) VALUES (?, ?, ?)",
                  ("bad", "not json{{", time.time()))
    c.con.commit()
    assert c.get("bad") is None
    assert c.errors


def test_normalize_query_collapses_whitespace():
    assert normalize_query("  a\t b\n c  ") == "a b c"


def test_search_engine_query_cache_hit_returns_same_results(tmp_path):
    cache = QueryCache(tmp_path)
    engine = build_engine(tmp_path)
    engine.query_cache = cache
    r1 = engine.search("sweep page lint")
    assert engine.last_cache_hit == 0  # first call: miss
    assert len(r1) == 2
    r2 = engine.search("sweep page lint")
    assert engine.last_cache_hit == 1  # second call: query-cache hit
    assert [x.chunk_id for x in r2] == [x.chunk_id for x in r1]
    # different k: cache miss
    engine.search("sweep page lint", k=1)
    assert engine.last_cache_hit == 0


def test_search_engine_cache_never_breaks_pipeline(tmp_path):
    cache = TinyTTLQueryCache(tmp_path)  # ttl=0: everything stale -> miss path
    engine = build_engine(tmp_path)
    engine.query_cache = cache
    results = engine.search("sweep page lint")
    assert len(results) == 2
    assert engine.last_cache_hit == 0


def test_query_cache_key_binds_to_corpus_generation(tmp_path):
    c = QueryCache(tmp_path)
    k0 = c.key("same query", 5, {"a": 1}, {"tier": "map"}, generation=0)
    k1 = c.key("same query", 5, {"a": 1}, {"tier": "map"}, generation=1)
    assert k0 != k1  # reindex must invalidate


def test_response_cache_key_binds_to_generation(tmp_path):
    c = ResponseCache(tmp_path)
    k = c.key("q", "model-x", 8, "v1", generation=3)
    assert k != c.key("q", "model-x", 8, "v1", generation=4)


def test_canonical_is_deterministic_and_not_repr(tmp_path):
    a = canonical({"b": [1, 2], "a": {"x": {3, 1}}, "f": 0.5})
    b = canonical({"a": {"x": {1, 3}}, "b": [1, 2], "f": 0.5})
    assert a == b  # key order + set order free
    assert "set(" not in a and "at 0x" not in a
    # config dict and filter dict both fold into the key
    assert QueryCache(tmp_path).key("q", 5, {"top_k": 3}, {"tier": "map"}) != \
        QueryCache(tmp_path).key("q", 5, {"top_k": 4}, {"tier": "map"})


def test_index_generation_roundtrip(tmp_path):
    assert read_index_generation(tmp_path) == 0  # missing file
    assert write_index_generation(tmp_path) == 1
    assert write_index_generation(tmp_path) == 2
    assert read_index_generation(tmp_path) == 2


def test_embedder_cache_mode_separation(tmp_path):
    """Red: query-space and document-space vectors of the SAME text must
    never satisfy each other through the embedding cache."""
    from alexandria.index.embedder import CachedEmbedder
    cache = CachedEmbedder(HashEmbedder(), tmp_path / "emb.sqlite")
    cache.embed(["some text"], mode="d")
    assert cache.last_cache_stats == {"hits": 0, "misses": 1}
    # same text, same mode: hit
    cache.embed(["some text"], mode="d")
    assert cache.last_cache_stats == {"hits": 1, "misses": 0}
    # query-mode request never reads the document-mode row
    cache.embed(["some text"], mode="q")
    assert cache.last_cache_stats == {"hits": 0, "misses": 1}
    assert cache._key("some text", "d") != cache._key("some text", "q")
