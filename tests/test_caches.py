"""Query + response cache tests (unit + SearchEngine integration)."""

import json
import time

from alexandria.cache import (
    QueryCache,
    ResponseCache,
    normalize_query,
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
    assert engine.last_cache_hit is False  # first call: miss
    assert len(r1) == 2
    r2 = engine.search("sweep page lint")
    assert engine.last_cache_hit is True  # second call: hit
    assert [x.chunk_id for x in r2] == [x.chunk_id for x in r1]
    # different k: cache miss
    engine.search("sweep page lint", k=1)
    assert engine.last_cache_hit is False


def test_search_engine_cache_never_breaks_pipeline(tmp_path):
    cache = TinyTTLQueryCache(tmp_path)  # ttl=0: everything stale -> miss path
    engine = build_engine(tmp_path)
    engine.query_cache = cache
    results = engine.search("sweep page lint")
    assert len(results) == 2
    assert engine.last_cache_hit is False
