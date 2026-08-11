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


def test_heading_text_is_searchable(tmp_path: Path):
    """The chunker moves headings into heading_path, so indexing only `text` made
    document titles unsearchable -- a systemic recall hole, since the title is often
    the most information-dense line in a note. A query matching a document's title
    must find it."""
    index = BM25Index(tmp_path / "fts.sqlite")
    index.index([{
        "chunk_id": "c1", "doc_id": "d1",
        "heading_path": "Phase 2 action plan: frontmatter isolation pinning on all agent types",
        "text": "Unrelated body prose that shares no words with the query.",
    }])

    hits = [cid for cid, _ in index.search("frontmatter isolation pinning agent types", 5)]
    assert hits == ["c1"]


def test_body_still_searchable_when_heading_absent(tmp_path: Path):
    index = BM25Index(tmp_path / "fts.sqlite")
    index.index([{"chunk_id": "c1", "doc_id": "d1", "text": "quarantine after repeated lint failures"}])
    assert [cid for cid, _ in index.search("quarantine lint", 5)] == ["c1"]


def test_reindexing_a_chunk_replaces_it_rather_than_duplicating(tmp_path):
    """The batched DELETE must keep exact upsert semantics.

    chunks_fts declares chunk_id UNINDEXED, so the delete-before-insert has no
    index to use and costs a full scan. Doing it once per batch instead of once
    per row is only safe if replacement still happens for every id in the batch.
    """
    from alexandria.index.bm25 import BM25Index

    index = BM25Index(tmp_path / "fts.sqlite")
    first = [{"chunk_id": f"c{i}", "doc_id": "d", "text": f"alpha unique{i}",
              "tags": [], "entities": []} for i in range(5)]
    index.index(first)
    second = [{"chunk_id": f"c{i}", "doc_id": "d", "text": f"beta unique{i}",
               "tags": [], "entities": []} for i in range(5)]
    index.index(second)

    rows = index.connection.execute("SELECT count(*) FROM chunks_fts").fetchone()[0]
    assert rows == 5, f"reindex duplicated rows instead of replacing: {rows}"
    assert index.search("alpha", k=10) == [], "stale text survived the reindex"
    assert len(index.search("beta", k=10)) == 5


def test_append_only_skips_the_delete_scan_on_a_fresh_table(tmp_path):
    """On a rebuild, chunks_fts was just dropped -- there is nothing to delete.

    Deleting anyway means a full O(table) scan per flush for zero effect (chunk_id
    is UNINDEXED, so DELETE...WHERE has no index to use). append_only=True must
    skip the DELETE entirely, not just batch it.
    """
    from alexandria.index.bm25 import BM25Index

    index = BM25Index(tmp_path / "fts.sqlite")
    # sqlite3.Connection.execute is a read-only attribute, so it cannot be
    # monkeypatched; set_trace_callback is the supported way to observe every
    # statement the connection actually runs.
    statements: list[str] = []
    index.connection.set_trace_callback(statements.append)
    # "alpha {i}", not "alpha{i}": FTS5 tokenises alpha0 as a single token, so a
    # search for "alpha" would match nothing and the assertion below would fail
    # for a tokenisation reason rather than the behaviour under test.
    chunks = [{"chunk_id": f"c{i}", "doc_id": "d", "text": f"alpha {i}",
               "tags": [], "entities": []} for i in range(5)]
    index.index(chunks, append_only=True)
    index.connection.set_trace_callback(None)

    deletes = [s for s in statements if s.strip().upper().startswith("DELETE")]
    assert deletes == [], f"append_only issued a DELETE anyway: {deletes}"
    assert len(index.search("alpha", k=10)) == 5


def test_append_only_still_dedupes_within_the_same_call(tmp_path):
    """A duplicate chunk_id inside one append_only batch must not double-insert
    into chunks_fts (metadata's ON CONFLICT already handles chunk_metadata)."""
    from alexandria.index.bm25 import BM25Index

    index = BM25Index(tmp_path / "fts.sqlite")
    chunks = [{"chunk_id": "dup", "doc_id": "d", "text": "first version",
               "tags": [], "entities": []},
              {"chunk_id": "dup", "doc_id": "d", "text": "second version",
               "tags": [], "entities": []}]
    index.index(chunks, append_only=True)

    rows = index.connection.execute("SELECT count(*) FROM chunks_fts").fetchone()[0]
    assert rows == 1, f"append_only duplicate chunk_id produced {rows} fts rows, want 1"


def test_batch_delete_spans_more_ids_than_one_sqlite_statement_allows(tmp_path):
    """A batch larger than the parameter chunk must still fully replace."""
    from alexandria.index.bm25 import BM25Index

    index = BM25Index(tmp_path / "fts.sqlite")
    n = BM25Index._DELETE_CHUNK * 2 + 37
    make = lambda word: [{"chunk_id": f"c{i}", "doc_id": "d", "text": f"{word} u{i}",
                          "tags": [], "entities": []} for i in range(n)]
    index.index(make("alpha"))
    index.index(make("beta"))

    rows = index.connection.execute("SELECT count(*) FROM chunks_fts").fetchone()[0]
    assert rows == n, f"expected {n} rows after replacement, got {rows}"
    assert index.search("alpha", k=5) == []
