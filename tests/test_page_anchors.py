"""#52 Phase 2: page anchors, openable pointers, and the persisted Chunk.meta.

Red's design verdict (2026-08-19) is the contract this file pins:
- `page` is an ANNOTATION computed from source offsets (form feeds), never an
  input to segmentation. Chunk text and chunk_ids must be byte-identical with
  and without the annotation, or the embedding cache orphans (the 5.39GB
  lesson) and already-indexed rows drift.
- Persist `Chunk.meta` as a JSON column via an ADDITIVE migration (the store
  was silently dropping `meta`/`ordinal`; this ends that class of bug).
- Backfill by re-running the chunker and upserting meta keyed by the EXISTING
  stable chunk_ids, gated on 100% chunk_id match and zero embedding calls.
- `page` = start page (where the cited text begins), null/absent for docs
  with no page structure; surfaced as its own field in the /search payload.
"""

import json
import sqlite3

import pytest

from alexandria import serve as serve_mod
from alexandria.config import AppConfig, load_config
from alexandria.index.chunker import chunk_doc_records, chunk_document
from alexandria.index.store import VectorStore


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _pager(words_per_page: int = 700, pages: int = 10) -> str:
    """A multi-page markdown body: \f separates pages (pdftotext -layout emits
    exactly one form feed per page boundary, verified on the live 10-page
    test paper: 9 form feeds for 10 pages)."""
    pages_text = []
    for p in range(1, pages + 1):
        body = " ".join(f"word{p}-{i}" for i in range(words_per_page))
        pages_text.append(f"# Page {p}\n\n{body}")
    return "\f".join(pages_text)


def _ingest_style_doc(corpus, name="paper.md", body=None):
    """A companion doc exactly as `alexandria ingest` writes one: ingest
    frontmatter (asset pointer, original name) + extracted text body."""
    d = corpus / "sources" / "assets" / name
    d.parent.mkdir(parents=True, exist_ok=True)
    body = body if body is not None else _pager(60, 2)
    d.write_text(
        "---\n"
        "type: doc\n"
        "source: ingest\n"
        "source_id: abc123def456\n"
        "ingest:\n"
        f"  original_name: {name}\n"
        "  original_path: /home/u/Downloads/paper.pdf\n"
        "  sha256: abc123def4567890\n"
        "  extraction: pdftotext\n"
        f"  asset: assets/ab/abc123def4567890.pdf\n"
        "  bytes: 12345\n"
        "  pages: 2\n"
        "---\n\n"
        + body)
    return d


def _index(corpus, monkeypatch):
    from alexandria.cli import app
    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    assert app(["--corpus", str(corpus), "index"]) == 0
    return VectorStore(corpus / ".alexandria" / "index")


# ---------------------------------------------------------------------------
# chunker: page annotation is additive and exact
# ---------------------------------------------------------------------------

def test_page_annotation_is_stable_and_covers_every_page():
    md = _pager()
    first = chunk_document("sources/x.md", md)
    second = chunk_document("sources/x.md", md)
    assert [c.chunk_id for c in first] == [c.chunk_id for c in second]
    assert len(first) == len(second)
    pages = [c.meta["page"] for c in first]
    assert pages == sorted(pages), "page must be monotonic along the document"
    assert pages[0] == 1 and pages[-1] == 10
    assert set(pages) == set(range(1, 11)), "every page must anchor at least one chunk"
    # the annotation must not have touched the text the cache key hashes
    assert [c.text for c in first] == [c.text for c in second]


