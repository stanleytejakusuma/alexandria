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

**`flock` requires a local filesystem.** It is advisory, and on NFS/SMB
mounts it is unreliable to the point of being a no-op. Since §5.8 explicitly
endorses NAS deployment, `assert_local_filesystem` is checked when a write
lock is first acquired for a corpus -- without it, a corpus mounted over the
network has no write lock at all and nothing says so.
"""

from __future__ import annotations

import fcntl
import platform
import subprocess
from pathlib import Path

__all__ = ["NotLocalFilesystem", "WriteLock", "assert_local_filesystem", "write_lock"]

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
        if resolved.startswith(mountpoint) and len(mountpoint) >= len(best_mount):
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
        if resolved.startswith(mountpoint) and len(mountpoint) >= len(best_mount):
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
    """`with WriteLock(corpus) as acquired:` -- acquired is False if another
    process already holds the lock; callers must skip mutation rather than
    block (§4.2: "the drain skips its run rather than blocking")."""

    def __init__(self, corpus: str | Path, *, check_filesystem: bool = True) -> None:
        self.corpus = Path(corpus).expanduser()
        self.path = self.corpus / ".alexandria" / "index" / ".write.lock"
        self._check_filesystem = check_filesystem
        self._fh = None

    def acquire(self) -> bool:
        if self._check_filesystem:
            assert_local_filesystem(self.corpus)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self._fh = fh
        return True

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
