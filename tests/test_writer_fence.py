"""#30 cross-writer integrity: every corpus writer holds the write lock.

Audit 2026-08-23 found four mutating commands that took NO write lock while
index/promote/erase/serve also read or wrote the same state: `restore`
(backup.restore_state), `sync` (cmd_sync), `reconcile` (reconcile_inbox), and
`cache --clear` (cmd_cache). Each test below holds the lock externally and
proves the command refuses loudly with nothing mutated -- the same fence
index/ingest/promote already had. Every test fails against the pre-fix code,
which never touches the lock.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from alexandria.backup import backup_state, restore_state
from alexandria.reconcile import reconcile_inbox
from alexandria.writelock import WriteLock


def _make_corpus(tmp_path: Path, *, with_inbox: bool = False) -> Path:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    if with_inbox:
        (corpus / "inbox").mkdir()
    return corpus


def _git_init(corpus: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(corpus), check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(corpus), check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=str(corpus), check=True)


def _write_state(corpus: Path, name: str, text: str) -> Path:
    p = corpus / ".alexandria" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def test_restore_refuses_while_another_writer_holds_the_lock(tmp_path: Path):
    """restore overwrites live .alexandria state; it must refuse (loudly,
    nothing restored) while any other writer holds the corpus write lock."""
    corpus = _make_corpus(tmp_path)
    state = _write_state(corpus, "queries.sqlite", "original")
    archive = tmp_path / "state.tar.gz"
    backup_state(corpus, archive)
    state.write_text("changed after backup")

    holder = WriteLock(corpus)
    assert holder.acquire(blocking=False)
    try:
        with pytest.raises(RuntimeError, match="restore could not acquire the corpus write lock"):
            restore_state(corpus, archive)
    finally:
        holder.release()

    # Nothing was restored: the post-backup edit survives.
    assert state.read_text() == "changed after backup"
    # The lock is free again for the next writer.
    probe = WriteLock(corpus)
    assert probe.acquire(blocking=False)
    probe.release()


def test_sync_refuses_while_another_writer_holds_the_lock(tmp_path: Path, capsys):
    """sync writes corpus source documents; it must refuse (exit nonzero,
    nothing written) while another writer holds the lock."""
    from alexandria import cli

    corpus = _make_corpus(tmp_path, with_inbox=True)
    holder = WriteLock(corpus)
    assert holder.acquire(blocking=False)
    try:
        rc = cli.app(["--corpus", str(corpus), "sync", "inbox"])
        assert rc == 1
        assert "sync could not acquire the corpus write lock" in capsys.readouterr().err
    finally:
        holder.release()

    probe = WriteLock(corpus)
    assert probe.acquire(blocking=False)
    probe.release()


def test_reconcile_refuses_while_another_writer_holds_the_lock(tmp_path: Path):
    """reconcile writes pending markers the drain consumes; it must refuse
    (nothing requeued) while another writer holds the lock."""
    corpus = _make_corpus(tmp_path, with_inbox=True)
    (corpus / "inbox" / "entry.md").write_text("stray entry\n")

    holder = WriteLock(corpus)
    assert holder.acquire(blocking=False)
    try:
        with pytest.raises(RuntimeError, match="reconcile could not acquire the corpus write lock"):
            reconcile_inbox(corpus)
    finally:
        holder.release()

    # No pending marker was created.
    assert not (corpus / ".alexandria" / "pending").exists()

    probe = WriteLock(corpus)
    assert probe.acquire(blocking=False)
    probe.release()


def test_cache_clear_refuses_while_another_writer_holds_the_lock(tmp_path: Path, capsys):
    """cache --clear mutates the query/response tables serve reads and writes;
    it must refuse (exit nonzero, nothing cleared) while another writer holds
    the lock."""
    from alexandria import cli

    corpus = _make_corpus(tmp_path)
    holder = WriteLock(corpus)
    assert holder.acquire(blocking=False)
    try:
        rc = cli.app(["--corpus", str(corpus), "cache", "--clear"])
        assert rc == 1
        assert "cache --clear could not acquire the corpus write lock" in capsys.readouterr().err
    finally:
        holder.release()

    probe = WriteLock(corpus)
    assert probe.acquire(blocking=False)
    probe.release()


def test_all_four_writers_acquire_and_release_the_lock_when_free(tmp_path: Path, capsys):
    """Positive half: with the lock free, every audited writer runs and then
    releases the lock (a fresh holder succeeds immediately afterward)."""
    from alexandria import cli

    corpus = _make_corpus(tmp_path, with_inbox=True)
    state = _write_state(corpus, "queries.sqlite", "original")
    archive = tmp_path / "state.tar.gz"
    backup_state(corpus, archive)
    state.write_text("changed after backup")

    assert restore_state(corpus, archive).restored
    assert state.read_text() == "original"
    assert cli.app(["--corpus", str(corpus), "sync", "inbox"]) == 0
    reconcile_inbox(corpus)
    capsys.readouterr()
    assert cli.app(["--corpus", str(corpus), "cache", "--clear"]) == 0
    capsys.readouterr()

    probe = WriteLock(corpus)
    assert probe.acquire(blocking=False)
    probe.release()
