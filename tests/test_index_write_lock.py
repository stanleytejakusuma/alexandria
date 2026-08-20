"""BACKLOG #50: `promote_pending` was the ONLY caller of `write_lock()`;
`cmd_index` never took it, so a `--rebuild` (which drops the FTS/vector
tables and refills them only from a chunk-record snapshot taken before the
drop) could race a concurrent `promote_pending` (CLI, or serve's 600s
drain). The dangerous ordering, confirmed by reading promote.py and
cli.py's cmd_index before writing any fix:

  1. cmd_index takes its chunk-record snapshot (`_load_chunk_records`) --
     call this t0. A NEW pending entry does not exist on disk yet.
  2. A promote (drain or CLI) writes the new entry's doc file, chunks it,
     embeds it, and calls `lexical.index(records, append_only=False)` --
     inserting real FTS rows for it. This happens strictly after t0.
  3. cmd_index reaches `lexical.drop()` (`--rebuild`'s `DELETE FROM
     chunks_fts` / `chunk_metadata`, see index/bm25.py) -- wiping ALL rows,
     including the ones step 2 just inserted.
  4. cmd_index's rebuild pipeline re-populates the tables using ONLY the t0
     snapshot (`append_only=True`, pure INSERT, never a delete+reinsert) --
     the step-2 entry is invisible to that snapshot, so its FTS row is never
     restored.
  5. promote already unlinked its pending marker (step 4/5 of its own
     five-step pipeline) regardless of what happened to its writes -- it has
     no way to know they were wiped.

Net effect: an entry considered permanently promoted (marker gone, never
retried) whose FTS rows do not exist -- permanently promoted-but-unsearchable,
in a corpus with no deletion path. This is real, not hypothetical: it only
requires promote's doc-write to land after t0 and its FTS insert to land
before step 3, both well within an ordinary few-second promote cycle
racing a multi-minute rebuild.

The fix: `cmd_index` now takes the SAME write lock promote_pending already
takes, but BLOCKING with a bounded timeout (`writelock.DEFAULT_LOCK_TIMEOUT`)
instead of promote's non-blocking skip -- an index run that silently did
nothing because the lock was busy would be exactly the "reported success
while doing nothing" failure this project keeps finding, so it waits, then
fails loudly and non-zero naming the holder if it can't get in. Because
promote_pending already holds this same lock across its own entire
embed->upsert->fts->bump->unlink sequence, the two are now fully mutually
exclusive: whichever gets the lock first runs to completion (marker write
AND FTS write together) before the other's snapshot/drop ever happens.
"""

from __future__ import annotations

import os
import threading
import time

import pytest

from alexandria.cli import app
from alexandria.config import AppConfig
from alexandria.index.bm25 import BM25Index
from alexandria.index.embedder import CachedEmbedder, HashEmbedder
from alexandria.index.store import VectorStore
from alexandria.pending import is_pending, list_pending
from alexandria.promote import promote_pending
from alexandria.writelock import write_lock


def _remember(tmp_path, monkeypatch, text: str) -> str:
    """Same helper as test_promote.py: write one entry through the real CLI
    path and return its entry id."""
    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    corpus = tmp_path / "corpus"
    assert app(["--corpus", str(corpus), "remember", text]) == 0
    pending = list_pending(corpus)
    assert len(pending) >= 1
    return pending[-1]


def _engine_pieces(corpus):
    # Must match ALEXANDRIA_EMBED_PROVIDER=hash set by _remember()/the earlier
    # `index` call in this file's tests -- AppConfig's own default ("local")
    # would mismatch the manifest already on disk and raise ManifestMismatch.
    config = AppConfig(corpus_path=corpus, embed_provider="hash")
    embedder = CachedEmbedder(HashEmbedder(), corpus / ".alexandria" / "cache" / "embeddings.sqlite")
    store = VectorStore(corpus / ".alexandria" / "index")
    lexical = BM25Index(corpus / ".alexandria" / "index" / "fts.sqlite")
    return config, embedder, store, lexical


# --------------------------------------------------------------------------
# 1. THE TEST THAT MATTERS MOST: the actual dangerous interleaving, closed.
# --------------------------------------------------------------------------