def test_a_paragraph_crossing_a_page_break_reports_its_start_page():
    """A 1200-word paragraph with a form feed after word 599: the pieces that
    begin before the break anchor page 1, the pieces that begin after it
    anchor page 2 -- start-page semantics, exactly as Red specified."""
    pre = " ".join(f"f{i}" for i in range(300))
    words = [f"c{i}" for i in range(1200)]
    words.insert(600, "\f")
    crossing = " ".join(words)
    md = ("# Page 1\n\n" + pre + "\n\n" + crossing +
          "\n\nThis paragraph is entirely on page two.\n")
    chunks = chunk_document("sources/x.md", md)
    starts = {c.meta["page"] for c in chunks if "c0 " in c.text}
    assert starts == {1}, "the paragraph start must anchor page one"
    tail_pages = {c.meta["page"] for c in chunks if "c1100" in c.text}
    assert tail_pages == {2}, "pieces after the break must anchor page two"
    later = [c for c in chunks if "entirely on page two" in c.text]
    assert later and later[0].meta["page"] == 2


def test_a_standalone_form_feed_between_paragraphs_advances_the_page():
    """pdftotext can emit a form feed on its own line (a page that ends
    between paragraphs). _paragraphs() strips and drops that token; the page
    cursor must still count it, or everything after is off by one."""
    pre = " ".join(f"g{i}" for i in range(300))
    para_two = " ".join(f"t{i}" for i in range(600))
    md = ("# Page 1\n\n" + pre + "\n\nParagraph one on page one.\n\n"
          "\f\n\n" + para_two + "\n")
    chunks = chunk_document("sources/x.md", md)
    first_page_two = next(c for c in chunks if c.meta["page"] == 2)
    assert "Paragraph one" not in first_page_two.text
    assert "t" in first_page_two.text, "the first page-2 chunk must carry page-2 text"


def test_a_long_paragraph_split_across_a_page_break_places_tail_pieces_correctly():
    """_hard_split breaks an oversized paragraph on word boundaries, which
    drops the form feed from the pieces; the per-piece cursor must still move
    so the pieces after the break anchor to page two."""
    pre = " ".join(f"a{i}" for i in range(400))
    post = " ".join(f"b{i}" for i in range(800))
    md = f"# Page 1\n\n{pre}\f{post}\n\n# Page 2\n\nafterword.\n"
    chunks = chunk_document("sources/x.md", md)
    first_page_two = next(c for c in chunks if c.meta["page"] == 2)
    assert "b" in first_page_two.text and "a0 " not in first_page_two.text
    assert all("b700" not in c.text or c.meta["page"] == 2 for c in chunks)


def test_hard_split_with_ff_reproduces_hard_split_pieces_exactly():
    """Red review finding #1: the annotated splitter must be byte-equivalent
    to the plain splitter on EVERY input, or reindexing silently re-embeds.
    Property test over adversarial paragraphs: CRLF, unicode whitespace,
    standalone form feeds, over-budget unbreakable tokens, empty runs."""
    from alexandria.index.chunker import _hard_split, _hard_split_with_ff
    import random
    rng = random.Random(52)
    cases = [
        "plain words here",
        "a\fb",                       # mid-paragraph feed, short
        "\fa b\f",                   # edge feeds
        "a\n\f\nb",                 # feed as its own line, no blank lines
        "\n\f\n",                   # feed-only
        "one\n\r\ntwo",             # CRLF paragraph break
        "x\n\u00a0\ny",             # unicode nbsp between lines
        "word " * 600,                 # over budget, plain
        ("A" * 20000) + " tail",       # over-budget unbreakable token
        " ".join(f"w{i}" for i in range(400)) + "\f" + " ".join(f"v{i}" for i in range(400)),
    ]
    for _ in range(200):
        n = rng.randint(0, 800)
        body = " ".join(f"k{i}" for i in range(n))
        if rng.random() < 0.3:
            pos = rng.randrange(0, len(body) + 1)
            body = body[:pos] + "\f" + body[pos:]
        if rng.random() < 0.2:
            body = body.replace(" ", "\u00a0", rng.randrange(1, 4))
        cases.append(body)

    for case in cases:
        for max_tokens in (64, 512):
            plain = _hard_split(case, max_tokens)
            annotated = _hard_split_with_ff(case, max_tokens)
            assert [p for p, _ in annotated] == plain, (
                f"piece drift at max_tokens={max_tokens}: {case[:60]!r}")
            # ff_before[start] is a page OFFSET, not a partition of the
            # paragraph's feeds (paragraph-level conservation happens in
            # chunk_document via para_ff). The meaningful invariants:
            # (a) offsets are non-decreasing along the piece stream,
            # (b) the first piece's offset = feeds in the leading whitespace,
            # (c) no offset exceeds the paragraph's total feeds.
            ffs = [ff for _, ff in annotated]
            if not ffs:
                continue  # feed-only paragraph: no words, no pieces
            assert ffs == sorted(ffs), f"non-monotonic offsets: {case[:60]!r}"
            leading = __import__("re").match(r"\s*", case).group().count("\f")
            assert ffs[0] == leading, f"leading feeds mis-counted: {case[:60]!r}"
            assert max(ffs) <= case.count("\f"), f"offset exceeds total: {case[:60]!r}"


