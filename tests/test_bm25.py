"""FTS5 lexical retrieval treats meaningful query terms as required and is safe."""

from pathlib import Path

from alexandria.index.bm25 import BM25Index


def chunk(chunk_id: str, text: str, **meta) -> dict:
    return {
        "chunk_id": chunk_id,
        "doc_id": meta.pop("doc_id", f"sources/{chunk_id}"),
        "text": text,
        "heading_path": "Heading",
        "type": meta.pop("type", "observation"),
        "project": meta.pop("project", None),
        "status": meta.pop("status", "active"),
        "source": meta.pop("source", "test"),
        "tags": meta.pop("tags", []),
        "entities": meta.pop("entities", []),
        "layer": meta.pop("layer", "sources"),
        "generated_at": meta.pop("generated_at", None),
    }


def test_bm25_requires_all_meaningful_terms(tmp_path: Path):
    index = BM25Index(tmp_path / "fts.sqlite")
    index.index([chunk("both", "sweep retries a page that fails lint"),
                 chunk("one", "sweep handles documents"),
                 chunk("other", "lint validates metadata")])

    assert [chunk_id for chunk_id, _ in index.search("sweep lint", 5)] == ["both"]


def test_bm25_escapes_fts_syntax_and_handles_stopword_only_queries(tmp_path: Path):
    index = BM25Index(tmp_path / "fts.sqlite")
    index.index([chunk("quoted", "literal star quote behavior")])

    assert index.search('" *', 5) == []
    assert index.search("the and of", 5) == []


def test_bm25_filters_before_limit(tmp_path: Path):
    index = BM25Index(tmp_path / "fts.sqlite")
    index.index([chunk("source", "retry lint", project="one"),
                 chunk("wiki", "retry lint page", project="two", layer="wiki"),
                 chunk("source-two", "retry lint failure", project="two")])

    assert {chunk_id for chunk_id, _ in index.search("retry lint", 2,
                                                      where={"project": "two"})} == {"wiki", "source-two"}
