"""The pending list: a directory of zero-length marker files.

SPEC-write-path-and-serve.md §4.1 / §7. `remember` writes an entry id here;
promotion consumes it. This is the input to the liveness signal (§7) and the
redo log for crash recovery (§4.2.1 step 5) -- so its format is specified
rather than implied:

- one zero-length file per unpromoted entry, named by entry id
- create with O_CREAT | O_EXCL (fails if already pending -- idempotent
  `remember` retries never double-queue the same entry)
- consume with unlink
- both operations are atomic in the kernel, so a drain scanning the
  directory while `remember` writes cannot observe a torn entry, and no
  lock is needed between the two

A single file holding a list would need its own lock and could be truncated
mid-write by a crash; a SQLite table would add a second store to keep
consistent with the first. The directory is the lazy option that is also the
correct one.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

__all__ = ["create_pending", "is_pending", "list_pending", "oldest_pending_age",
           "pending_dir", "unlink_pending"]


def pending_dir(corpus: str | Path) -> Path:
    return Path(corpus).expanduser() / ".alexandria" / "pending"


def create_pending(corpus: str | Path, entry_id: str) -> bool:
    """Mark entry_id as pending. Returns True if newly created, False if it
    was already pending (O_CREAT|O_EXCL makes this safe to call twice)."""
    directory = pending_dir(corpus)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / entry_id
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    os.close(fd)
    return True


def unlink_pending(corpus: str | Path, entry_id: str) -> bool:
    """Consume entry_id. Returns True if it was pending, False if already gone
    (idempotent -- unlinking twice, e.g. on a promote rerun, is not an error)."""
    path = pending_dir(corpus) / entry_id
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


def is_pending(corpus: str | Path, entry_id: str) -> bool:
    return (pending_dir(corpus) / entry_id).exists()


def list_pending(corpus: str | Path) -> list[str]:
    """Entry ids currently pending, oldest first (by mtime)."""
    directory = pending_dir(corpus)
    if not directory.exists():
        return []
    # stat() after iterdir() is a TOCTOU against a drain consuming markers:
    # the marker is the redo log, so it is unlinked concurrently by design.
    # A vanished entry is not an error, it is the normal success case.
    entries: list[tuple[float, str]] = []
    for p in directory.iterdir():
        try:
            if p.is_file():
                entries.append((p.stat().st_mtime, p.name))
        except FileNotFoundError:
            continue
    entries.sort()
    return [name for _, name in entries]


def oldest_pending_age(corpus: str | Path, *, now: float | None = None) -> float | None:
    """Seconds since the oldest still-pending entry was created, or None if
    nothing is pending -- the primary liveness signal (§7). A single scandir,
    no parsing, nothing to corrupt."""
    directory = pending_dir(corpus)
    if not directory.exists():
        return None
    oldest: float | None = None
    for entry in directory.iterdir():
        try:
            if not entry.is_file():
                continue
            mtime = entry.stat().st_mtime
        except FileNotFoundError:
            # Consumed mid-scan by a concurrent drain. This is the liveness
            # path (§7) -- it must never raise, or the health signal dies
            # exactly when the system is busiest.
            continue
        if oldest is None or mtime < oldest:
            oldest = mtime
    if oldest is None:
        return None
    return (now if now is not None else time.time()) - oldest
