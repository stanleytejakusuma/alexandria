"""The vector store retains all chunk metadata and filters before limiting results."""

from pathlib import Path

import pytest

from alexandria.index.store import VectorStore


def record(chunk_id: str, doc_id: str, vector: list[float], **meta) -> dict:
    return {
        "chunk_id": chunk_id,
        "doc_id": doc_id,
        "text": meta.pop("text", f"text for {chunk_id}"),
        "heading_path": meta.pop("heading_path", "Heading"),
        "vector": vector,
        "type": meta.pop("type", "observation"),
        "project": meta.pop("project", None),
        "status": meta.pop("status", "active"),
        "source": meta.pop("source", "test"),
        "tags": meta.pop("tags", []),
        "entities": meta.pop("entities", []),
        "layer": meta.pop("layer", None),
        "generated_at": meta.pop("generated_at", None),
    }


def test_store_upserts_gets_and_derives_layer(tmp_path: Path):
    store = VectorStore(tmp_path / "index")
    store.upsert([record("source", "sources/notes/a", [1.0, 0.0]),
                  record("wiki", "wiki/topics/a", [0.0, 1.0])])

    assert store.count() == 2
    assert store.get("source")["layer"] == "sources"
    assert store.get("wiki")["layer"] == "wiki"


def test_store_applies_metadata_filter_before_vector_limit(tmp_path: Path):
    store = VectorStore(tmp_path / "index")
    store.upsert([
        record("near-source", "sources/a", [1.0, 0.0], project="one"),
        record("matching-wiki", "wiki/a", [0.8, 0.2], project="two"),
        record("matching-source", "sources/b", [0.7, 0.3], project="two"),
    ])

    results = store.search_vector([1.0, 0.0], k=2, where={"project": "two"})

    assert [result["chunk_id"] for result in results] == ["matching-wiki", "matching-source"]


def test_store_filters_list_metadata_and_drop(tmp_path: Path):
    store = VectorStore(tmp_path / "index")
    store.upsert([record("a", "sources/a", [1.0, 0.0], tags=["python", "retrieval"]),
                  record("b", "sources/b", [0.0, 1.0], tags=["rust"])])

    assert [row["chunk_id"] for row in store.search_vector([1.0, 0.0], 5,
                                                               where={"tags": "retrieval"})] == ["a"]
    store.drop()
    assert store.count() == 0


def test_get_many_returns_all_requested_records(tmp_path: Path):
    """Fusion looked each candidate up individually -- up to 40 separate scans per
    query, measured at ~494ms. One batched query replaces them."""
    store = VectorStore(tmp_path / "index")
    store.upsert([record(f"c{i}", f"sources/n/{i}", [float(i), 0.0]) for i in range(5)])

    got = store.get_many([f"c{i}" for i in range(5)])

    assert set(got) == {f"c{i}" for i in range(5)}
    assert got["c3"]["chunk_id"] == "c3"


def test_get_many_handles_missing_and_empty(tmp_path: Path):
    store = VectorStore(tmp_path / "index")
    store.upsert([record("real", "sources/n/a", [1.0, 0.0])])

    assert store.get_many([]) == {}
    got = store.get_many(["real", "does-not-exist"])
    assert set(got) == {"real"}          # missing ids are absent, never invented


def test_get_many_agrees_with_get(tmp_path: Path):
    store = VectorStore(tmp_path / "index")
    store.upsert([record("a", "sources/n/a", [1.0, 0.0])])
    assert store.get_many(["a"])["a"] == store.get("a")


def test_append_matches_upsert_for_new_records(tmp_path: Path):
    """append() skips merge_insert's match scan, which is O(table size) per call and
    makes a full rebuild O(n^2). It is only sound when every chunk_id is new, so the
    rows it produces must be indistinguishable from the upsert path's."""
    rows = [record(f"c{i}", f"sources/{i}", [1.0, 0.0]) for i in range(5)]

    merged = VectorStore(tmp_path / "merged")
    merged.upsert(rows)
    appended = VectorStore(tmp_path / "appended")
    appended.append(rows)

    assert appended.count() == merged.count() == 5
    for i in range(5):
        assert appended.get(f"c{i}") == merged.get(f"c{i}")


def test_append_in_several_calls_keeps_every_row(tmp_path: Path):
    """The rebuild path appends in buffered batches, not one shot -- a later append
    must not clobber or drop rows written by an earlier one."""
    store = VectorStore(tmp_path / "index")
    store.append([record("a", "sources/a", [1.0, 0.0])])
    store.append([record("b", "sources/b", [0.0, 1.0])])
    store.append([])

    assert store.count() == 2
    assert store.get("a")["chunk_id"] == "a"
    assert store.get("b")["chunk_id"] == "b"


# ---------------------------------------------------------------------------
# #45: reading an unverified_legacy index must use cosine distance, not
# LanceDB's default (raw L2, scale-sensitive) metric. Measured live: a
# same-direction vector at 100x magnitude ranked as if nearly orthogonal
# under the default metric -- the exact "quietly wrong ranking" this guard
# exists to prevent. This is a LanceDB-specific fix; the SQLite fallback's
# _cosine already computes true (scale-invariant) cosine similarity
# unconditionally, so it needs no change (verified below too).
# ---------------------------------------------------------------------------

