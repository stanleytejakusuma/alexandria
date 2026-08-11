"""The vector store retains all chunk metadata and filters before limiting results."""

from pathlib import Path

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
