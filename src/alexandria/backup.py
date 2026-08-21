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
    # is OLDER than the corpus's current live generation. Red review
    # 2026-08-21 (finding #1): the counter is cache-key-freshness ONLY, never
    # an identity/ordering fact anything else depends on, so restore_state()
    # structurally REFUSES to write a regressing value (see
    # generation_preserved below) rather than warning after the fact. This
    # field remains as an operator-visible record of what would have
    # happened, not as the enforcement mechanism.
    generation_regression: tuple[int, int] | None = None  # (archive_gen, corpus_gen)
    # True when a regressing generation.json member was present in the
    # archive but deliberately NOT written, so the corpus's own current
    # (newer) generation value was left untouched instead.
    generation_preserved: bool = False


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


# #6 erasure-core, Red review 2026-08-21 (finding #6): a hostile/malformed
# generation.json member must not be able to blow up memory or CPU before
# any real extraction happens -- the peek below reads untrusted bytes BEFORE
# the existing allowlist/filter="data" extraction path would ever touch this
# member. A real generation.json is a few dozen bytes; anything past a
# generous margin is not a file this code needs to understand, it is
# refused the same as a parse failure.
_MAX_GENERATION_PEEK_BYTES = 4096


def _archive_generation(tar: tarfile.TarFile) -> int | None:
    """Peek the archive's generation.json WITHOUT extracting it, so this
    check works identically in dry-run and real-restore mode. None if the
    archive has no such member, the member is implausibly large, or it
    fails to parse (fails open here -- a corrupt/absent generation in the
    ARCHIVE is not this function's failure to diagnose; restore's own
    per-member handling covers that separately). Broad except: a crafted
    member (e.g. deeply nested JSON raising RecursionError) must degrade to
    "no generation info," never abort restore with a traceback the
    unpeeked extraction path would not have raised."""
    try:
        member = tar.getmember(_GENERATION_MEMBER)
    except KeyError:
        return None
    if member.size > _MAX_GENERATION_PEEK_BYTES:
        return None
    try:
        fh = tar.extractfile(member)
        if fh is None:
            return None
        return int(json.loads(fh.read())["generation"])
    except Exception:  # noqa: BLE001 -- fail open to "no generation info", never crash restore
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

    #6 erasure-core item 4, Red review 2026-08-21 (finding #1, the
    load-bearing one): the generation counter is used ONLY as a cache-key
    freshness stamp (verified live: read_index_generation appears nowhere
    outside cache.py and search.py's cache-key path) -- it carries no
    identity or ordering meaning a real disaster-recovery restore could
    ever need to go "backward" to. The correct fix is therefore structural,
    not a warning: generation.json is EXCLUDED from extraction whenever the
    archive's value is not strictly newer than the corpus's live value, and
    the live value is preserved as-is (never regressed, never silently
    bumped past what the corpus actually knows). This eliminates the "stale
    cache entries become valid again" failure class outright -- there is no
    detect-vs-block tradeoff to make, because the counter can no longer
    regress at all. Every OTHER restored path (queries.sqlite, audit,
    eval_runs, pending, liveness) still restores normally; this is scoped
    to exactly the one member whose value has ordering meaning.
    `generation_regression` on the result records what would have happened,
    for operator visibility, without ever letting it happen.
    """
    corpus = Path(corpus)
    result = RestoreResult(dry_run=dry_run)
    allowed_prefixes = tuple(STATE_PATHS)
    with tarfile.open(archive_path, "r:gz") as tar:
        archive_gen = _archive_generation(tar)
        corpus_gen = _corpus_generation(corpus)
        regresses = (archive_gen is not None and corpus_gen is not None
                    and archive_gen < corpus_gen)
        if regresses:
            result.generation_regression = (archive_gen, corpus_gen)
        for member in tar.getmembers():
            if not _is_allowed_member(member.name, allowed_prefixes):
                # Trimming is deliberate (see docstring), but silence is not:
                # a restore that dropped members reported the same "restored N
                # paths" as a clean one, so the operator could not tell a good
                # backup from a tampered or truncated one.
                result.skipped.append(member.name)
                continue
            if regresses and posixpath.normpath(member.name) == _GENERATION_MEMBER:
                # Structural fix: never write a regressing generation value.
                # Reported separately from `skipped` (that list means "outside
                # the allowlist, untrusted"; this member IS trusted, it is
                # simply not applied, which is a different fact worth a
                # different bucket so an operator scanning `skipped` for
                # tampering evidence does not see an unrelated, benign entry).
                result.generation_preserved = True
                continue
            result.restored.append(member.name)
            if not dry_run:
                # filter="data" is a second, independent line of defence
                # (symlink/hardlink/absolute-path escapes); the allowlist above
                # is the first and must stand on its own.
                tar.extract(member, path=corpus, filter="data")
    return result