def test_a_feed_glued_to_a_heading_line_still_advances_the_page():
    """Red review seam: '\f# Chapter 2' -- the dominant layout in chaptered
    documents, where a page break coincides with a heading. splitlines detaches
    the feed from the heading line; the sectioning flush must keep it."""
    pre = " ".join(f"h{i}" for i in range(300))
    post = " ".join(f"p{i}" for i in range(600))
    md = ("# Chapter 1\n\n" + pre + "\n\n"
          "\f# Chapter 2\n\n" + post + "\n")
    chunks = chunk_document("sources/x.md", md)
    two = [c for c in chunks if "p0 " in c.text]
    assert two and two[0].meta["page"] == 2
    # the feed section produced no units and no heading drift
    assert any("Chapter 2" in c.heading_paths for c in chunks)


def test_a_leading_form_feed_means_the_document_starts_on_page_two():
    """A document that genuinely starts on page two: the very first character
    is a form feed. split_headings' leading buffer is whitespace-only; it must
    not be dropped (conservation would also catch it)."""
    md = "\f# Title\n\nFirst paragraph is on page two.\n"
    chunks = chunk_document("sources/x.md", md)
    first = chunks[0]
    assert first.meta["page"] == 2


def test_page_cursor_conservation_detects_a_dropped_feed():
    """Red review invariant: the final cursor must equal the document's feed
    count (minus frontmatter). It is a SENTINEL -- unreachable while the seam
    holds, so simulate the seam regressing (a sectioning flush that drops
    feed-carrying buffers, the pre-fix behavior) and assert it fires."""
    import alexandria.index.chunker as ch

    orig_split = ch.split_headings

    def leaky(markdown):
        # Simulate the pre-fix seam: form feeds never reach section text.
        return [ch.Section(s.heading_path, s.text.replace("\f", ""))
                for s in orig_split(markdown)]

    ch.split_headings = leaky
    try:
        with pytest.raises(ValueError, match="conservation"):
            ch.chunk_document(
                "sources/x.md",
                "# A\n\nbody text\f# B\n\nmore body\n")
    finally:
        ch.split_headings = orig_split


def test_blank_pages_conserve_feed_multiplicity():
    """Red review: a blank page is TWO form feeds ('\f\n\f'), not one. The
    cursor must count both -- presence is not conserved, COUNT is."""
    pre = " ".join(f"q{i}" for i in range(300))
    post = " ".join(f"r{i}" for i in range(600))
    md = ("# A\n\n" + pre + "\n\n"
          "\f\n\f\n\n"           # one blank page = two feeds
          "# C\n\n" + post + "\n")
    chunks = chunk_document("sources/x.md", md)
    three = [c for c in chunks if "r0 " in c.text]
    assert three and three[0].meta["page"] == 3