def test_a_rebuild_and_a_concurrent_promote_no_longer_race_the_promote_defers_instead(
        tmp_path, monkeypatch):
    """Deterministically reproduces the exact ordering from the module
    docstring using a synchronization point at `lexical.drop()` -- the
    precise moment the old code was vulnerable. With the fix, cmd_index
    holds the write lock across this entire pause, so the concurrent
    promote must observe `skipped_locked` (not touch anything, marker
    stays pending) rather than racing in a write that the rebuild then
    destroys.

    MUTATION CHECK (performed by hand, see report): reverting cli.py's
    lock-acquire in cmd_index back to no lock at all makes the promote
    call below actually SUCCEED during the pause -- `skipped_locked`
    becomes False and the entry is marked promoted -- and the final
    assertion here (skipped_locked is True) fails, exactly reproducing
    the bug this test exists to catch.
    """
    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    corpus = tmp_path / "corpus"

    # An existing, already-indexed doc -- makes --rebuild non-trivial (it has
    # something to legitimately preserve, not just an empty table).
    note = corpus / "sources" / "pi" / "existing.md"
    note.parent.mkdir(parents=True)
    note.write_text(
        "---\ntype: observation\ntitle: Existing\nproject: core\nsource: pi\ntags: []\n"
        "entities: []\ngenerated:\n  at: '2026-08-01T00:00:00Z'\n---\n"
        "# Existing\n\nAlready indexed before the race begins.\n"
    )
    assert app(["--corpus", str(corpus), "index"]) == 0

    # A second entry, pending but not yet promoted -- the one the race
    # threatens.
    entry_id = _remember(tmp_path, monkeypatch,
                          "Racy fact that must not be silently lost by a concurrent rebuild.")
    config, embedder, store, lexical = _engine_pieces(corpus)

    # #30 P2a: the rebuild no longer calls lexical.drop() -- it stages a
    # fresh release instead. The synchronization point is now the FIRST
    # write into the staged release (write_manifest inside the candidate
    # directory), which is inside cmd_index's write-lock critical section --
    # the property under test is unchanged: a concurrent promote must defer.
    from alexandria import cli as cli_mod

    reached_drop = threading.Event()
    go_ahead = threading.Event()
    original_write = cli_mod.write_manifest

    def paused_write(*args, **kwargs):
        if "index_dir" in kwargs:  # only the rebuild's staged write pauses
            reached_drop.set()
            assert go_ahead.wait(timeout=10), "test synchronization stalled waiting for go_ahead"
        return original_write(*args, **kwargs)

    # patch the name cli.py actually CALLS (bound at import time into its own
    # namespace), not the module attribute -- the classic import-binding trap
    monkeypatch.setattr(cli_mod, "write_manifest", paused_write)

    index_result: list[int] = []

    def run_rebuild():
        index_result.append(app(["--corpus", str(corpus), "index", "--rebuild"]))

    thread = threading.Thread(target=run_rebuild)
    thread.start()
    try:
        assert reached_drop.wait(timeout=15), "rebuild never reached its staged write -- lock wiring changed?"

        # The dangerous window: reached exactly where the old code would have
        # let a concurrent promote's FTS insert land before the drop wipes it.
        promote_result = promote_pending(corpus, config, embedder, store, lexical)
    finally:
        go_ahead.set()
        thread.join(timeout=30)

    assert index_result == [0], f"rebuild did not exit 0: {index_result}"
    assert promote_result.skipped_locked is True, (
        "promote ran concurrently with the rebuild instead of deferring -- "
        "cmd_index is not holding the write lock across its critical section")
    assert promote_result.promoted == [], "a skipped promote must not report anything promoted"
    assert is_pending(corpus, entry_id), (
        "the marker was consumed even though promote reported skipped_locked -- "
        "it would now be permanently un-retried")

    # No data loss: the entry is still retryable, and actually retrying it
    # (lock now free) succeeds and lands real FTS rows.
    promote_result2 = promote_pending(corpus, config, embedder, store, lexical)
    assert entry_id in promote_result2.promoted
    assert not is_pending(corpus, entry_id)
    rows = lexical.connection.execute(
        "SELECT chunk_id FROM chunk_metadata WHERE source = 'inbox'").fetchall()
    assert rows, "promoted entry has no FTS rows after the deferred retry"


