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
            name = member.name
            if not any(name == prefix or name.startswith(prefix + "/") for prefix in allowed_prefixes):
                continue
            result.restored.append(name)
            if not dry_run:
                tar.extract(member, path=corpus, filter="data")
    return result
