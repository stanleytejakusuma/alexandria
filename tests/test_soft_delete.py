"""Soft-delete: a `deleted` flag that is genuinely enforced by retrieval, not
just recorded in frontmatter.

SPEC-data-model-and-ambient-capture.md §D4a names the failure mode this whole
file exists to prevent: `deleted` living ONLY in document frontmatter looks
like deletion (an operator can see it in the file) but is worthless, because
`store.upsert`'s field projection (`_normalise_record` keeps only
`ALL_FIELDS`) silently drops any key that isn't declared in
`index/store.py:SCALAR_FIELDS` / `index/bm25.py:METADATA_COLUMNS` before the
row is ever written -- so the chunk stays fully retrievable forever. Every
test in the first two sections below would fail if that regressed.

Design recap (see index/filtering.py for the authoritative comments):
- WRITE time (`deleted_flag`): missing/unrecognised -> "false" (visible).
  Correct because the overwhelming majority of chunk records never touch
  deletion at all, so defaulting them to hidden would be its own regression.
- READ time (`not_deleted_clause`): allow-list `deleted = 'false'`, not a
  negated deny-list. A row whose column is NULL, corrupted, or written before
  this column existed is EXCLUDED, not shown -- "fail closed" per ARC-BRIEF.
- Durability: `deleted` is a frontmatter/document property re-derived by
  `chunker.doc_frontmatter_metadata` on every reindex, exactly like `type` or
  `project` -- never index-side-only state -- so a `--rebuild` cannot
  resurrect a tombstoned document.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from alexandria.cli import app
from alexandria.corpus import Doc, render
from alexandria.config import load_config
from alexandria.index.bm25 import METADATA_COLUMNS, BM25Index, searchable_text
from alexandria.index.chunker import chunk_doc_records, doc_frontmatter_metadata
from alexandria.index.embedder import HashEmbedder
from alexandria.index.filtering import deleted_flag, not_deleted_clause
from alexandria.index.store import SCALAR_FIELDS, VectorStore
from alexandria.retrieval.rerank import IdentityReranker
from alexandria.retrieval.search import SearchConfig, SearchEngine

requires_git_filter_repo = pytest.mark.skipif(
    shutil.which("git-filter-repo") is None,
    reason="git-filter-repo not installed -- required for `alexandria erase`'s "
           "actual history rewrite")


# ---------------------------------------------------------------------------
# The §D4a blocker, directly: the exact code locations the spec names.
# ---------------------------------------------------------------------------

def test_deleted_is_an_indexed_column_not_frontmatter_only():
    """Cheapest possible regression guard for SPEC §D4a's blocker: catches a
    revert before any end-to-end behaviour test would even run. Kept alongside
    the end-to-end tests below, not instead of them -- declaration alone
    doesn't prove the filter is enforced (see the "one leg" test further down
    for that).

    The two legs declare the column differently, and asserting the wrong shape
    was itself a bug in this test:
      - dense: `deleted` must be in SCALAR_FIELDS, because store.py projects
        records through that tuple and silently drops anything absent from it
        (that projection IS the §D4a blocker).
      - lexical: bm25.py has no such projection. Its chunk_metadata DDL and
        INSERT name every column explicitly, so `deleted` is declared there
        instead. METADATA_COLUMNS is the *user-facing filter whitelist*, and
        `deleted` is deliberately NOT a member: it is enforced unconditionally
        by not_deleted_clause on every query, not opt-in per request. Adding it
        there would let a caller filter ON it, which is not the requirement.

    So the real invariant for the lexical leg is that the column is persisted
    and unconditionally filtered -- asserted here against the schema itself.
    """
    assert "deleted" in SCALAR_FIELDS

    with tempfile.TemporaryDirectory() as tmp:
        index = BM25Index(Path(tmp) / "bm25.sqlite")
        cols = {row[1] for row in index.connection.execute("PRAGMA table_info(chunk_metadata)")}
        assert "deleted" in cols, "lexical leg must persist `deleted` as a real column"

    assert "deleted" not in METADATA_COLUMNS, (
        "`deleted` is enforced unconditionally, not exposed as an opt-in user filter"
    )


# ---------------------------------------------------------------------------
# index/filtering.py: the two normalisation/predicate helpers directly.
# ---------------------------------------------------------------------------

def test_deleted_flag_write_time_normalisation():
    assert deleted_flag(True) == "true"
    assert deleted_flag(False) == "false"
    assert deleted_flag(None) == "false"          # never touched deletion -> visible
    assert deleted_flag("true") == "true"
    assert deleted_flag("TRUE") == "true"
    # Round-tripped stored string, not Python truthiness -- "false" the
    # string must not flip to "true" just because non-empty strings are
    # truthy in Python. This is what makes it safe to re-upsert a record
    # fetched back out of the store (get/get_many return the stored string)
    # without it silently becoming deleted.
    assert deleted_flag("false") == "false"
    # Anything else at write time defaults permissively (see module docstring).
    assert deleted_flag("garbage") == "false"


def test_not_deleted_clause_is_an_allow_list():
    assert not_deleted_clause() == "deleted = 'false'"
    assert not_deleted_clause("m") == "m.deleted = 'false'"


# ---------------------------------------------------------------------------
# index/chunker.py: frontmatter -> chunk metadata, strict bool identity.
# ---------------------------------------------------------------------------

def test_doc_frontmatter_metadata_deleted_defaults_to_not_deleted():
    assert doc_frontmatter_metadata({}, "sources/a")["deleted"] is False


def test_doc_frontmatter_metadata_deleted_is_strict_bool_identity():
    assert doc_frontmatter_metadata({"deleted": True}, "sources/a")["deleted"] is True
    assert doc_frontmatter_metadata({"deleted": False}, "sources/a")["deleted"] is False
    # A hand-typed quoted string is truthy in Python (bool("false") is True)
    # but must not tombstone a document by typo -- only the exact Python
    # bool True (which is all the CLI ever writes) counts as deleted.
    assert doc_frontmatter_metadata({"deleted": "false"}, "sources/a")["deleted"] is False
    assert doc_frontmatter_metadata({"deleted": "true"}, "sources/a")["deleted"] is False


# ---------------------------------------------------------------------------
# index/store.py: dense retrieval excludes deleted chunks.
# ---------------------------------------------------------------------------

def record(chunk_id: str, doc_id: str, vector: list[float], *, deleted=None, **meta) -> dict:
    row = {
        "chunk_id": chunk_id, "doc_id": doc_id,
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
    if deleted is not None:
        row["deleted"] = deleted
    return row


def test_dense_search_excludes_deleted_chunks(tmp_path: Path):
    store = VectorStore(tmp_path / "index")
    store.upsert([
        record("visible", "sources/a", [1.0, 0.0]),
        record("gone", "sources/b", [1.0, 0.0], deleted=True),
    ])

    results = store.search_vector([1.0, 0.0], k=5)

    assert [r["chunk_id"] for r in results] == ["visible"]
    # get()/get_many() stay UNFILTERED on purpose -- admin resolution (e.g.
    # `alexandria delete` reusing an already-indexed vector to undelete a
    # document) must still be able to see a deleted row by id.
    assert store.get("gone") is not None
    assert store.get("gone")["deleted"] == "true"


def test_dense_search_omits_a_record_that_would_have_pushed_out_a_result(tmp_path: Path):
    """prefilter=True / the WHERE-clause form matters, not just a post-hoc
    filter: a deleted row must never occupy one of the k slots in the first
    place, or a small k could come back short even though a real k-th match
    exists."""
    store = VectorStore(tmp_path / "index")
    store.upsert([
        record("best", "sources/a", [1.0, 0.0], deleted=True),
        record("second", "sources/b", [0.9, 0.1]),
        record("third", "sources/c", [0.8, 0.2]),
    ])

    results = store.search_vector([1.0, 0.0], k=2)

    assert [r["chunk_id"] for r in results] == ["second", "third"]


# ---------------------------------------------------------------------------
# index/bm25.py: lexical retrieval excludes deleted chunks.
# ---------------------------------------------------------------------------

def bm25_chunk(chunk_id: str, text: str, *, deleted=None, doc_id: str | None = None) -> dict:
    row = {"chunk_id": chunk_id, "doc_id": doc_id or f"sources/{chunk_id}", "text": text}
    if deleted is not None:
        row["deleted"] = deleted
    return row


def test_lexical_search_excludes_deleted_chunks(tmp_path: Path):
    lexical = BM25Index(tmp_path / "fts.sqlite")
    lexical.index([
        bm25_chunk("visible", "sweep retries a page that fails lint"),
        bm25_chunk("gone", "sweep retries a page that fails lint", deleted=True),
    ])

    hits = [chunk_id for chunk_id, _ in lexical.search("sweep retries lint", k=5)]

    assert hits == ["visible"]


# ---------------------------------------------------------------------------
# retrieval/search.py: hybrid engine, both legs -- "a filter applied on one
# leg and not the other is a hole" (ARC-BRIEF, verbatim).
# ---------------------------------------------------------------------------

def test_hybrid_search_excludes_deleted_through_both_retrieval_legs(tmp_path: Path):
    embedder = HashEmbedder(dim=24)
    texts = ["sweep page fails lint", "sweep page retry lint", "unrelated notes"]
    vectors = embedder.embed(texts)
    rows = [
        record("sources/a", "sources/a", vectors[0], text=texts[0]),
        record("sources/b", "sources/b", vectors[1], text=texts[1], deleted=True),
        record("sources/c", "sources/c", vectors[2], text=texts[2]),
    ]
    store = VectorStore(tmp_path / "index")
    store.upsert(rows)
    lexical = BM25Index(tmp_path / "fts.sqlite")
    lexical.index(rows)
    engine = SearchEngine(embedder, store, lexical, IdentityReranker(),
                          SearchConfig(prefetch=5, top_k=5, wiki_boost=1.25))

    # Each retrieval leg independently, BEFORE fusion -- proves the exclusion
    # happens at the source, not as a downstream side effect of get_many()
    # dropping an id nobody separately filtered.
    dense_ids = {row["chunk_id"] for row in store.search_vector(vectors[1], k=5)}
    lexical_ids = {chunk_id for chunk_id, _ in lexical.search("sweep page lint", k=5)}
    assert "sources/b" not in dense_ids
    assert "sources/b" not in lexical_ids

    results = engine.search("sweep page lint")
    assert "sources/b" not in {r.chunk_id for r in results}
    assert "sources/a" in {r.chunk_id for r in results}


# ---------------------------------------------------------------------------
# Fail-closed: a `deleted` value this feature's own write path never
# produces (legacy data, corruption, a future third state) must be excluded,
# not shown. Written directly against the underlying table, bypassing
# upsert()/_normalise_record() entirely -- that is the only way to get an
# unrecognised value in, since the public write path always normalises
# through deleted_flag().
# ---------------------------------------------------------------------------

def _write_raw_deleted(store: VectorStore, chunk_id: str, value) -> None:
    if store._fallback is not None:
        store._fallback.connection.execute(
            "UPDATE chunks SET deleted = ? WHERE chunk_id = ?", (value, chunk_id))
        store._fallback.connection.commit()
    else:  # real LanceDB backend (installed in this env; see AGENTS.md note)
        table = store._open_table()
        table.update(where=f"chunk_id = '{chunk_id}'", values={"deleted": value})


def test_dense_search_fails_closed_on_an_unrecognised_deleted_value(tmp_path: Path):
    store = VectorStore(tmp_path / "index")
    store.upsert([record("row", "sources/a", [1.0, 0.0])])
    _write_raw_deleted(store, "row", "maybe")  # neither "true" nor "false"

    assert store.search_vector([1.0, 0.0], k=5) == []
    assert store.get("row") is not None  # still resolvable by id for repair


def test_dense_search_fails_closed_on_a_null_deleted_value(tmp_path: Path):
    store = VectorStore(tmp_path / "index")
    store.upsert([record("row", "sources/a", [1.0, 0.0])])
    _write_raw_deleted(store, "row", None)

    assert store.search_vector([1.0, 0.0], k=5) == []


def test_lexical_search_fails_closed_on_an_unrecognised_deleted_value(tmp_path: Path):
    lexical = BM25Index(tmp_path / "fts.sqlite")
    lexical.index([bm25_chunk("row", "sweep retries lint")])
    lexical.connection.execute("UPDATE chunk_metadata SET deleted = 'maybe' WHERE chunk_id = 'row'")
    lexical.connection.commit()

    assert lexical.search("sweep retries lint", k=5) == []


# ---------------------------------------------------------------------------
# Durability across reindex: the most likely real-world regression named in
# ARC-BRIEF. `deleted` must be re-derived from frontmatter, not index state.
# ---------------------------------------------------------------------------

def test_deleted_flag_survives_a_full_reindex_from_frontmatter(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    corpus = tmp_path / "corpus"
    note = corpus / "sources" / "n.md"
    note.parent.mkdir(parents=True)
    note.write_text(render({"type": "observation", "title": "T", "deleted": True},
                            "Body about sweep retries and lint failures.\n"),
                    encoding="utf-8")
    config = load_config(corpus_override=str(corpus))

    records, errors = chunk_doc_records(note, corpus, config)
    assert not errors
    assert records and all(r["deleted"] is True for r in records)

    embedder = HashEmbedder(dim=16)
    vectors = embedder.embed([r["text"] for r in records])
    for r, v in zip(records, vectors, strict=True):
        r["vector"] = v
    store = VectorStore(corpus / ".alexandria" / "index")
    lexical = BM25Index(corpus / ".alexandria" / "index" / "fts.sqlite")

    # A FULL rebuild: drop both indexes, then rebuild purely from what
    # chunk_doc_records derives off disk -- exactly what `alexandria index
    # --rebuild` does. If `deleted` lived in any index-side state (never
    # touched here), this sequence would lose it; it doesn't, because
    # doc_frontmatter_metadata re-derives it fresh on every call.
    store.drop()
    lexical.drop()
    store.append(records)
    lexical.index(records, append_only=True)

    assert store.search_vector(vectors[0], k=5) == []
    assert lexical.search(searchable_text(records[0]), k=5) == []


def test_cli_delete_survives_index_rebuild(tmp_path: Path, monkeypatch, capsys):
    """End to end through the real CLI: index, confirm visible, delete,
    confirm hidden, `index --rebuild`, confirm STILL hidden. This is the
    exact regression ARC-BRIEF calls out as most likely."""
    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    corpus = tmp_path / "corpus"
    note = corpus / "sources" / "n.md"
    note.parent.mkdir(parents=True)
    note.write_text(
        "---\ntype: observation\ntitle: T\n---\n\n"
        "Body about sweep retries and lint failures across the pipeline.\n",
        encoding="utf-8")

    assert app(["--corpus", str(corpus), "index"]) == 0
    capsys.readouterr()
    assert app(["--corpus", str(corpus), "search", "sweep retries lint"]) == 0
    assert "sources/n" in capsys.readouterr().out

    assert app(["--corpus", str(corpus), "delete", "sources/n"]) == 0
    capsys.readouterr()
    assert app(["--corpus", str(corpus), "search", "sweep retries lint"]) == 0
    assert "sources/n" not in capsys.readouterr().out

    assert app(["--corpus", str(corpus), "index", "--rebuild"]) == 0
    capsys.readouterr()
    assert app(["--corpus", str(corpus), "search", "sweep retries lint"]) == 0
    assert "sources/n" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# CLI surface: `alexandria delete` / `--undelete` / `--list`.
# ---------------------------------------------------------------------------

def _write_note(corpus: Path, rel: str, title: str) -> Path:
    path = corpus / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ntype: observation\ntitle: {title}\n---\n\nBody {title}.\n",
                    encoding="utf-8")
    return path


def test_cmd_delete_marks_frontmatter_and_hides_from_search(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    corpus = tmp_path / "corpus"
    path = _write_note(corpus, "sources/a.md", "a")
    assert app(["--corpus", str(corpus), "index"]) == 0
    capsys.readouterr()

    assert app(["--corpus", str(corpus), "delete", "sources/a"]) == 0
    out = capsys.readouterr().out
    assert "deleted" in out and "sources/a" in out

    doc = Doc.read(path, root=corpus)
    assert doc.frontmatter["deleted"] is True


def test_cmd_delete_undelete_restores_visibility(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    corpus = tmp_path / "corpus"
    _write_note(corpus, "sources/a.md", "a")
    assert app(["--corpus", str(corpus), "index"]) == 0
    capsys.readouterr()
    assert app(["--corpus", str(corpus), "delete", "sources/a"]) == 0
    capsys.readouterr()
    assert app(["--corpus", str(corpus), "search", "Body a"]) == 0
    assert "sources/a" not in capsys.readouterr().out

    assert app(["--corpus", str(corpus), "delete", "sources/a", "--undelete"]) == 0
    capsys.readouterr()
    assert app(["--corpus", str(corpus), "search", "Body a"]) == 0
    assert "sources/a" in capsys.readouterr().out


def test_cmd_delete_list_shows_only_flagged_documents(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    corpus = tmp_path / "corpus"
    _write_note(corpus, "sources/a.md", "a")
    _write_note(corpus, "sources/b.md", "b")

    assert app(["--corpus", str(corpus), "delete", "--list"]) == 0
    assert "no documents are flagged deleted" in capsys.readouterr().out

    assert app(["--corpus", str(corpus), "delete", "sources/a"]) == 0
    capsys.readouterr()
    assert app(["--corpus", str(corpus), "delete", "--list"]) == 0
    out = capsys.readouterr().out
    assert "sources/a" in out
    assert "sources/b" not in out

    assert app(["--corpus", str(corpus), "delete", "sources/a", "--undelete"]) == 0
    capsys.readouterr()
    assert app(["--corpus", str(corpus), "delete", "--list"]) == 0
    assert "no documents are flagged deleted" in capsys.readouterr().out


def test_cmd_delete_requires_doc_id_without_list(tmp_path: Path, capsys):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    assert app(["--corpus", str(corpus), "delete"]) == 1
    assert "requires a doc_id" in capsys.readouterr().err


def test_cmd_delete_unknown_doc_id_fails_loudly(tmp_path: Path, capsys):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    assert app(["--corpus", str(corpus), "delete", "sources/nope"]) == 1
    assert "no such document" in capsys.readouterr().err


def test_cmd_delete_before_first_index_still_writes_frontmatter(tmp_path: Path, capsys):
    corpus = tmp_path / "corpus"
    path = _write_note(corpus, "sources/a.md", "a")

    assert app(["--corpus", str(corpus), "delete", "sources/a"]) == 0
    out = capsys.readouterr().out
    assert "not yet indexed" in out

    doc = Doc.read(path, root=corpus)
    assert doc.frontmatter["deleted"] is True


# ---------------------------------------------------------------------------
# delete path containment (SOL-04): a doc_id must name an indexable document
# inside the corpus, never a file outside it.
# ---------------------------------------------------------------------------


def test_cmd_delete_refuses_path_traversal(tmp_path: Path, capsys):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    victim = tmp_path / "victim.md"
    victim.write_text("---\ntype: observation\ntitle: outside\n---\n\nSECRET outside body.\n",
                      encoding="utf-8")
    before = victim.read_bytes()

    assert app(["--corpus", str(corpus), "delete", "../victim"]) == 1
    assert "path traversal" in capsys.readouterr().err
    assert victim.read_bytes() == before, "delete must never rewrite a file outside the corpus"


def test_cmd_delete_refuses_absolute_path(tmp_path: Path, capsys):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    victim = tmp_path / "victim.md"
    victim.write_text("---\ntype: observation\n---\n\noutside.\n", encoding="utf-8")
    before = victim.read_bytes()

    assert app(["--corpus", str(corpus), "delete", str(victim)]) == 1
    assert "not a corpus-relative" in capsys.readouterr().err
    assert victim.read_bytes() == before


def test_cmd_delete_refuses_non_indexable_root(tmp_path: Path, capsys):
    corpus = tmp_path / "corpus"
    path = _write_note(corpus, "inbox/a.md", "a")  # top-level inbox/ is not indexed

    assert app(["--corpus", str(corpus), "delete", "inbox/a"]) == 1
    assert "not indexable" in capsys.readouterr().err

    doc = Doc.read(path, root=corpus)
    assert doc.frontmatter.get("deleted") is not True, "un-indexable file must not be tombstoned"


# ---------------------------------------------------------------------------
# SOL-01/SOL-02/SOL-03 regression guards: delete must converge on every row a
# document ever produced (by stable doc_id), never resurrect via enrichment
# routing or a divergent leg, and fail loudly rather than silently leak.
# ---------------------------------------------------------------------------


def _synthetic(chunk_id: str, doc_id: str, vector: list[float], text: str,
               target: str, *, deleted=False) -> dict:
    """A synthetic enrichment row exactly as enrich.synthetic_records shapes it:
    kind/parent_doc/target_chunk set explicitly, chunk_id `{base}::hqN`."""
    return {
        **record(chunk_id, doc_id, vector, text=text, deleted=deleted),
        "kind": "synthetic",
        "parent_doc": doc_id,
        "target_chunk": target,
    }


def test_cmd_delete_hides_chunks_indexed_under_a_previous_body(tmp_path: Path, monkeypatch, capsys):
    """SOL-02: chunk ids are content-derived, so editing a document changes them.
    delete must tombstone the OLD rows by doc_id, not look up the NEW ids (which
    would match nothing) and report success while the old content stays live."""
    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    corpus = tmp_path / "corpus"
    path = _write_note(corpus, "sources/a.md", "a")
    assert app(["--corpus", str(corpus), "index"]) == 0
    capsys.readouterr()

    # Edit the body WITHOUT reindexing -- old chunk "Body a." stays in both stores.
    path.write_text("---\ntype: observation\ntitle: a\n---\n\nCompletely different body now.\n",
                    encoding="utf-8")

    assert app(["--corpus", str(corpus), "delete", "sources/a"]) == 0
    capsys.readouterr()

    assert app(["--corpus", str(corpus), "search", "Body a"]) == 0
    assert "sources/a" not in capsys.readouterr().out, (
        "the OLD chunk must be tombstoned even though the current body no longer produces its id"
    )


def test_mark_deleted_tombstones_enrichment_synthetic_rows(tmp_path: Path):
    """SOL-01 (root cause): synthetic rows carry parent_doc == doc_id but a
    distinct chunk_id (`{base}::hqN`); mark_deleted must match them by
    parent_doc (dense) and by the `{doc_id}#` prefix (lexical), not by
    re-derived ordinary chunk ids."""
    base = record("sources/a#abc", "sources/a", [1.0, 0.0], text="ordinary base body")
    synth = _synthetic("sources/a#abc::hq1", "sources/a", [0.0, 1.0],
                       "unique hypothetical deletion needle", "sources/a#abc")

    store = VectorStore(tmp_path / "index")
    store.upsert([base, synth])
    assert store.mark_deleted("sources/a", True) == 2
    assert store.get("sources/a#abc")["deleted"] == "true"
    assert store.get("sources/a#abc::hq1")["deleted"] == "true"

    lexical = BM25Index(tmp_path / "fts.sqlite")
    lexical.index([base, synth])
    assert lexical.mark_deleted("sources/a", True) == 2
    for cid in ("sources/a#abc", "sources/a#abc::hq1"):
        got = lexical.connection.execute(
            "SELECT deleted FROM chunk_metadata WHERE chunk_id = ?", (cid,)).fetchone()[0]
        assert got == "true", cid


def test_synthetic_routing_cannot_resurrect_a_deleted_target(tmp_path: Path):
    """SOL-01 (defense in depth): even if a synthetic row is left deleted=false,
    routing it to its target via unfiltered get_many() must drop the target when
    the target's stored deleted flag is not 'false'."""
    embedder = HashEmbedder(dim=24)
    base_vec = embedder.embed(["ordinary base body"])[0]
    synth_vec = embedder.embed(["unique hypothetical deletion needle"])[0]

    base = record("sources/a#abc", "sources/a", base_vec, text="ordinary base body", deleted=True)
    synth = _synthetic("sources/a#abc::hq1", "sources/a", synth_vec,
                       "unique hypothetical deletion needle", "sources/a#abc", deleted=False)

    store = VectorStore(tmp_path / "index")
    store.upsert([base, synth])
    lexical = BM25Index(tmp_path / "fts.sqlite")
    lexical.index([base, synth])
    engine = SearchEngine(embedder, store, lexical, IdentityReranker(),
                          SearchConfig(prefetch=5, top_k=5, wiki_boost=1.25))

    results = engine.search("unique hypothetical deletion needle")
    assert "sources/a#abc" not in {r.chunk_id for r in results}
    assert all("::hq" not in r.chunk_id for r in results), "synthetic rows must never surface directly"


