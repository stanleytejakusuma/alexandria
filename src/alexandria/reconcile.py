"""§7.1 mitigation 2: the independent observer that does not trust the marker.

Oldest-pending-age (§7) assumes the pending marker exists. If `remember`
appends to `inbox/<date>.md` but fails to write the pending file -- a crash
between the two writes, a full disk, a permission error on
`.alexandria/pending/` -- then nothing is pending, the system reports
perfect health, and the fact is stranded forever. That is the original
three-day weekly-loop outage wearing a new hat: a healthy signal that is
healthy *because* the work never got recorded.

This module derives health from the artifacts instead: it compares inbox
entries against promoted documents directly, an invariant checkable from the
two sources of truth alone -- "every entry in inbox/*.md has a corresponding
document in sources/inbox/". An unpromoted entry that IS correctly marked
pending is normal backlog waiting for the next drain cycle, not a fault;
conflating the two would make this command report "unhealthy" on every
ordinary corpus with unpromoted work queued, training operators to ignore it
-- the same cry-wolf failure mode this project has repeatedly found in
monitoring that reports success on unverified assumptions. Stranded entries
are (re)queued and reported; normal-pending entries are counted separately
and do not affect health.

But a marker only downgrades severity **while it is still young enough to be
plausible** (F6, amended 2026-08-13). On that date nine entries sat marked
pending for 3.3 hours because nothing implemented the 600s drain they were
waiting for; a boolean marker check called every one of them healthy backlog.
So the rule is a time bound against the same threshold §7's health surface
already applies (`liveness.WARN_MULTIPLE * DEFAULT_DRAIN_INTERVAL_SECONDS`):
unpromoted with no marker is stranded; unpromoted with a marker older than
that is *also* stranded; unpromoted with a fresh marker is ordinary backlog.
A stale marker is left exactly as it is -- refreshing its mtime would reset
the only clock that detected the fault, so `requeued` reports only markers
this run actually created.

Must not inherit `parse_inbox_file`'s blindness: that function swallows
OSError/UnicodeDecodeError and returns [], which would make an unreadable
inbox file's entries vacuously satisfy the invariant while sitting stranded
-- the exact flaw this module exists to close, reintroduced one level down
through a shared parser. This module therefore uses
`read_inbox_file_strict`, which raises instead, and counts unreadable FILES
as a hard error, not silence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .connectors.base import RawItem
from .connectors.inbox import InboxConnector, read_inbox_file_strict
from .liveness import DEFAULT_DRAIN_INTERVAL_SECONDS, WARN_MULTIPLE
from .pending import create_pending, pending_age
from .writelock import DEFAULT_LOCK_TIMEOUT, write_lock

__all__ = ["ReconcileReport", "STALE_MARKER_SECONDS", "reconcile_inbox"]

# F6: past this, a pending marker is no longer evidence that anything is
# tracking the entry. Same threshold, same constants, as liveness.check().
STALE_MARKER_SECONDS = WARN_MULTIPLE * DEFAULT_DRAIN_INTERVAL_SECONDS


@dataclass
class ReconcileReport:
    total_entries: int = 0
    total_files: int = 0
    stranded: list[str] = field(default_factory=list)       # no doc AND no live marker (missing, or older than STALE_MARKER_SECONDS)
    requeued: list[str] = field(default_factory=list)       # stranded ids whose marker this run created
    already_pending: list[str] = field(default_factory=list)  # no doc but freshly marked pending -- normal backlog
    unreadable_files: list[str] = field(default_factory=list)  # hard errors, never swallowed

    @property
    def healthy(self) -> bool:
        return not self.stranded and not self.unreadable_files


def reconcile_inbox(corpus: str | Path, *, requeue: bool = True) -> ReconcileReport:
    """Walk every inbox/*.md file directly (never through the swallowing
    parser) and verify each entry has a promoted document. A stranded entry
    is re-queued regardless of its current pending-marker state -- the whole
    point is to not trust that state."""
    corpus = Path(corpus).expanduser()
    inbox_dir = corpus / "inbox"
    report = ReconcileReport()
    if not inbox_dir.is_dir():
        return report

    # #30 cross-writer integrity (2026-08-23): reconcile writes pending
    # markers (create_pending) -- the exact state promote_pending consumes
    # under its own write lock.  Hold the same lock, bounded and loud, so a
    # reconcile can never interleave with a drain mid-discover.
    lock = write_lock(corpus)
    if not lock.acquire(blocking=True, timeout=DEFAULT_LOCK_TIMEOUT):
        holder = lock.holder_pid()
        raise RuntimeError(
            f"reconcile could not acquire the corpus write lock within "
            f"{DEFAULT_LOCK_TIMEOUT:.0f}s (held by {holder or 'an unknown process'}); "
            f"nothing was requeued -- wait for the current writer and retry")
    try:
        return _reconcile_inbox_locked(corpus, inbox_dir, report, requeue=requeue)
    finally:
        lock.release()


def _reconcile_inbox_locked(corpus: Path, inbox_dir: Path, report, *, requeue: bool) -> ReconcileReport:
    """Reconcile body, run with the corpus write lock held (see above)."""
    conn = InboxConnector(inbox_dir=inbox_dir)
    for path in sorted(inbox_dir.glob("*.md")):
        report.total_files += 1
        try:
            entries = read_inbox_file_strict(path)
        except (OSError, UnicodeDecodeError) as exc:
            report.unreadable_files.append(f"{path.name}: {exc}")
            continue
        for entry in entries:
            report.total_entries += 1
            item = RawItem(
                source_id=entry.entry_id,
                content=entry.text,
                meta={"created": entry.created, "last": entry.last,
                      "harness": entry.harness, "session": entry.session,
                      "corrects": entry.corrects, "file": path.name},
            )
            docs = conn.normalize(item)
            promoted = all((corpus / doc.path).exists() for doc in docs)
            if promoted:
                continue
            age = pending_age(corpus, entry.entry_id)
            if age is not None and age <= STALE_MARKER_SECONDS:
                report.already_pending.append(entry.entry_id)
                continue
            report.stranded.append(entry.entry_id)
            # create_pending is O_CREAT|O_EXCL, so a stale marker survives
            # untouched and is not claimed as requeued -- the age IS the
            # evidence, and a report that quietly reset it would be the
            # "reported success while doing nothing" bug wearing a new hat.
            if requeue and create_pending(corpus, entry.entry_id):
                report.requeued.append(entry.entry_id)
    return report