def test_a_body_that_merely_mentions_ingest_is_not_annotated_as_an_asset(tmp_path):
    """Red review: the backfill pre-filter must scope 'ingest:' to the
    frontmatter, not the whole body -- a note ABOUT this system mentions the
    word and must not be treated as an ingest artifact."""
    from alexandria.index.chunker import _frontmatter_has_ingest
    assert _frontmatter_has_ingest("---\ningest:\n  asset: x\n---\n\nbody\n") is True
    assert _frontmatter_has_ingest("---\ntitle: t\n---\n\nWe discussed ingest: here.\n") is False
    assert _frontmatter_has_ingest("no frontmatter, but ingest: appears\n") is False


def test_plain_markdown_has_no_page_annotation():
    chunks = chunk_document("sources/note.md", "# Note\n\nJust a note with no pages.\n")
    assert all("page" not in c.meta for c in chunks)


# ---------------------------------------------------------------------------
# store: additive meta column, both backends
# ---------------------------------------------------------------------------

def test_old_sqlite_schema_gains_meta_column_without_losing_rows(tmp_path):
    from alexandria.index.store import _SQLiteVectorStore
    db = tmp_path / "fallback.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE chunks ("
                "chunk_id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, text TEXT NOT NULL, "
                "heading_path TEXT NOT NULL, vector TEXT NOT NULL, type TEXT, project TEXT, "
                "status TEXT, source TEXT, tags TEXT NOT NULL, entities TEXT NOT NULL, "
                "layer TEXT NOT NULL, generated_at TEXT, enrichment TEXT, kind TEXT, "
                "parent_doc TEXT, target_chunk TEXT, deleted TEXT NOT NULL DEFAULT 'false')")
    con.execute("INSERT INTO chunks (chunk_id, doc_id, text, heading_path, vector, tags, entities, layer, deleted) "
                "VALUES ('sources/a#1', 'sources/a', 'old text', '', '[0.1]', '[]', '[]', 'sources', 'false')")
    con.commit(); con.close()

    store = _SQLiteVectorStore(db)
    row = store.get("sources/a#1")
    assert row is not None and row["text"] == "old text"
    assert row["meta"] == {}, "pre-migration rows must read back as an empty annotation"

    store.upsert([{"chunk_id": "sources/a#2", "doc_id": "sources/a", "text": "new",
                   "heading_path": "", "vector": [0.2], "type": None, "project": None,
                   "status": None, "source": None, "tags": [], "entities": [],
                   "layer": "sources", "generated_at": None, "enrichment": None,
                   "kind": None, "parent_doc": None, "target_chunk": None,
                   "deleted": "false", "meta": {"page": 3}}])
    assert store.get("sources/a#2")["meta"] == {"page": 3}


def test_old_lance_schema_gains_meta_column_without_losing_rows(tmp_path):
    """Migration is a WRITE-path concern: reads must work on a pre-#52 table
    untouched (no write on the read path), and the first write adds the column
    in place without losing rows (Red review finding, 2026-08-19)."""
    lancedb = pytest.importorskip("lancedb")
    import pyarrow as pa
    index_dir = tmp_path / "index"
    con = lancedb.connect(str(index_dir))
    schema = pa.schema([
        pa.field("chunk_id", pa.string()), pa.field("doc_id", pa.string()),
        pa.field("text", pa.string()), pa.field("heading_path", pa.string()),
        pa.field("vector", pa.list_(pa.float32())),
        pa.field("deleted", pa.string())])
    con.create_table("chunks", data=[{
        "chunk_id": "sources/a#1", "doc_id": "sources/a", "text": "old",
        "heading_path": "", "vector": [0.1], "deleted": "false"}], schema=schema)

    store = VectorStore(index_dir)
    # READ first: must work and must not migrate the table.
    row = store.get("sources/a#1")
    assert row is not None and row["text"] == "old"
    assert row["meta"] == {}
    assert "meta" not in {f.name for f in store._open_table().schema}

    # WRITE: the first write adds the column in place; the row survives with
    # its vector intact.
    assert store.update_meta({"sources/a#1": {"page": 4}}) == 1
    assert "meta" in {f.name for f in store._open_table().schema}
    row = store.get("sources/a#1")
    assert row["meta"] == {"page": 4}
    assert row["vector"] == pytest.approx([0.1]) and row["text"] == "old"


