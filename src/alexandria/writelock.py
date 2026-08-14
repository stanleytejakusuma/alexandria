"""§4.2 the write lock.

`fcntl.flock(fd, LOCK_EX | LOCK_NB)` on `.alexandria/index/.write.lock`, held
across the whole promote -> embed -> upsert -> FTS -> generation-bump section.

**Not an O_EXCL sentinel.** An O_EXCL lock file survives SIGKILL with no
owner recorded and no way to distinguish a live holder from a dead one, so
one hard kill wedges every future writer silently -- and §7's detector would
not report it. `flock` is released by the kernel when the holding process
dies, by any means.

The drain skips its run rather than blocking when the lock is held: the
weekly reconcile is the long job, and a skipped drain costs at most one
interval of freshness.

BACKLOG #50: `index` is the other caller and needs the opposite policy --
skip-silently is right for a periodic drain but wrong for a scheduled or
manual index run, where doing nothing while reporting success is the failure
mode this project keeps re-finding. `acquire()` defaults to the drain's
LOCK_NB behaviour; `index` opts into `acquire(blocking=True, timeout=...)`,
which polls up to a bounded deadline and then fails loudly, naming the
holder, instead of racing a concurrent promote/drain that never took the
same lock.

**`flock` requires a local filesystem.** It is advisory, and on NFS/SMB
mounts it is unreliable to the point of being a no-op. Since §5.8 explicitly
endorses NAS deployment, `assert_local_filesystem` is checked when a write
lock is first acquired for a corpus -- without it, a corpus mounted over the
network has no write lock at all and nothing says so.
"""

from __future__ import annotations

import fcntl
import os
import platform
import subprocess
import time
from pathlib import Path

__all__ = ["NotLocalFilesystem", "WriteLock", "assert_local_filesystem", "write_lock"]

# BACKLOG #50: how long a BLOCKING acquire() polls before giving up. A single
# promote cycle (embed a handful of pending entries, upsert, FTS write, bump,
# unlink) is seconds even under real load -- 30s is an order of magnitude of
# headroom above that, so an ordinary promote never trips it, while a genuinely
# wedged holder (crashed mid-lock, holding a dead flock -- which the kernel
# would have already released, so realistically a hung/looping process) does
# not hang a scheduled index run indefinitely. Chosen once here rather than
# duplicated at each blocking call site.
DEFAULT_LOCK_TIMEOUT = 30.0

# Poll granularity for the blocking path. flock() has no native timeout, so a
# bounded wait is a spin-loop of non-blocking attempts; short enough that the
# reported wait time is close to the real deadline, long enough not to burn
# a core busy-waiting on lock files that are typically held for seconds.
_POLL_INTERVAL = 0.05

# fs types known to make flock unreliable-to-no-op. Anything NOT in this set is
# treated as local -- deliberately permissive on unknown/unusual types (e.g. a
# CI runner's overlay fs) rather than blocking every filesystem this wasn't
# tested against; the check exists to catch the specific documented failure
# mode (NFS/SMB), not to become a filesystem allowlist.
_NETWORK_FS_TYPES = frozenset({"nfs", "nfs4", "smbfs", "cifs", "afpfs", "webdav"})

# The filesystem a given resolved path lives on cannot change within a
# process's lifetime, and this check would otherwise shell out to `mount` on
# every single promote cycle (including the inline per-/remember path).
# Cached per resolved path, once per process.
_checked_local: set[str] = set()


class NotLocalFilesystem(Exception):
    """The corpus lives on a filesystem where flock is unreliable or a no-op."""


def _is_under(resolved: str, mountpoint: str) -> bool:
    """True if `resolved` is at or beneath `mountpoint`, respecting path
    component boundaries.

    A bare startswith() is wrong: "/Volumes/Databank" starts with
    "/Volumes/Data", so a local disk mounted next to an NFS share inherits the
    share's filesystem type and the corpus is refused for a network filesystem
    it isn't on. Fail-closed, so this was an availability bug rather than a
    safety hole -- but a refusal nobody can explain is how a guard gets
    disabled wholesale."""
    if mountpoint == "/":
        return True
    mountpoint = mountpoint.rstrip("/")
    return resolved == mountpoint or resolved.startswith(mountpoint + "/")


