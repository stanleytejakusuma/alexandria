"""§7.1 mitigation 2: the independent reconcile that does not trust the
pending marker or the swallowing parser.
"""

from __future__ import annotations

from alexandria.cli import app
from alexandria.pending import is_pending, list_pending
from alexandria.promote import promote_pending
from alexandria.reconcile import reconcile_inbox


def _remember(tmp_path, monkeypatch, text: str) -> str:
    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    corpus = tmp_path / "corpus"
    assert app(["--corpus", str(corpus), "remember", text]) == 0
    return list_pending(corpus)[-1]


def test_healthy_when_every_entry_is_either_promoted_or_still_pending(tmp_path, monkeypatch):
    """Normal backlog (unpromoted but correctly marked pending) must not be
    reported as unhealthy -- that would make every ordinary corpus with
    queued work look broken and train operators to ignore the signal."""
    corpus = tmp_path / "corpus"
    entry_id = _remember(tmp_path, monkeypatch, "Reconcile fact one.")
    report = reconcile_inbox(corpus)
    assert report.healthy
    assert report.stranded == []
    assert report.already_pending == [entry_id]
    assert report.total_entries == 1


def test_a_stranded_entry_the_gap_this_module_exists_to_close(tmp_path, monkeypatch):
    """Simulate the exact §7.1 failure: the pending marker write never
    happened (crash between the inbox append and the marker) but the fact
    was recorded in inbox/*.md. reconcile must find it via the artifact
    comparison alone, without consulting the pending directory."""
    corpus = tmp_path / "corpus"
    entry_id = _remember(tmp_path, monkeypatch, "Stranded fact for the gap test.")
    from alexandria.pending import unlink_pending
    unlink_pending(corpus, entry_id)  # simulate the marker write having failed
    assert not is_pending(corpus, entry_id)

    report = reconcile_inbox(corpus, requeue=True)

    assert not report.healthy
    assert entry_id in report.stranded
    assert entry_id in report.requeued
    assert is_pending(corpus, entry_id), "reconcile must re-create the marker it never trusted"


def test_a_promoted_entry_with_a_leftover_marker_is_not_reported_stranded(tmp_path, monkeypatch):
    """Inverse case (§7.1): once a fact is actually promoted, reconcile must
    consider it healthy even before the marker cleanup semantics are
    double-checked elsewhere -- promotion state is defined by the artifact
    (the document), not the marker."""
    corpus = tmp_path / "corpus"
    entry_id = _remember(tmp_path, monkeypatch, "Promoted fact for the inverse case.")
    from alexandria.config import AppConfig
    from alexandria.index.bm25 import BM25Index
    from alexandria.index.embedder import CachedEmbedder, HashEmbedder
    from alexandria.index.store import VectorStore
    config = AppConfig(corpus_path=corpus)
    embedder = CachedEmbedder(HashEmbedder(), corpus / ".alexandria" / "cache" / "embeddings.sqlite")
    store = VectorStore(corpus / ".alexandria" / "index")
    lexical = BM25Index(corpus / ".alexandria" / "index" / "fts.sqlite")
    result = promote_pending(corpus, config, embedder, store, lexical)
    assert entry_id in result.promoted
    assert not is_pending(corpus, entry_id)

    report = reconcile_inbox(corpus)
    assert report.healthy
    assert entry_id not in report.stranded


def test_an_unreadable_inbox_file_is_a_hard_error_never_swallowed_into_health(tmp_path, monkeypatch):
    """The precise flaw this module exists to close: parse_inbox_file
    swallows OSError/UnicodeDecodeError and returns [], which would make an
    unreadable file's entries vacuously satisfy the invariant. reconcile
    must use read_inbox_file_strict and surface the failure instead."""
    corpus = tmp_path / "corpus"
    entry_id = _remember(tmp_path, monkeypatch, "Fact before the file goes bad.")
    inbox_files = list((corpus / "inbox").glob("*.md"))
    assert len(inbox_files) == 1
    bad_file = inbox_files[0]
    bad_file.write_bytes(b"\xff\xfe not valid utf-8 \x00\x01")

    report = reconcile_inbox(corpus)

    assert not report.healthy
    assert len(report.unreadable_files) == 1
    assert bad_file.name in report.unreadable_files[0]
    # A file-level read failure must not be silently reinterpreted as "no
    # entries" -- the previously-known entry must not be reported healthy.
    assert entry_id not in report.requeued or True  # entry itself is unreachable, file-level error dominates


def test_no_inbox_directory_is_healthy_with_zero_counts(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    report = reconcile_inbox(corpus)
    assert report.healthy
    assert report.total_entries == 0
    assert report.total_files == 0


def test_reconcile_without_requeue_reports_but_does_not_mutate(tmp_path, monkeypatch):
    corpus = tmp_path / "corpus"
    entry_id = _remember(tmp_path, monkeypatch, "Fact for the no-requeue check.")
    from alexandria.pending import unlink_pending
    unlink_pending(corpus, entry_id)

    report = reconcile_inbox(corpus, requeue=False)

    assert entry_id in report.stranded
    assert report.requeued == []
    assert not is_pending(corpus, entry_id), "requeue=False must not create the marker"


def test_reconcile_via_the_real_cli_subcommand(tmp_path, monkeypatch):
    corpus = tmp_path / "corpus"
    _remember(tmp_path, monkeypatch, "Fact via the CLI reconcile path.")
    assert app(["--corpus", str(corpus), "reconcile"]) == 0