def test_stale_lexical_leg_cannot_resurrect_via_hydration(tmp_path: Path):
    """SOL-03 (defense in depth): after dense commits but lexical does not, a
    lexical-only candidate hydrates from the dense record whose deleted is
    'true' and must be dropped, so the divergent leg cannot leak the document."""
    embedder = HashEmbedder(dim=24)
    vec = embedder.embed(["sweep page lint"])[0]
    row = record("sources/a#abc", "sources/a", vec, text="sweep page lint")

    store = VectorStore(tmp_path / "index")
    store.upsert([row])
    lexical = BM25Index(tmp_path / "fts.sqlite")
    lexical.index([row])

    # dense tombstoned; lexical deliberately left stale (deleted='false').
    store.mark_deleted("sources/a", True)

    engine = SearchEngine(embedder, store, lexical, IdentityReranker(),
                          SearchConfig(prefetch=5, top_k=5, wiki_boost=1.25))
    results = engine.search("sweep page lint")
    assert "sources/a#abc" not in {r.chunk_id for r in results}


def test_cmd_delete_reports_partial_store_failure(tmp_path: Path, monkeypatch, capsys):
    """SOL-03 (loud failure): if the lexical flip raises after the dense flip
    commits, delete must return nonzero (never a silent success), keep the
    frontmatter durable, and tell the operator how to converge."""
    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    corpus = tmp_path / "corpus"
    _write_note(corpus, "sources/a.md", "a")
    assert app(["--corpus", str(corpus), "index"]) == 0
    capsys.readouterr()

    def boom(self, doc_id, deleted):
        raise RuntimeError("boom")

    monkeypatch.setattr(BM25Index, "mark_deleted", boom)

    assert app(["--corpus", str(corpus), "delete", "sources/a"]) == 1
    err = capsys.readouterr().err
    assert "failed partway" in err
    assert "re-run `alexandria delete" in err

    doc = Doc.read(corpus / "sources" / "a.md", root=corpus)
    assert doc.frontmatter["deleted"] is True