def _fs_type_macos(path: Path) -> str | None:
    try:
        out = subprocess.run(["mount"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    resolved = str(path)
    best_mount, best_type = "", None
    for line in out.splitlines():
        # "/dev/disk3s1 on /Users (apfs, local, journaled)" -- mountpoint is
        # between " on " and " (", type is the first token inside the parens.
        if " on " not in line or "(" not in line:
            continue
        mountpoint = line.split(" on ", 1)[1].split(" (", 1)[0]
        if _is_under(resolved, mountpoint) and len(mountpoint) >= len(best_mount):
            paren = line[line.index("(") + 1:line.index(")")]
            best_mount, best_type = mountpoint, paren.split(",")[0].strip()
    return best_type


def _fs_type_linux(path: Path) -> str | None:
    try:
        mounts = Path("/proc/mounts").read_text()
    except OSError:
        return None
    resolved = str(path)
    best_mount, best_type = "", None
    for line in mounts.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        mountpoint, fstype = parts[1], parts[2]
        if _is_under(resolved, mountpoint) and len(mountpoint) >= len(best_mount):
            best_mount, best_type = mountpoint, fstype
    return best_type


def assert_local_filesystem(path: str | Path) -> None:
    """Refuse to proceed if `path` is on a filesystem where flock's protection
    is unreliable. Unknown/undetectable filesystem types are allowed through --
    this guards the documented NFS/SMB failure mode, not an allowlist.
    Cached per resolved path for the life of the process."""
    resolved = str(Path(path).expanduser().resolve())
    if resolved in _checked_local:
        return
    system = platform.system()
    if system == "Darwin":
        fs_type = _fs_type_macos(Path(resolved))
    elif system == "Linux":
        fs_type = _fs_type_linux(Path(resolved))
    else:
        fs_type = None
    if fs_type is not None and fs_type.lower() in _NETWORK_FS_TYPES:
        raise NotLocalFilesystem(
            f"{resolved} is on a {fs_type} mount -- flock is advisory and "
            f"unreliable to the point of being a no-op on network filesystems, "
            f"so the write lock (SPEC §4.2) would not actually protect "
            f"concurrent writers. Move the corpus to local storage, or run "
            f"`alexandria serve` on the machine that holds the disk and reach "
            f"it over the network instead of mounting the corpus remotely.")
    _checked_local.add(resolved)


class WriteLock:
    """`with WriteLock(corpus) as acquired:` -- LOCK_NB by default: acquired is
    False immediately if another process already holds the lock, and callers
    must skip mutation rather than block (§4.2: "the drain skips its run
    rather than blocking"). That is the DRAIN's policy, not the only one.

    BACKLOG #50: a periodic drain can safely skip a tick, but a manual or
    scheduled `index` run that silently did nothing because the lock was busy
    is the exact failure this project keeps finding -- a step reporting
    success (exit 0) while doing nothing. `index` therefore calls
    `acquire(blocking=True, timeout=...)` instead: wait, bounded, then fail
    loudly (non-zero exit, names the holder) rather than skip or race. Two
    callers, two deliberate policies, both explicit at the call site -- there
    is no third, implicit default.
    """

    def __init__(self, corpus: str | Path, *, check_filesystem: bool = True) -> None:
        self.corpus = Path(corpus).expanduser()
        self.path = self.corpus / ".alexandria" / "index" / ".write.lock"
        self._check_filesystem = check_filesystem
        self._fh = None

    def acquire(self, *, blocking: bool = False, timeout: float | None = None) -> bool:
        """Non-blocking (default): try once, return False immediately if held.

        blocking=True: poll until acquired or `timeout` seconds elapse (must be
        a positive number -- an unbounded wait is exactly what BACKLOG #50
        found unacceptable for a scheduled job, so it is not offered here).
        """
        if blocking and (timeout is None or timeout <= 0):
            raise ValueError("blocking=True requires a positive timeout")
        if self._check_filesystem:
            assert_local_filesystem(self.corpus)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+")
        deadline = time.monotonic() + timeout if blocking else None
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if deadline is None or time.monotonic() >= deadline:
                    fh.close()
                    return False
                time.sleep(_POLL_INTERVAL)
        self._fh = fh
        self._record_holder()
        return True

    def _record_holder(self) -> None:
        """Best-effort: stamp our own pid into the lock file so a caller that
        later fails to acquire (blocking, timed out) can name who has it. Only
        the flock holder ever writes here, so this is safe by convention, not
        by a second lock -- and failing to write it must not fail acquisition."""
        try:
            self._fh.seek(0)
            self._fh.truncate()
            self._fh.write(str(os.getpid()))
            self._fh.flush()
        except OSError:
            pass

    def holder_pid(self) -> str | None:
        """Best-effort pid of whoever last held (or holds) the lock, for a
        diagnostic message. Not authoritative: does not confirm the pid is
        still alive or still the current holder, just the last writer."""
        try:
            text = self.path.read_text().strip()
        except OSError:
            return None
        return text or None

    def release(self) -> None:
        if self._fh is not None:
            fcntl.flock(self._fh, fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *exc_info) -> None:
        self.release()


def write_lock(corpus: str | Path, *, check_filesystem: bool = True) -> WriteLock:
    return WriteLock(corpus, check_filesystem=check_filesystem)
