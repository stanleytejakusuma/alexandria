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

import json
import posixpath
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["BackupResult", "RestoreResult", "backup_state", "restore_state", "STATE_PATHS"]

_GENERATION_MEMBER = ".alexandria/index/generation.json"


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
    # #6 erasure-core item 4: non-None only when the archive's generation.json
    # is OLDER than the corpus's current live generation -- restoring it would
    # let ResponseCache/QueryCache entries keyed to the intervening
    # generations look valid again once that generation number is reused by
    # write_index_generation() (the exact "stale answers resurface" failure
    # class cache.py's own read_index_generation docstring already names for
    # file corruption, now shown to apply to a restore too). None means
    # either the archive has no generation.json, the corpus has none yet
    # (nothing to be stale relative to), or the archive's generation is
    # current or newer (a normal forward restore).
    generation_regression: tuple[int, int] | None = None  # (archive_gen, corpus_gen)


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


def _archive_generation(tar: tarfile.TarFile) -> int | None:
    """Peek the archive's generation.json WITHOUT extracting it, so this
    check works identically in dry-run and real-restore mode. None if the
    archive has no such member or it fails to parse (fails open here -- a
    corrupt/absent generation in the ARCHIVE is not this function's failure
    to diagnose; restore's own per-member handling covers that separately)."""
    try:
        member = tar.getmember(_GENERATION_MEMBER)
    except KeyError:
        return None
    try:
        fh = tar.extractfile(member)
        if fh is None:
            return None
        return int(json.loads(fh.read())["generation"])
    except (json.JSONDecodeError, KeyError, ValueError, OSError):
        return None


def _corpus_generation(corpus: Path) -> int | None:
    """The corpus's OWN current generation, read directly (not via
    cache.read_index_generation, to avoid a cache.py<->backup.py import
    cycle and because this needs the exact same fail-open-to-None posture
    as _archive_generation, not that function's fail-loud-on-corruption
    contract -- a corrupt LIVE generation file is a separate, already-
    diagnosed problem (GenerationFileCorrupt), not this check's job)."""
    path = corpus / _GENERATION_MEMBER
    if not path.exists():
        return None
    try:
        return int(json.loads(path.read_text())["generation"])
    except (json.JSONDecodeError, KeyError, ValueError, OSError):
        return None


def restore_state(corpus: Path, archive_path: Path, *, dry_run: bool = False) -> RestoreResult:
    """Extract a backup_state() archive back into `corpus`, overwriting.

    Refuses to extract anything outside STATE_PATHS -- an archive that
    somehow smuggled an unexpected member (corrupted, hand-edited, or a
    future version writing extra paths) is trimmed to the known-safe set
    rather than trusted wholesale. This is also what makes a backup and
    restore of a DIFFERENT corpus directory safe: nothing outside
    `.alexandria/{queries.sqlite,audit,eval_runs*.jsonl,pending,
    liveness.json,index/generation.json}` can ever be written.

    #6 erasure-core item 4: BEFORE writing anything, compares the archive's
    generation.json against the corpus's current one. Restoring an OLDER
    generation number lets query/response cache entries keyed to the
    intervening generations look valid again once that number is reused --
    the exact "stale answers resurface" class cache.py's own docstring
    already names for file corruption, shown here to apply to an ordinary
    restore too (e.g. restoring a backup taken before a tombstone was
    applied and several reindexes ran). Detected, not prevented: a restore
    with a real, deliberate reason to go backward (disaster recovery from a
    corrupted corpus) must still be possible -- this surfaces the regression
    as an explicit, named fact on RestoreResult rather than silently
    allowing or silently blocking it.
    """
    corpus = Path(corpus)
    result = RestoreResult(dry_run=dry_run)
    allowed_prefixes = tuple(STATE_PATHS)
    with tarfile.open(archive_path, "r:gz") as tar:
        archive_gen = _archive_generation(tar)
        corpus_gen = _corpus_generation(corpus)
        if archive_gen is not None and corpus_gen is not None and archive_gen < corpus_gen:
            result.generation_regression = (archive_gen, corpus_gen)
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