def test_dense_leg_survives_an_old_schema_without_deleted(tmp_path: Path):
    """The live corpus's Lance table predates the `deleted` column. search_vector
    must NOT error and silently degrade retrieval to lexical-only just because
    that column does not exist -- a table without it has no tombstones to hide,
    so the tombstone predicate is a no-op that must be skipped, not fatal."""
    import lancedb
    import pyarrow as pa

    db = lancedb.connect(str(tmp_path / "index"))
    db.create_table(
        "chunks",
        data=[{
            "chunk_id": "row", "doc_id": "sources/a", "text": "sweep lint",
            "heading_path": "H", "type": "observation", "project": "",
            "status": "active", "source": "test", "layer": "sources",
            "generated_at": "", "vector": [1.0, 0.0], "tags": [],
            "entities": [], "enrichment": "", "kind": "", "parent_doc": "",
            "target_chunk": "",
        }],
        schema=pa.schema([
            pa.field("chunk_id", pa.string()), pa.field("doc_id", pa.string()),
            pa.field("text", pa.string()), pa.field("heading_path", pa.string()),
            pa.field("type", pa.string()), pa.field("project", pa.string()),
            pa.field("status", pa.string()), pa.field("source", pa.string()),
            pa.field("layer", pa.string()), pa.field("generated_at", pa.string()),
            pa.field("vector", pa.list_(pa.float32(), 2)),
            pa.field("tags", pa.list_(pa.string())), pa.field("entities", pa.list_(pa.string())),
            pa.field("enrichment", pa.string()), pa.field("kind", pa.string()),
            pa.field("parent_doc", pa.string()), pa.field("target_chunk", pa.string()),
        ]),
    )

    store = VectorStore(tmp_path / "index")
    results = store.search_vector([1.0, 0.0], k=5)
    assert [r["chunk_id"] for r in results] == ["row"], (
        "a table without a `deleted` column must still return dense candidates, "
        "not error and degrade the whole dense leg"
    )

    # And the tombstone projection must fail loudly with a rebuild instruction,
    # never silently tombstone only the lexical leg.
    try:
        store.mark_deleted("sources/a", True)
    except RuntimeError as exc:
        assert "index --rebuild" in str(exc)
    else:
        raise AssertionError("mark_deleted on an old schema must fail loudly")


