"""FTS5 lexical retrieval treats meaningful query terms as required and is safe."""

from pathlib import Path

from alexandria.index.bm25 import BM25Index, fts_query


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


def test_bm25_ranks_fuller_matches_first_without_excluding_partial_ones(tmp_path: Path):
    """Originally asserted ONLY the all-terms document may return. That exclusivity
    was the bug: it made BM25 a filter rather than a ranker, so one incidental word
    in a natural question ('anything', 'new') dropped a perfectly-titled document
    below rank 200 on the real corpus. The real requirement is ORDERING -- the
    document matching both terms must come first -- not suppression of the rest."""
    index = BM25Index(tmp_path / "fts.sqlite")
    index.index([chunk("both", "sweep retries a page that fails lint"),
                 chunk("one", "sweep handles documents"),
                 chunk("other", "lint validates metadata")])

    ranked = [chunk_id for chunk_id, _ in index.search("sweep lint", 5)]
    assert ranked[0] == "both"
    assert set(ranked) >= {"both", "one", "other"}


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


def test_query_terms_are_optional_not_mandatory():
    """AND made BM25 a filter, not a ranker: one incidental word in a natural
    question ('anything', 'new') dropped a perfectly-titled document below rank 200.
    FTS5's bm25() already rewards matching more terms, so OR is the correct join."""
    expression = fts_query("consult memory before building anything new")
    assert " OR " in expression
    assert " AND " not in expression


def test_a_missing_term_does_not_eliminate_an_otherwise_strong_match(tmp_path):
    index = BM25Index(tmp_path / "fts.sqlite")
    index.index([
        {"chunk_id": "target", "doc_id": "d1",
         "text": "consult memory before building on capital-adjacent infra"},
        {"chunk_id": "other", "doc_id": "d2", "text": "totally unrelated content"},
    ])
    # 'anything' and 'new' appear in neither document
    hits = [chunk_id for chunk_id, _ in index.search(
        "consult memory before building anything new", 5, None)]
    assert "target" in hits


def test_more_matching_terms_still_ranks_higher():
    """OR must not flatten ranking -- bm25() should still prefer the fuller match."""
    import tempfile
    from pathlib import Path
    index = BM25Index(Path(tempfile.mkdtemp()) / "fts.sqlite")
    index.index([
        {"chunk_id": "strong", "doc_id": "d1", "text": "alpha beta gamma delta"},
        {"chunk_id": "weak", "doc_id": "d2", "text": "alpha unrelated words here"},
    ])
    ranked = [chunk_id for chunk_id, _ in index.search("alpha beta gamma", 5, None)]
    assert ranked[0] == "strong"
