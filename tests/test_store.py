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
