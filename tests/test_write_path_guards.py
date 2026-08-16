"""Write-path guards found by code review of the write-path package.

Two failures, both invisible from the read side:

1. The inbox append was a size check plus two writes, under no lock, on a
   ThreadingHTTPServer. Two concurrent /remember calls could interleave into
   one fused entry -- in a corpus with no deletion path, unrecoverable.
2. verify_manifest() was wired only into _build_search_engine (the reader).
   cmd_index and promote_pending wrote vectors with no check and then
   rewrote the manifest to match, so the read guard passed forever over a
   mixed vector space.

Each test here fails if its guard is reverted; that is the point of them.
"""

from __future__ import annotations

import errno
import json
import os

import pytest

from alexandria.cli import app, append_inbox_entry, _cached_embedder
from alexandria.config import load_config
from alexandria.connectors.inbox import parse_inbox_file
from alexandria.index.manifest import read_manifest
from alexandria.index.bm25 import BM25Index
from alexandria.index.store import VectorStore
from alexandria.promote import promote_pending


# --- 1. the inbox append is atomic -------------------------------------------------

def test_inbox_append_writes_the_entry_in_exactly_one_syscall(tmp_path, monkeypatch):
    """The whole point: a size check plus two writes is the race. Reverting to
    the two-write form fails this test."""
    corpus = tmp_path / "corpus"
    calls: list[bytes] = []
    real_write = os.write

    def spy(fd, data):
        calls.append(data)
        return real_write(fd, data)

    monkeypatch.setattr("alexandria.cli.os.write", spy)
    result = append_inbox_entry(corpus, "the gateway routes billing traffic")

    assert result.status == "written"
    inbox_writes = [d for d in calls if b"gateway routes billing" in d]
    assert len(inbox_writes) == 1, (
        f"entry must reach the inbox in one atomic append, got {len(inbox_writes)} writes")
    # The separator must ride in the SAME write, or the interleave is still open.
    assert b"\n\xc2\xa7\n" in inbox_writes[0]


def test_a_fresh_inbox_files_leading_separator_still_parses_as_one_entry(tmp_path):
    """The separator is now written unconditionally -- deciding on file size is
    the race. That leaves an empty leading chunk on a fresh file, which the
    parser must drop. If it ever stops dropping it, this catches it."""
    corpus = tmp_path / "corpus"
    result = append_inbox_entry(corpus, "a single fact about billing tiers")

    entries = parse_inbox_file(result.path)
    assert len(entries) == 1
    assert entries[0].text == "a single fact about billing tiers"


def test_sequential_appends_stay_separate_entries(tmp_path):
    corpus = tmp_path / "corpus"
    append_inbox_entry(corpus, "first fact about routing")
    result = append_inbox_entry(corpus, "second fact about billing")

    entries = parse_inbox_file(result.path)
    assert [e.text for e in entries] == [
        "first fact about routing", "second fact about billing"]


# --- 2. the manifest guard is wired to the WRITE path ------------------------------

def _index_a_tiny_corpus(tmp_path, monkeypatch):
    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    corpus = tmp_path / "corpus"
    doc = corpus / "sources" / "note.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("---\nsource: test\n---\n\nexample gateway routes traffic "
                   "through a placeholder billing tier.\n")
    assert app(["--corpus", str(corpus), "index"]) == 0
    return corpus


def test_index_refuses_to_add_vectors_to_an_index_another_model_built(tmp_path, monkeypatch):
    """The gap the review found: no test asserted the guard was wired to any
    command. Deleting the verify_manifest_for_write call in cmd_index makes
    this pass silently -- and the corrupted index would then be undetectable,
    because cmd_index rewrites the manifest to match what it just wrote."""
    corpus = _index_a_tiny_corpus(tmp_path, monkeypatch)
    manifest_path = corpus / ".alexandria" / "index" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["provider"] = "some-other-provider"
    manifest_path.write_text(json.dumps(manifest))

    second = corpus / "sources" / "second.md"
    second.write_text("---\nsource: test\n---\n\nanother document entirely.\n")

    with pytest.raises(SystemExit) as excinfo:
        app(["--corpus", str(corpus), "index"])

    assert "some-other-provider" in str(excinfo.value)
    # and it must refuse BEFORE writing, so the manifest is not rewritten to
    # match the vectors it would have added
    assert json.loads(manifest_path.read_text())["provider"] == "some-other-provider"


def test_rebuild_is_the_escape_hatch_when_the_manifest_mismatches(tmp_path, monkeypatch):
    """The manifest error's own advice is "rebuild", but the write guard ran
    BEFORE the --rebuild drop and refused it -- a self-contradiction. --rebuild
    drops the table first, so there is no existing vector space to mix with and
    a mismatched (or switched) provider must not block it."""
    corpus = _index_a_tiny_corpus(tmp_path, monkeypatch)  # indexed with "hash"
    manifest_path = corpus / ".alexandria" / "index" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["provider"] = "some-other-provider"
    manifest_path.write_text(json.dumps(manifest))

    assert app(["--corpus", str(corpus), "index", "--rebuild"]) == 0
    assert read_manifest(corpus)["provider"] == "hash"