def test_cmd_delete_invalidates_a_stale_enrichment_payload(tmp_path: Path, monkeypatch, capsys):
    """#6 erasure-core item 2: a document's cached enrichment payload (which
    could carry a since-judged-bad hypothetical, or simply be stale) must
    not survive a tombstone -- otherwise a future --enrich run reattaches it
    from the store with no re-validation, even though the document itself
    is already unretrievable. Verified end to end through the real CLI."""
    from alexandria.enrich import EnrichmentStore
    from alexandria.index.releases import resolve_active_index_dir

    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    corpus = tmp_path / "corpus"
    _write_note(corpus, "sources/a.md", "a")
    assert app(["--corpus", str(corpus), "index"]) == 0
    capsys.readouterr()

    index_dir = resolve_active_index_dir(corpus)
    store = EnrichmentStore(index_dir)
    store.put("sources/a", "somesha", "m@v1", {"summary": "s"})
    assert store.count() == 1

    assert app(["--corpus", str(corpus), "delete", "sources/a"]) == 0
    capsys.readouterr()

    # re-open (the CLI's store instance is separate from this test's)
    assert EnrichmentStore(index_dir).count() == 0


def test_cmd_delete_undelete_does_not_touch_enrichment(tmp_path: Path, monkeypatch, capsys):
    """--undelete must NOT invalidate enrichment -- the document's content
    and recipe fingerprint are unchanged, so the cached payload is still
    valid and forcing a re-enrichment call would be pure waste."""
    from alexandria.enrich import EnrichmentStore
    from alexandria.index.releases import resolve_active_index_dir

    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    corpus = tmp_path / "corpus"
    _write_note(corpus, "sources/a.md", "a")
    assert app(["--corpus", str(corpus), "index"]) == 0
    capsys.readouterr()

    index_dir = resolve_active_index_dir(corpus)
    store = EnrichmentStore(index_dir)
    store.put("sources/a", "somesha", "m@v1", {"summary": "s"})

    assert app(["--corpus", str(corpus), "delete", "sources/a"]) == 0
    capsys.readouterr()
    assert EnrichmentStore(index_dir).count() == 0  # deleted -- invalidated

    # re-add for the undelete half of this test (delete already consumed it)
    store2 = EnrichmentStore(index_dir)
    store2.put("sources/a", "somesha", "m@v1", {"summary": "s2"})
    assert app(["--corpus", str(corpus), "delete", "sources/a", "--undelete"]) == 0
    capsys.readouterr()
    assert EnrichmentStore(index_dir).count() == 1  # undelete: untouched