# --------------------------------------------------------------------------
# 2. WriteLock.acquire(blocking=..., timeout=...) mechanism, in isolation.
# --------------------------------------------------------------------------

def test_default_acquire_remains_non_blocking_for_the_drain(tmp_path):
    """Done-criterion #2: drain behaviour must be unchanged -- the default,
    no-argument acquire() must still fail INSTANTLY when the lock is held,
    never wait."""
    first = write_lock(tmp_path)
    assert first.acquire() is True
    try:
        second = write_lock(tmp_path)
        started = time.monotonic()
        acquired = second.acquire()
        elapsed = time.monotonic() - started
    finally:
        first.release()
    assert acquired is False
    assert elapsed < 0.2, f"default acquire() took {elapsed:.3f}s -- it must not wait at all"


def test_blocking_acquire_waits_for_a_release_then_succeeds(tmp_path):
    first = write_lock(tmp_path)
    assert first.acquire() is True

    def releaser():
        time.sleep(0.3)
        first.release()

    threading.Thread(target=releaser).start()

    second = write_lock(tmp_path)
    started = time.monotonic()
    acquired = second.acquire(blocking=True, timeout=5.0)
    elapsed = time.monotonic() - started
    if acquired:
        second.release()

    assert acquired is True, "blocking acquire gave up even though the lock was released in time"
    assert elapsed >= 0.25, (
        f"returned in {elapsed:.3f}s, before the 0.3s release -- it did not actually wait, "
        f"it must have raced or misreported success")


def test_blocking_acquire_requires_a_positive_timeout(tmp_path):
    lock = write_lock(tmp_path)
    with pytest.raises(ValueError):
        lock.acquire(blocking=True)
    with pytest.raises(ValueError):
        lock.acquire(blocking=True, timeout=0)
    with pytest.raises(ValueError):
        lock.acquire(blocking=True, timeout=-1)


def test_blocking_acquire_times_out_and_reports_the_holder_pid(tmp_path):
    holder = write_lock(tmp_path)
    assert holder.acquire() is True
    try:
        contender = write_lock(tmp_path)
        started = time.monotonic()
        acquired = contender.acquire(blocking=True, timeout=0.3)
        elapsed = time.monotonic() - started

        assert acquired is False
        assert elapsed >= 0.25, f"gave up in {elapsed:.3f}s, faster than the 0.3s timeout"
        assert elapsed < 3.0, f"took {elapsed:.3f}s to give up on a 0.3s timeout -- it hung"
        # Two open()s on the same path model two processes (same convention as
        # test_writelock.py); the pid recorded is still this test process's,
        # but proves the mechanism reads back what the holder wrote.
        assert contender.holder_pid() == str(os.getpid())
    finally:
        holder.release()


# --------------------------------------------------------------------------
# 3. cmd_index CLI wiring: waits, then fails loudly and non-zero, names the
#    holder. Run 8x (a fresh tmp corpus each time) -- concurrency/timing
#    tests are exactly the kind that can pass once and lie.
# --------------------------------------------------------------------------

def test_index_cli_fails_loudly_and_nonzero_naming_the_holder_run_8x(tmp_path, monkeypatch):
    from alexandria import cli
    monkeypatch.setattr(cli, "DEFAULT_LOCK_TIMEOUT", 0.3)

    successes = 0
    failures = []
    for i in range(8):
        corpus = tmp_path / f"corpus{i}"
        corpus.mkdir()
        holder = write_lock(corpus)
        assert holder.acquire() is True
        try:
            started = time.monotonic()
            with pytest.raises(SystemExit) as excinfo:
                app(["--corpus", str(corpus), "index"])
            elapsed = time.monotonic() - started
            message = str(excinfo.value)
            ok = (
                elapsed >= 0.25
                and elapsed < 5.0
                and "could not acquire" in message
                and str(os.getpid()) in message
            )
            if ok:
                successes += 1
            else:
                failures.append((i, elapsed, message))
        finally:
            holder.release()

    assert successes == 8, f"8 runs, {successes} clean, failures: {failures}"


