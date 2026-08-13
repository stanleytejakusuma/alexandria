"""§6 backup/restore: `.alexandria` STATE, never the rebuildable indexes.

Scope is deliberately narrow. `chunks.lance` (2.2GB) and `fts.sqlite`
(644MB) are excluded on purpose -- they are 100% reconstructible from
`sources/`/`wiki/` via `alexandria index --rebuild`, so backing them up
would only multiply storage cost for zero unique information. What IS
backed up here cannot be reconstructed from anything else:

- queries.sqlite   -- 2,377+ logged queries, the training signal for any
                       future learning loop (§8's closed-loop tuning).
- audit/*.jsonl     -- the search/answer audit trail.
- eval_runs.jsonl   -- regression history; losing it blinds `--fail-on-regression`.
- pending/          -- this package's own new state: unconsumed write markers.
                       Losing these silently strands facts (the exact §7.1 gap).
- liveness.json     -- this package's own new state: the freshness signal.
- index/generation.json -- the cache-invalidation counter.

A single portable .tar.gz, stdlib `tarfile` only (matches serve.py's
no-new-dependency constraint). Restore overwrites in place; callers that
want a dry run pass dry_run=True and get the file list without writing.
"""

from __future__ import annotations

import posixpath
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["BackupResult", "RestoreResult", "backup_state", "restore_state", "STATE_PATHS"]


# Relative to the corpus root. Files are backed up if present; directories
# are walked recursively. Order doesn't matter -- tarfile handles both.
STATE_PATHS = (
    ".alexandria/queries.sqlite",
    ".alexandria/audit",
    ".alexandria/eval_runs.jsonl",
    ".alexandria/eval_runs.invalid.jsonl",
    ".alexandria/pending",
    ".alexandria/liveness.json",
    ".alexandria/index/generation.json",
)


@dataclass
class BackupResult:
    archive_path: Path
    included: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)  # present in STATE_PATHS, absent on disk -- not an error


@dataclass
class RestoreResult:
    restored: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    dry_run: bool = False


def backup_state(corpus: Path, archive_path: Path) -> BackupResult:
    """Write a .tar.gz of every present STATE_PATHS entry.

    An entry that doesn't exist yet (a fresh corpus with no pending
    entries, say) is recorded in `missing`, not treated as a failure --
    absence of optional state is normal, not corruption.
    """
    corpus = Path(corpus)
    archive_path = Path(archive_path)
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    result = BackupResult(archive_path=archive_path)
    with tarfile.open(archive_path, "w:gz") as tar:
        for rel in STATE_PATHS:
            full = corpus / rel
            if full.exists():
                tar.add(full, arcname=rel)
                result.included.append(rel)
            else:
                result.missing.append(rel)
    return result


def _is_allowed_member(name: str, allowed_prefixes: tuple[str, ...]) -> bool:
    """Whether a tar member name may be written, judged on its NORMALISED form.

    Matching the raw name is not enough: `.alexandria/pending/../../../etc/x`
    starts with an allowlisted prefix and would pass, while resolving to a path
    outside the corpus entirely. Normalising first collapses the `..` before the
    prefix is tested, so traversal THROUGH a permitted prefix is rejected by the
    allowlist itself rather than relying on tarfile's `filter="data"` to catch
    it downstream. Defence in depth means each layer holds alone.
    """
    if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        return False  # absolute (posix) or drive-qualified (windows)
    normalised = posixpath.normpath(name)
    if normalised.startswith("../") or normalised == ".." or normalised.startswith("/"):
        return False
    return any(normalised == prefix or normalised.startswith(prefix + "/")
               for prefix in allowed_prefixes)


def restore_state(corpus: Path, archive_path: Path, *, dry_run: bool = False) -> RestoreResult:
    """Extract a backup_state() archive back into `corpus`, overwriting.

    Refuses to extract anything outside STATE_PATHS -- an archive that
    somehow smuggled an unexpected member (corrupted, hand-edited, or a
    future version writing extra paths) is trimmed to the known-safe set
    rather than trusted wholesale. This is also what makes a backup and
    restore of a DIFFERENT corpus directory safe: nothing outside
    `.alexandria/{queries.sqlite,audit,eval_runs*.jsonl,pending,
    liveness.json,index/generation.json}` can ever be written.
    """
    corpus = Path(corpus)
    result = RestoreResult(dry_run=dry_run)
    allowed_prefixes = tuple(STATE_PATHS)
    with tarfile.open(archive_path, "r:gz") as tar:
        for member in tar.getmembers():
            if not _is_allowed_member(member.name, allowed_prefixes):
                # Trimming is deliberate (see docstring), but silence is not:
                # a restore that dropped members reported the same "restored N
                # paths" as a clean one, so the operator could not tell a good
                # backup from a tampered or truncated one.
                result.skipped.append(member.name)
                continue
            result.restored.append(member.name)
            if not dry_run:
                # filter="data" is a second, independent line of defence
                # (symlink/hardlink/absolute-path escapes); the allowlist above
                # is the first and must stand on its own.
                tar.extract(member, path=corpus, filter="data")
    return result