# ---------------------------------------------------------------------------
# records + backfill
# ---------------------------------------------------------------------------

def test_chunk_records_carry_page_asset_and_original_name(tmp_path):
    corpus = tmp_path / "corpus"
    (corpus / "sources").mkdir(parents=True)
    doc = _ingest_style_doc(corpus)
    config = AppConfig(corpus_path=corpus)
    records, err = chunk_doc_records(doc, corpus, config)
    assert err is None
    assert records and "meta" in records[0]
    m = records[0]["meta"]
    assert m["page"] == 1
    assert m["asset"] == "assets/ab/abc123def4567890.pdf"
    assert m["original_name"] == "paper.md"


def test_plain_documents_get_an_empty_meta(tmp_path):
    corpus = tmp_path / "corpus"
    d = corpus / "sources" / "note.md"
    d.parent.mkdir(parents=True)
    d.write_text("---\nproject: x\n---\n\nJust a note.\n")
    records, _ = chunk_doc_records(d, corpus, AppConfig(corpus_path=corpus))
    assert records and records[0]["meta"] == {}


def test_backfill_meta_updates_only_meta_and_never_embeds(tmp_path, monkeypatch):
    corpus = tmp_path / "corpus"
    (corpus / "sources").mkdir(parents=True)
    doc = _ingest_style_doc(corpus, body=_pager(120, 3))
    (corpus / "sources" / "plain.md").write_text("---\nproject: x\n---\n\nA plain note.\n")
    store = _index(corpus, monkeypatch)

    from alexandria.index.chunker import backfill_meta
    before = {r["chunk_id"]: (r["text"], r["vector"]) for r in
              [store.get(cid) for cid in
               [c["chunk_id"] for c in chunk_doc_records(doc, corpus, AppConfig(corpus_path=corpus))[0]]]}
    # simulate rows indexed before the meta column carried annotations
    assert store.update_meta({cid: {} for cid in before}) == len(before)

    # backfill takes NO embedder: there is nothing to call, by construction.
    stats = backfill_meta(corpus, store, AppConfig(corpus_path=corpus))
    assert stats.docs > 0 and stats.chunks_updated > 0

    for cid, (text, vector) in before.items():
        row = store.get(cid)
        assert row["text"] == text and row["vector"] == vector, "backfill must not rewrite text or vectors"
        assert row["meta"], f"{cid} should have gained an annotation"

    # plain docs were annotated too (they legitimately get {} -- no page, no asset)
    plain = [c["chunk_id"] for c in
             chunk_doc_records(corpus / "sources" / "plain.md", corpus, AppConfig(corpus_path=corpus))[0]]
    for cid in plain:
        assert store.get(cid)["meta"] == {}


def test_backfill_aborts_on_chunk_id_mismatch(tmp_path, monkeypatch):
    corpus = tmp_path / "corpus"
    (corpus / "sources").mkdir(parents=True)
    doc = _ingest_style_doc(corpus)
    store = _index(corpus, monkeypatch)
    # corrupt the DOC ON DISK: re-chunking must then produce chunk_ids that do
    # not exist, and the gate must refuse to half-annotate. (Backend-agnostic:
    # the real drift case is index-vs-disk divergence, not a hand-tampered row.)
    doc.write_text(doc.read_text() + "\n\nNEW WORDS that were never indexed.\n")

    from alexandria.index.chunker import backfill_meta
    with pytest.raises(RuntimeError, match="chunk_id"):
        backfill_meta(corpus, store, AppConfig(corpus_path=corpus))


# ---------------------------------------------------------------------------
# serve surface + soft delete + health
# ---------------------------------------------------------------------------