def test_index_cli_succeeds_when_the_lock_is_released_before_the_timeout(tmp_path, monkeypatch):
    """The other half of 'waits, bounded': a lock that clears in time must
    let index proceed normally, not fail just because it had to wait."""
    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    from alexandria import cli
    monkeypatch.setattr(cli, "DEFAULT_LOCK_TIMEOUT", 5.0)
    corpus = tmp_path / "corpus"
    corpus.mkdir()

    holder = write_lock(corpus)
    assert holder.acquire() is True

    def releaser():
        time.sleep(0.3)
        holder.release()

    threading.Thread(target=releaser).start()

    started = time.monotonic()
    result = app(["--corpus", str(corpus), "index"])
    elapsed = time.monotonic() - started

    assert result == 0, "index must succeed once the briefly-held lock is released"
    assert elapsed >= 0.25, f"returned in {elapsed:.3f}s -- it did not actually wait for the lock"


@pytest.mark.skipif(os.name == "nt", reason="flock contract is POSIX-only")
def test_index_read_lock_excludes_a_writer_for_the_whole_read_epoch(tmp_path):
    """A reader either sees one coherent epoch or refuses before any lookup."""
    from alexandria.writelock import index_read_lock

    writer = write_lock(tmp_path)
    with index_read_lock(tmp_path):
        assert not writer.acquire(), "writer entered while a reader held the shared epoch lock"
    assert writer.acquire(), "writer did not resume after the reader released its snapshot"
    writer.release()


@pytest.mark.skipif(os.name == "nt", reason="flock contract is POSIX-only")
def test_rebuild_mutates_projections_in_place_so_warm_handles_stay_valid(tmp_path, monkeypatch):
    """Red round 2, condition 2: pin the file-identity assumption explicitly.

    The reader fence is safe partly BECAUSE a rebuild mutates the same files:
    BM25's long-lived connection pins an open-file description, so an in-place
    DELETE+refill is visible to it, while a write-temp-and-rename refactor
    would leave a warm server reading the OLD inode's lexical rows against the
    NEW dense vectors -- permanently, silently, with the marker cleared and the
    shared epoch held. That failure is invisible to every other test, so the
    assumption is pinned here rather than left as a comment.
    """
    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    corpus = tmp_path / "corpus"
    note = corpus / "sources" / "note.md"
    note.parent.mkdir(parents=True)
    note.write_text("---\nsource: test\n---\n\nledger reconciliation runs nightly\n")
    assert app(["--corpus", str(corpus), "index"]) == 0

    fts = corpus / ".alexandria" / "index" / "fts.sqlite"
    before = fts.stat()
    assert app(["--corpus", str(corpus), "index", "--rebuild"]) == 0
    after = fts.stat()

    assert (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino), (
        "--rebuild replaced the lexical projection file instead of mutating it; "
        "a warm server's open connection would now serve a superseded inode")
    assert not (corpus / ".alexandria" / "index" / ".rebuild-in-progress").exists(), (
        "a successful rebuild must clear its durable marker")


@pytest.mark.skipif(os.name == "nt", reason="flock contract is POSIX-only")
def test_reader_rides_out_a_brief_promote_but_still_refuses_a_long_writer(tmp_path):
    """The reader's wait absorbs a drain; it must never wait out a rebuild."""
    from alexandria.writelock import IndexReadUnavailable, index_read_lock

    holder = write_lock(tmp_path)
    assert holder.acquire()

    def release_soon():
        time.sleep(0.1)
        holder.release()

    threading.Thread(target=release_soon, daemon=True).start()
    with index_read_lock(tmp_path, retry_for=2.0):
        pass  # rode out the brief writer instead of refusing

    # Also exercise the SHIPPED default, not just a generous test value: the
    # 250ms budget has to absorb a promote-sized hold to be worth having.
    holder2 = write_lock(tmp_path)
    assert holder2.acquire()

    def release_promptly():
        time.sleep(0.05)
        holder2.release()

    threading.Thread(target=release_promptly, daemon=True).start()
    with index_read_lock(tmp_path):
        pass

    long_writer = write_lock(tmp_path)
    assert long_writer.acquire()
    try:
        started = time.monotonic()
        with pytest.raises(IndexReadUnavailable, match="writer"):
            index_read_lock(tmp_path, retry_for=0.2).acquire()
        assert time.monotonic() - started < 2.0, "reader waited far past its bound"
    finally:
        long_writer.release()