def test_index_allows_a_fresh_corpus_that_has_no_manifest_yet(tmp_path, monkeypatch):
    """Absence is only a refusal when there are vectors to disagree with.
    An empty index must still be indexable, or nothing can ever bootstrap."""
    corpus = _index_a_tiny_corpus(tmp_path, monkeypatch)
    assert read_manifest(corpus) is not None


def test_a_second_promote_is_not_deadlocked_by_the_write_guard(tmp_path, monkeypatch):
    """Regression for a deadlock the guard itself introduced: promote writes
    vectors but never claimed them, so a corpus reached only through
    remember+promote went non-empty with no manifest -- and every later
    promote refused forever. promote must claim the manifest before writing."""
    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    corpus = tmp_path / "corpus"
    corpus.mkdir()

    assert app(["--corpus", str(corpus), "remember", "first fact about routing"]) == 0
    assert app(["--corpus", str(corpus), "promote"]) == 0
    assert read_manifest(corpus) is not None, "promote must claim the vector space it writes"

    assert app(["--corpus", str(corpus), "remember", "second fact about billing"]) == 0
    assert app(["--corpus", str(corpus), "promote"]) == 0


# --- 4. round-3 fixes: the two gates the round-2 fixes left untested ---------------


def test_a_promote_that_dies_before_writing_vectors_does_not_mislabel_the_index(
    tmp_path, monkeypatch
):
    """The claim predicate must be the SAME predicate the guard exempts on.

    With the claim keyed on `read_manifest() is None` but the exemption keyed
    on `store.count() == 0`, a promote that claims provider A and then dies
    before store.upsert leaves an empty index labelled A. The next promote
    under provider B is exempted (count is still 0), sees a manifest present
    so does not claim, and lands B vectors under an A label. promote never
    rewrites the manifest at the end the way cmd_index does, so nothing ever
    repairs it -- and there is no deletion path.
    """
    from alexandria.index.manifest import read_manifest

    corpus = tmp_path / "corpus"
    (corpus / ".alexandria").mkdir(parents=True)

    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    assert app(["--corpus", str(corpus), "remember", "first fact"]) == 0

    config = load_config(corpus_override=corpus)
    embedder = _cached_embedder(config, corpus)
    store = VectorStore(corpus / ".alexandria" / "index")
    lexical = BM25Index(corpus / ".alexandria" / "index" / "fts.sqlite")

    class Boom(RuntimeError):
        pass

    def die_after_embed(step):
        if step == "embed":
            raise Boom("process died before any vector landed")

    with pytest.raises(Boom):
        promote_pending(corpus, config, embedder, store, lexical, test_hook=die_after_embed)

    # The crash left a manifest but no vectors: exactly the stranded state.
    assert read_manifest(corpus) is not None
    assert store.count() == 0

    # A second promote under the same empty-index condition must RE-CLAIM,
    # not skip. Reverting the predicate to `read_manifest(corpus) is None`
    # makes this assertion fail, because the stale claim survives.
    stale = dict(read_manifest(corpus))
    stale["provider"] = "some-other-provider"
    manifest_path = corpus / ".alexandria" / "index" / "manifest.json"
    manifest_path.write_text(json.dumps(stale), encoding="utf-8")

    result = promote_pending(corpus, config, embedder, store, lexical)

    assert result.promoted, "the entry should have promoted on the retry"
    assert store.count() > 0, "vectors landed, so the label now matters"
    assert read_manifest(corpus)["provider"] == config.embed_provider, (
        "the manifest still carries a provider that did not build these vectors"
    )


def test_a_remember_whose_marker_fails_reports_it_rather_than_claiming_success(
    tmp_path, monkeypatch
):
    """F6: an entry written to the inbox with no pending marker is invisible
    to promote. remember must not report success, and the distinct
    inbox_write_failed status must not be conflated with it -- reconcile keys
    off these codes, so a mislabel sends recovery down the wrong path."""
    corpus = tmp_path / "corpus"
    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")

    def refuse(*a, **kw):
        raise OSError(errno.EROFS, "read-only file system")

    monkeypatch.setattr("alexandria.cli.create_pending", refuse)
    result = append_inbox_entry(corpus, "a fact whose marker cannot be written")

    assert result.status == "marker_failed", "must not be reported as written"
    assert result.entry is not None, "the entry EXISTS on disk; callers need its id to recover"
    assert "read-only file system" in (result.error or "")

    # The inbox entry is genuinely there -- which is why silence would be a
    # data-visibility bug rather than a no-op.
    entries = parse_inbox_file(result.path)
    assert len(entries) == 1
    assert entries[0].entry_id == result.entry.entry_id