@requires_git_filter_repo
def test_cli_erase_end_to_end_removes_from_git_history_and_cache(tmp_path: Path, monkeypatch, capsys):
    """End to end through the real CLI: index, delete via --yes, confirm the
    document is gone from search AND from git history AND from the
    embedding cache -- the full #6 tail contract in one flow."""
    import subprocess

    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(corpus), check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(corpus), check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=str(corpus), check=True)

    note = _write_note(corpus, "sources/erase-me.md", "erase-me")
    subprocess.run(["git", "add", "-A"], cwd=str(corpus), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add erase-me"], cwd=str(corpus), check=True)

    assert app(["--corpus", str(corpus), "index"]) == 0
    capsys.readouterr()
    assert app(["--corpus", str(corpus), "search", "erase-me"]) == 0
    assert "sources/erase-me" in capsys.readouterr().out

    # Without --yes: prints the blast radius, touches nothing, exits 3.
    rc = app(["--corpus", str(corpus), "erase", "sources/erase-me"])
    assert rc == 3
    capsys.readouterr()
    assert note.exists()  # untouched

    # With --yes: actually erases.
    rc = app(["--corpus", str(corpus), "erase", "sources/erase-me", "--yes"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "erase:" in out
    assert not note.exists()

    log = subprocess.run(["git", "log", "--oneline", "--all", "--", "sources/erase-me.md"],
                         cwd=str(corpus), capture_output=True, text=True)
    assert log.stdout.strip() == ""

    assert app(["--corpus", str(corpus), "search", "erase-me"]) == 0
    assert "sources/erase-me" not in capsys.readouterr().out


@requires_git_filter_repo
def test_cli_erase_purges_the_embedding_cache(tmp_path: Path, monkeypatch, capsys):
    """The cache-before-history sequencing invariant, proven through the
    real CLI: after erase, the document's chunk text is no longer a cache
    hit (a re-embed of identical text would be a fresh provider call)."""
    import subprocess

    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(corpus), check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(corpus), check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=str(corpus), check=True)

    _write_note(corpus, "sources/cached.md", "cached")
    subprocess.run(["git", "add", "-A"], cwd=str(corpus), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add cached"], cwd=str(corpus), check=True)

    assert app(["--corpus", str(corpus), "index"]) == 0
    capsys.readouterr()

    cache_path = corpus / ".alexandria" / "cache" / "embeddings.sqlite"
    assert cache_path.exists()
    import sqlite3
    conn = sqlite3.connect(str(cache_path))
    rows_before = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    conn.close()
    assert rows_before > 0

    assert app(["--corpus", str(corpus), "erase", "sources/cached", "--yes"]) == 0
    capsys.readouterr()

    conn = sqlite3.connect(str(cache_path))
    rows_after = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    conn.close()
    assert rows_after < rows_before


def test_cli_erase_refuses_an_unknown_doc_id(tmp_path: Path, monkeypatch, capsys):
    import subprocess

    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(corpus), check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(corpus), check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=str(corpus), check=True)

    rc = app(["--corpus", str(corpus), "erase", "sources/never-existed", "--yes"])
    assert rc == 1


@requires_git_filter_repo
def test_cli_erase_leaves_a_recoverable_pre_erase_git_backup(tmp_path: Path, monkeypatch, capsys):
    import subprocess

    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(corpus), check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(corpus), check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=str(corpus), check=True)
    _write_note(corpus, "sources/x.md", "x")
    subprocess.run(["git", "add", "-A"], cwd=str(corpus), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add x"], cwd=str(corpus), check=True)

    assert app(["--corpus", str(corpus), "index"]) == 0
    capsys.readouterr()
    assert app(["--corpus", str(corpus), "erase", "sources/x", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "erase:" in out
    assert "pre-erase git history retained" in out

    # The pre-erase .git is retained under the verified-untracked durable
    # state root, never under a tracked-worktree name.
    backups = sorted((corpus / ".alexandria" / "erase-backups").iterdir())
    assert len(backups) == 1
    backup_dir = backups[0] / "git"
    assert backup_dir.is_dir()
    log = subprocess.run(["git", "--git-dir", str(backup_dir), "log", "--oneline"],
                         cwd=str(corpus), capture_output=True, text=True, timeout=10)
    assert "add x" in log.stdout


@requires_git_filter_repo
def test_cli_erase_never_touches_the_untracked_alexandria_state_directory(
        tmp_path: Path, monkeypatch, capsys):
    """Regression test for a real bug found live during development: an
    early implementation's working-tree sync used `git clean -fd` at the
    corpus root, which -- because .alexandria/ (index, embedding cache,
    audit trail) is deliberately UNTRACKED corpus state -- deleted the
    entire index and cache alongside the one erased document. This proves
    a SECOND, unrelated document's index entry and the cache database
    itself both survive an erase of a completely different document."""
    import subprocess
    import sqlite3

    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(corpus), check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(corpus), check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=str(corpus), check=True)

    _write_note(corpus, "sources/keep.md", "keep")
    _write_note(corpus, "sources/erase-me.md", "erase-me")
    subprocess.run(["git", "add", "-A"], cwd=str(corpus), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add both"], cwd=str(corpus), check=True)

    assert app(["--corpus", str(corpus), "index"]) == 0
    capsys.readouterr()

    cache_path = corpus / ".alexandria" / "cache" / "embeddings.sqlite"
    assert cache_path.exists()
    audit_dir = corpus / ".alexandria" / "audit"

    assert app(["--corpus", str(corpus), "erase", "sources/erase-me", "--yes"]) == 0
    capsys.readouterr()

    # The whole .alexandria/ tree must still be present and functional.
    assert cache_path.exists()
    conn = sqlite3.connect(str(cache_path))
    conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()
    conn.close()

    # The OTHER document must still be fully searchable -- its index entry
    # was never touched by the erase.
    assert app(["--corpus", str(corpus), "search", "keep"]) == 0
    assert "sources/keep" in capsys.readouterr().out


@requires_git_filter_repo
def test_cli_erase_reports_when_rewritten_history_is_already_active(
        tmp_path: Path, monkeypatch, capsys):
    """#77: a post-swap GitEraseError (history_changed=True) must be reported
    as ALREADY ACTIVE with a recovery/retry instruction -- never as
    'history is UNCHANGED', which would be a false promise after the
    rewritten .git landed."""
    import subprocess

    import alexandria.erasure as erasure_mod
    from alexandria.erasure import GitEraseError

    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(corpus), check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(corpus), check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=str(corpus), check=True)
    _write_note(corpus, "sources/boom.md", "boom")
    subprocess.run(["git", "add", "-A"], cwd=str(corpus), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add boom"], cwd=str(corpus), check=True)

    assert app(["--corpus", str(corpus), "index"]) == 0
    capsys.readouterr()

    def post_swap_failure(corpus_arg, rel_path, **kwargs):
        raise GitEraseError("simulated failure after the rewritten .git landed",
                            history_changed=True)

    monkeypatch.setattr(erasure_mod, "erase_from_git_history", post_swap_failure)
    rc = app(["--corpus", str(corpus), "erase", "sources/boom", "--yes"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "ALREADY ACTIVE" in err
    assert "UNCHANGED" not in err
    assert "recover" in err.lower()


@requires_git_filter_repo
def test_cli_erase_holds_the_corpus_write_lock_across_the_whole_operation(
        tmp_path: Path, monkeypatch, capsys):
    """The load-bearing structural fix from this item's own pre-code
    failure-frame note: cmd_erase must hold the corpus write lock across
    tombstone + cache purge + git rewrite as ONE critical section, not
    three separate windows a concurrent index/promote/second-erase could
    interleave with. Proven by attempting a SECOND, independent
    (non-blocking) lock acquisition from mid-way through the git-rewrite
    step and confirming it is refused as busy."""
    import subprocess

    from alexandria.writelock import WriteLock
    import alexandria.erasure as erasure_mod

    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(corpus), check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(corpus), check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=str(corpus), check=True)
    _write_note(corpus, "sources/locked.md", "locked")
    subprocess.run(["git", "add", "-A"], cwd=str(corpus), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add locked"], cwd=str(corpus), check=True)

    assert app(["--corpus", str(corpus), "index"]) == 0
    capsys.readouterr()

    observed = {}
    real_erase = erasure_mod.erase_from_git_history

    def spying_erase(corpus_arg, rel_path, **kwargs):
        # mid-operation: a fresh, independent lock handle must find the
        # corpus write lock already held (non-blocking acquire must fail).
        probe = WriteLock(corpus_arg)
        observed["acquired_while_erase_running"] = probe.acquire(blocking=False)
        if observed["acquired_while_erase_running"]:
            probe.release()  # avoid leaving a stray lock if the assertion below fails
        return real_erase(corpus_arg, rel_path, **kwargs)

    monkeypatch.setattr(erasure_mod, "erase_from_git_history", spying_erase)
    # cmd_erase does `from .erasure import ... erase_from_git_history` INSIDE the
    # function body, so patching the module attribute is sufficient (no bound
    # copy exists yet at monkeypatch time).

    rc = app(["--corpus", str(corpus), "erase", "sources/locked", "--yes"])
    assert rc == 0
    capsys.readouterr()

    assert "acquired_while_erase_running" in observed
    assert observed["acquired_while_erase_running"] is False, (
        "a second, independent lock acquisition succeeded WHILE erase was "
        "still running its git-rewrite step -- the write lock is not held "
        "across the whole operation")

    # sanity: the lock must be free again immediately after erase returns.
    probe2 = WriteLock(corpus)
    assert probe2.acquire(blocking=False) is True
    probe2.release()


# --- Red round 2, finding 2: a pre-swap rewrite failure must roll the
# tombstone back so a retry can pass the clean-state preflight.


@requires_git_filter_repo
def test_cli_erase_pre_swap_failure_rolls_back_the_tombstone_for_retry(
        tmp_path: Path, monkeypatch, capsys):
    """If the git rewrite fails BEFORE rewritten history becomes active, the
    erased document's tombstone must be undone (file bytes restored from HEAD,
    index rows un-flagged) so the corpus is unchanged and `--yes` retries."""
    import subprocess

    import alexandria.erasure as erasure_mod
    from alexandria.erasure import GitEraseError

    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(corpus), check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(corpus), check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=str(corpus), check=True)
    _write_note(corpus, "sources/retry.md", "retry me")
    subprocess.run(["git", "add", "-A"], cwd=str(corpus), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add retry"], cwd=str(corpus), check=True)

    assert app(["--corpus", str(corpus), "index"]) == 0
    capsys.readouterr()

    real_erase = erasure_mod.erase_from_git_history
    calls = [0]

    def pre_swap_failure(corpus_arg, rel_path, **kwargs):
        calls[0] += 1
        if calls[0] == 1:
            raise GitEraseError("simulated pre-swap rewrite failure", history_changed=False)
        return real_erase(corpus_arg, rel_path, **kwargs)

    monkeypatch.setattr(erasure_mod, "erase_from_git_history", pre_swap_failure)
    rc = app(["--corpus", str(corpus), "erase", "sources/retry", "--yes"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "rolled back" in err
    assert "UNCHANGED" in err

    # The file is back and searchable again...
    note = corpus / "sources" / "retry.md"
    assert note.exists()
    assert "retry me" in note.read_text()
    assert app(["--corpus", str(corpus), "search", "retry"]) == 0
    assert "sources/retry" in capsys.readouterr().out

    # ...and a retry with a CLEAN preflight can pass (no tracked changes left)
    # and completes the real rewrite.
    assert app(["--corpus", str(corpus), "erase", "sources/retry", "--yes"]) == 0
    assert not note.exists()


# --- Red round 2, finding 3: cmd_erase must fail closed (and roll the
# tombstone back) when whole-cache invalidation does not actually clear.


@requires_git_filter_repo
def test_cli_erase_fails_closed_when_cache_invalidation_does_not_clear(
        tmp_path: Path, monkeypatch, capsys):
    """If purge_all leaves durable rows, cmd_erase must abort BEFORE rewriting
    history and roll the tombstone back so the corpus is unchanged."""
    import subprocess

    from alexandria.index import embedder as embedder_mod

    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(corpus), check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(corpus), check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=str(corpus), check=True)
    _write_note(corpus, "sources/c.md", "c")
    subprocess.run(["git", "add", "-A"], cwd=str(corpus), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add c"], cwd=str(corpus), check=True)

    assert app(["--corpus", str(corpus), "index"]) == 0
    capsys.readouterr()

    def broken_purge_all(self):
        return 0  # leaves every row in place

    monkeypatch.setattr(embedder_mod.CachedEmbedder, "purge_all", broken_purge_all)
    rc = app(["--corpus", str(corpus), "erase", "sources/c", "--yes"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "could not be invalidated" in err
    assert "UNCHANGED" in err

    # History was NOT rewritten and the file is back (tombstone rolled back).
    log = subprocess.run(["git", "log", "--oneline", "--all", "--", "sources/c.md"],
                         cwd=str(corpus), capture_output=True, text=True)
    assert log.stdout.strip() != ""
    note = corpus / "sources" / "c.md"
    assert note.exists()
    assert "c" in note.read_text()

    # And the cache rows are still there (purge_all was the sabotage, not a fix).
    import sqlite3
    conn = sqlite3.connect(str(corpus / ".alexandria" / "cache" / "embeddings.sqlite"))
    assert conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] > 0
    conn.close()
