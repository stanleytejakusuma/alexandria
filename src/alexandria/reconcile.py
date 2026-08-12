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
document in sources/inbox/". An unpromoted entry is only reported as
*stranded* when it ALSO has no pending marker -- that combination is the
actual failure signature (nothing is tracking it, by any mechanism). An
unpromoted entry that IS correctly marked pending is normal backlog waiting
for the next drain cycle, not a fault; conflating the two would make this
command report "unhealthy" on every ordinary corpus with unpromoted work
queued, training operators to ignore it -- the same cry-wolf failure mode
this project has repeatedly found in monitoring that reports success on
unverified assumptions. Stranded entries are (re)queued and reported;
normal-pending entries are counted separately and do not affect health.

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
from .pending import create_pending, is_pending

__all__ = ["ReconcileReport", "reconcile_inbox"]


@dataclass
class ReconcileReport:
    total_entries: int = 0
    total_files: int = 0
    stranded: list[str] = field(default_factory=list)       # no doc AND no pending marker
    requeued: list[str] = field(default_factory=list)       # stranded ids now (re)marked pending
    already_pending: list[str] = field(default_factory=list)  # no doc but correctly marked pending -- normal backlog
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
            if is_pending(corpus, entry.entry_id):
                report.already_pending.append(entry.entry_id)
                continue
            report.stranded.append(entry.entry_id)
            if requeue:
                create_pending(corpus, entry.entry_id)
                report.requeued.append(entry.entry_id)
    return report
