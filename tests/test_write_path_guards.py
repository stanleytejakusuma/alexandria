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

import json
import os

import pytest

from alexandria.cli import app, append_inbox_entry
from alexandria.connectors.inbox import parse_inbox_file
from alexandria.index.manifest import read_manifest


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
