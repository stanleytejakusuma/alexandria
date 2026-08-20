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

def test_default_metric_is_scale_sensitive_to_unnormalized_vectors(tmp_path: Path):
    """Pins the FAILURE MODE this fix closes -- without force_cosine_metric,
    an unnormalized (raw) vector at the SAME direction as the query ranks
    WORSE than an orthogonal unit vector. If this test ever fails, LanceDB's
    own default metric changed and the #45 fix may no longer be necessary
    (or may need re-verification)."""
    store = VectorStore(tmp_path)
    store.upsert([
        record("unit_same_dir", "sources/a", [1.0, 0.0, 0.0]),
        record("raw_100x_same_dir", "sources/b", [100.0, 0.0, 0.0]),
        record("unit_orthogonal", "sources/c", [0.0, 1.0, 0.0]),
    ])
    results = store.search_vector([1.0, 0.0, 0.0], k=3)
    ranked = [r["chunk_id"] for r in results]
    assert ranked[0] == "unit_same_dir"
    # THE BUG: raw_100x_same_dir (same direction, just unnormalized) ranks
    # BEHIND the orthogonal vector under raw L2 distance.
    assert ranked.index("raw_100x_same_dir") > ranked.index("unit_orthogonal"), (
        "if this assertion fails, LanceDB's default metric is no longer "
        "scale-sensitive and this whole fix should be re-examined")


def test_force_cosine_metric_ranks_same_direction_vectors_together_regardless_of_scale(tmp_path: Path):
    """THE FIX: with force_cosine_metric=True, a same-direction vector ranks
    correctly (tied with the unit vector) regardless of its magnitude --
    exactly true cosine similarity, safe to use against an index whose
    normalization cannot be proven."""
    store = VectorStore(tmp_path, force_cosine_metric=True)
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


def test_force_cosine_metric_is_a_pure_noop_for_already_normalized_vectors(tmp_path: Path):
    """For the DEFAULT (verified l2) case -- everything already unit-length
    -- forcing cosine distance must produce the SAME ranking as the default
    metric, so opting into it never changes behavior for the common path."""
    vectors = {
        "a": [0.6, 0.8, 0.0],
        "b": [0.0, 1.0, 0.0],
        "c": [-0.6, 0.8, 0.0],
    }
    query = [1.0, 0.0, 0.0]

    default_store = VectorStore(tmp_path / "default")
    default_store.upsert([record(k, f"sources/{k}", v) for k, v in vectors.items()])
    default_ranked = [r["chunk_id"] for r in default_store.search_vector(query, k=3)]

    cosine_store = VectorStore(tmp_path / "cosine", force_cosine_metric=True)
    cosine_store.upsert([record(k, f"sources/{k}", v) for k, v in vectors.items()])
    cosine_ranked = [r["chunk_id"] for r in cosine_store.search_vector(query, k=3)]

    assert default_ranked == cosine_ranked