def test_search_results_and_payload_surface_page_and_asset(tmp_path, monkeypatch):
    from alexandria.cli import app
    from alexandria.index.store import VectorStore as VS
    from alexandria.retrieval.search import SearchEngine
    corpus = tmp_path / "corpus"
    (corpus / "sources").mkdir(parents=True)
    doc = _ingest_style_doc(corpus, body=_pager(80, 3))
    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    assert app(["--corpus", str(corpus), "index"]) == 0

    ctx, tcp_server, uds_servers = serve_mod.bind(
        corpus, config=load_config(corpus_override=corpus), host="127.0.0.1", port=0)
    addr = tcp_server.server_address
    import threading
    t = threading.Thread(target=tcp_server.serve_forever, daemon=True)
    t.start()
    try:
        # The expected page is computed by the SAME chunker in THIS process:
        # CI has no tiktoken (it is not a dependency), so token boundaries --
        # and therefore chunk start pages -- legitimately differ between
        # environments. The invariant under test is that the served payload
        # carries the chunker's own annotation, not a hard-coded page.
        from alexandria.index.chunker import chunk_document
        md = doc.read_text()
        target = next(c for c in chunk_document("sources/assets/paper.md", md)
                      if "word3-10" in c.text)
        expected_page = target.meta["page"]
        import http.client
        conn = http.client.HTTPConnection(*addr, timeout=60)
        conn.request("POST", "/search", json.dumps({"query": "word3-10", "k": 5}),
                     {"Content-Type": "application/json"})
        resp = conn.getresponse(); body = json.loads(resp.read())
        conn.close()
        assert resp.status == 200, body
        hits = [r for r in body["results"] if "word3-10" in r["text"]]
        assert hits, body
        assert hits[0]["page"] == expected_page, (hits[0]["page"], expected_page)
        assert hits[0]["asset"] == "assets/ab/abc123def4567890.pdf"
    finally:
        tcp_server.shutdown(); tcp_server.server_close()


def test_delete_tombstones_the_companion_but_keeps_the_asset(tmp_path, monkeypatch):
    from alexandria.cli import app
    from alexandria.retrieval.search import SearchEngine
    import yaml
    corpus = tmp_path / "corpus"
    (corpus / "sources").mkdir(parents=True)
    doc = _ingest_style_doc(corpus)
    asset = corpus / "assets" / "ab" / "abc123def4567890.pdf"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    assert app(["--corpus", str(corpus), "index"]) == 0
    # sanity: it was searchable before the delete
    store = VectorStore(corpus / ".alexandria" / "index")
    assert store.count() > 0

    assert app(["--corpus", str(corpus), "delete", "sources/assets/paper.md"]) == 0
    fm = yaml.safe_load(doc.read_text().split("---")[1])
    assert fm["deleted"] is True, "the companion must be tombstoned"
    assert asset.exists(), "the binary is the memory: delete must leave it recoverable"
    # tombstoned chunks must be gone from search and stay gone after a reindex
    def searchable():
        ids = list(store.chunk_ids())
        return sum(1 for rec in store.get_many(ids).values()
                   if rec.get("deleted") == "false")
    assert searchable() == 0
    assert app(["--corpus", str(corpus), "index"]) == 0
    assert searchable() == 0, "a reindex must not resurrect a tombstoned ingest doc"


def test_assets_directory_does_not_create_a_phantom_shortfall(tmp_path):
    corpus = tmp_path / "corpus"
    (corpus / "sources" / "assets").mkdir(parents=True)
    (corpus / "assets" / "ab").mkdir(parents=True)
    _ingest_style_doc(corpus)
    (corpus / "assets" / "ab" / "abc123def4567890.pdf").write_bytes(b"%PDF fake")
    (corpus / "assets" / "ab" / "abc123def4567890.png").write_bytes(b"png")
    count = serve_mod._source_document_count(corpus)
    assert count == 1, "assets/ binaries are not source documents"