def test_search_vector_uses_cosine_distance_not_raw_l2(tmp_path: Path):
    """#45 (Red review, 2026-08-20): forced UNCONDITIONALLY for every
    LanceDB search, not a caller-wired opt-in -- a same-direction vector at
    100x magnitude must rank WITH the unit vector, not behind an orthogonal
    one (LanceDB's default metric is raw L2 distance, which is scale-
    sensitive; measured live before this fix: the 100x vector ranked at
    distance 9801, behind an orthogonal unit vector at distance 2.0)."""
    store = VectorStore(tmp_path)
    store.upsert([
        record("unit_same_dir", "sources/a", [1.0, 0.0, 0.0]),
        record("raw_100x_same_dir", "sources/b", [100.0, 0.0, 0.0]),
        record("unit_orthogonal", "sources/c", [0.0, 1.0, 0.0]),
    ])
    results = store.search_vector([1.0, 0.0, 0.0], k=3)
    by_id = {r["chunk_id"]: r["_distance"] for r in results}
    assert by_id["unit_same_dir"] == pytest.approx(by_id["raw_100x_same_dir"], abs=1e-4), (
        "under cosine distance, same-direction vectors must rank identically "
        "regardless of magnitude")
    assert by_id["unit_same_dir"] < by_id["unit_orthogonal"], (
        "the same-direction vectors must still outrank the orthogonal one")


def test_cosine_distance_preserves_ranking_for_already_normalized_vectors(tmp_path: Path):
    """For the common (verified l2) case -- everything already unit-length --
    cosine distance produces the SAME RANK ORDER as raw L2 would have
    (Red review: the raw _distance VALUE is not byte-identical -- for unit
    vectors, L2-squared = 2 x cosine-distance -- but rank order, which is
    all fusion/RRF ever consumes, is unaffected). Verified separately at
    realistic scale (1024-dim, 200 random unit vectors, matching the real
    corpus's embedding dimension): top-200 order byte-for-byte identical."""
    vectors = {
        "a": [0.6, 0.8, 0.0],
        "b": [0.0, 1.0, 0.0],
        "c": [-0.6, 0.8, 0.0],
    }
    store = VectorStore(tmp_path)
    store.upsert([record(k, f"sources/{k}", v) for k, v in vectors.items()])
    ranked = [r["chunk_id"] for r in store.search_vector([1.0, 0.0, 0.0], k=3)]
    # cos(theta) to query (1,0,0): a=(0.6,0.8,0)->0.6, b=(0,1,0,)->0.0,
    # c=(-0.6,0.8,0)->-0.6 -- true cosine order, independent of any raw-L2 quirk.
    assert ranked == ["a", "b", "c"]


def test_no_ann_index_exists_after_a_normal_write_path(tmp_path: Path):
    """Red review, 2026-08-20: cosine distance is forced for every LanceDB
    search. If the table ever gained an ANN vector index built with a
    DIFFERENT metric (e.g. metric='l2'), querying with .metric('cosine')
    could error or silently degrade recall -- the query metric and the
    index-build metric are not independent. This pins the CURRENT invariant
    that the write path never creates one (flat search only), so the #45 fix
    stays safe. If this ever needs to change, the metric coupling above must
    be re-verified at the same time."""
    store = VectorStore(tmp_path)
    store.upsert([record(f"c{i}", f"sources/{i}", [1.0, float(i), 0.0]) for i in range(20)])
    table = store._open_table()
    if table is None:  # pragma: no cover - SQLite fallback has no ANN concept
        return
    assert table.list_indices() == [], (
        "an ANN vector index now exists -- verify it matches the forced "
        "cosine metric before this passes, or querying may error/degrade")


def test_a_zero_vector_in_a_legacy_index_is_omitted_not_nan_poisoning(tmp_path: Path):
    """Red review, 2026-08-20: a VERIFIED index cannot contain a zero vector
    (CachedEmbedder's _l2_normalize refuses to store one -- ValueError at
    write time). An unverified_legacy index has no such guarantee; it could
    genuinely contain one from before that guard existed. Verified live:
    LanceDB's cosine metric does not NaN-poison top-k on a zero vector --
    it silently OMITS that row from results entirely (a real completeness
    gap, but not the "NaN sorts unpredictably and corrupts ranking" failure
    mode). This pins that behavior so a future LanceDB version change would
    be caught here, not discovered as a live surprise."""
    store = VectorStore(tmp_path)
    store.upsert([
        record("zero", "sources/zero", [0.0, 0.0, 0.0]),
        record("unit", "sources/unit", [1.0, 0.0, 0.0]),
    ])
    results = store.search_vector([1.0, 0.0, 0.0], k=5)
    ids = [r["chunk_id"] for r in results]
    assert "unit" in ids, "the real (nonzero) vector must still be found"
    assert all(not (r["_distance"] != r["_distance"]) for r in results), (
        "no NaN distance may reach the caller (NaN != NaN is how you detect it)")
