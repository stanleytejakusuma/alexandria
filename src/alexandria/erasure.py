"""Git-history erasure for one Alexandria source path.

``alexandria erase`` is deliberately separate from a normal tombstone.  It
removes a *current source path* from the active corpus Git history.  It does
not claim physical-media erasure: the ratified retention policy keeps a
complete pre-erase Git database as a manual-recovery backup, and does not
reach remotes, filesystem snapshots, or external backups.

Safety boundary
---------------
``git filter-repo`` never runs in the live corpus.  Alexandria first creates a
same-filesystem disposable mirror, rewrites and validates that mirror, then
performs a two-rename cutover.  Two renames cannot be crash-atomic together,
so a durable transaction marker is written before the first rename.  A later
``erase`` invocation recovers an interrupted cutover before doing any new
work: it restores the original Git directory if the second rename never
landed, or completes the narrowly-scoped work-tree synchronization when the
rewritten Git directory did land.

The supported repository shape is intentionally narrow.  A corpus must have a
single checked-out branch, no remotes, no active linked worktrees, and no
non-sample hooks.  Extra refs/tags/stashes are refused rather than silently
lost.  Untracked corpus operational state (notably ``.alexandria/``) is
allowed and never cleaned.  Tracked or staged edits are refused before any
mutation.

The erasure contract is *path-history erasure*.  Historical renames/copies and
an exact blob shared under another path are refused, because filtering only
the current path would otherwise overclaim raw-text removal.  This command
does not scan arbitrary quotations, links, or semantic copies in other docs.
"""

from __future__ import annotations

import collections
import datetime as _dt
import json as _json
import os
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

__all__ = [
    "ErasePreflight",
    "EraseResult",
    "GitEraseError",
    "erase_from_git_history",
    "impact_report",
    "preflight_git_erase",
    "recover_interrupted_erase",
]


_TXN_FILE = "erase-txn.json"


class GitEraseError(Exception):
    """A Git-history erasure step failed.

    ``history_changed`` is true only when the rewritten Git directory has
    already become active.  Callers must not report that history is unchanged
    in that case; the durable transaction marker remains for recovery.
    """

    def __init__(self, message: str, *, history_changed: bool = False) -> None:
        super().__init__(message)
        self.history_changed = history_changed


@dataclass(frozen=True)
class ErasePreflight:
    """Validated, immutable facts captured before an erase mutates a corpus."""

    rel_path: str
    path_touching_commits: int
    head_ref: str
    head_oid: str | None
    refs: tuple[str, ...]
    user_name: str | None
    user_email: str | None
    target_blob_ids: tuple[str, ...]


@dataclass(frozen=True)
class EraseResult:
    """Outcome of a completed path-history rewrite.

    ``path_touching_commits`` is the number of commits whose tree contained
    the removed path.  ``filter-repo`` necessarily assigns fresh IDs to every
    reachable descendant too, so this is deliberately not called a count of
    every commit ID rewritten.
    """

    path_touching_commits: int
    backup_git_dir: Path | None
    history_rewritten: bool


def _run_git(
    args: list[str], *, cwd: Path, timeout: float = 120.0
) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise GitEraseError("git is not installed or not on PATH") from exc
    except subprocess.SubprocessError as exc:
        raise GitEraseError(f"git {' '.join(args)} failed to run: {exc}") from exc


def _require_success(result: subprocess.CompletedProcess, *, action: str) -> str:
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise GitEraseError(f"{action} failed: {detail}")
    return result.stdout


def _normalise_rel_path(rel_path: str) -> str:
    pure = PurePosixPath(rel_path)
    if not rel_path or pure.is_absolute() or ".." in pure.parts or str(pure) != rel_path:
        raise GitEraseError(f"erase path must be a normalized corpus-relative path, got {rel_path!r}")
    return rel_path


def _commit_count_for_path(repo: Path, rel_path: str) -> int:
    """Count reachable commits whose tree touched ``rel_path``."""
    out = _run_git(["log", "--oneline", "--all", "--", rel_path], cwd=repo)
    if out.returncode != 0:
        raise GitEraseError(f"git log failed: {out.stderr.strip()}")
    return sum(1 for line in out.stdout.splitlines() if line.strip())


def _local_config(repo: Path, key: str) -> str | None:
    out = _run_git(["config", "--local", "--get", key], cwd=repo)
    if out.returncode == 0:
        return out.stdout.rstrip("\n")
    if out.returncode == 1:
        return None
    raise GitEraseError(f"could not read local Git config {key}: {out.stderr.strip()}")


def _list_refs(repo: Path) -> tuple[str, ...]:
    out = _run_git(["for-each-ref", "--format=%(refname)"], cwd=repo)
    return tuple(line for line in _require_success(out, action="listing Git refs").splitlines() if line)


def _target_blob_ids(repo: Path, rel_path: str) -> tuple[str, ...]:
    """Reachable blob IDs ever presented at the exact current path."""
    out = _run_git(["rev-list", "--objects", "--all", "--", rel_path], cwd=repo)
    text = _require_success(out, action="enumerating target blobs")
    blobs: set[str] = set()
    for line in text.splitlines():
        object_id, sep, object_path = line.partition(" ")
        if sep and object_path == rel_path and object_id:
            blobs.add(object_id)
    return tuple(sorted(blobs))


def _repo_has_no_commits(repo: Path) -> bool:
    """True for a symbolic unborn branch: the supported zero-history shape."""
    head = _run_git(["rev-parse", "--verify", "HEAD"], cwd=repo)
    return head.returncode == 128


def _refuse_historical_aliases(repo: Path, rel_path: str) -> None:
    """Fail closed for rename/copy history that path filtering cannot cover."""
    if _repo_has_no_commits(repo):
        # A never-committed source has no history in which a rename/copy
        # alias could exist; the terminal synchronization step removes the
        # raw file itself.
        return
    out = _run_git(
        ["log", "--follow", "--name-status", "--format=", "--", rel_path], cwd=repo
    )
    text = _require_success(out, action="checking target rename history")
    aliases: set[str] = set()
    for line in text.splitlines():
        fields = line.split("\t")
        if len(fields) == 3 and fields[0][:1] in {"R", "C"}:
            old_path, new_path = fields[1:]
            if new_path == rel_path and old_path != rel_path:
                aliases.add(old_path)
    if aliases:
        listed = ", ".join(sorted(aliases))
        raise GitEraseError(
            "refusing path-history erase: Git reports historical rename/copy "
            f"alias(es) for {rel_path}: {listed}. Erase each historical path "
            "through an explicitly reviewed operation; this command will not "
            "claim to remove text it did not target."
        )


def _refuse_shared_blobs(repo: Path, rel_path: str, target_blob_ids: tuple[str, ...]) -> None:
    """Fail rather than leaving an exact target blob reachable elsewhere.

    ``git rev-list --objects`` deduplicates blobs by first-seen path, so a
    reachability scan would miss a duplicate path for an identical blob.
    The current tree is walked explicitly instead: the supported contract
    refuses an exact blob that is currently reachable under any path other
    than the erase target (historical renames/copies are covered by
    ``_refuse_historical_aliases``).
    """
    if not target_blob_ids or _repo_has_no_commits(repo):
        return
    target = set(target_blob_ids)
    out = _run_git(["ls-tree", "-r", "HEAD"], cwd=repo)
    text = _require_success(out, action="checking for shared target blobs in the current tree")
    shared: set[str] = set()
    for line in text.splitlines():
        meta, sep, object_path = line.partition("\t")
        if not sep:
            continue
        parts = meta.split()
        if len(parts) == 3 and parts[1] == "blob" and parts[2] in target and object_path != rel_path:
            shared.add(object_path)
    if shared:
        listed = ", ".join(sorted(shared))
        raise GitEraseError(
            "refusing path-history erase: exact target blob(s) are also reachable "
            f"under other path(s): {listed}. Review that blast radius explicitly; "
            "Alexandria will not silently retain the same raw blob elsewhere."
        )


def _check_supported_repo_shape(corpus: Path) -> tuple[str, tuple[str, ...]]:
    git_dir = corpus / ".git"
    if not git_dir.is_dir():
        raise GitEraseError(
            f"{corpus} is not a git repository with a supported standalone .git directory "
            "(linked worktrees are refused)"
        )
    top = _run_git(["rev-parse", "--show-toplevel"], cwd=corpus)
    if top.returncode != 0 or Path(top.stdout.strip()).resolve() != corpus.resolve():
        raise GitEraseError(f"{corpus} is not the top-level directory of its Git repository")

    status = _run_git(["status", "--porcelain=v1", "--untracked-files=no"], cwd=corpus)
    tracked_status = _require_success(status, action="checking tracked working-tree state")
    if tracked_status.strip():
        raise GitEraseError(
            "refusing erase while tracked or staged changes exist; commit, stash, or "
            "discard them first. Untracked corpus state is allowed and will not be cleaned."
        )

    head = _run_git(["symbolic-ref", "-q", "HEAD"], cwd=corpus)
    if head.returncode != 0:
        raise GitEraseError("refusing erase from a detached or unborn-unsymbolic HEAD")
    head_ref = head.stdout.strip()
    refs = _list_refs(corpus)
    if refs and refs != (head_ref,):
        raise GitEraseError(
            "refusing erase in a repository with refs beyond its checked-out branch "
            f"({', '.join(refs)}). The supported shape is exactly one branch, no tags, "
            "stashes, notes, or extra branches; export or simplify the repository first."
        )

    remotes = _require_success(_run_git(["remote"], cwd=corpus), action="checking Git remotes")
    if remotes.strip():
        raise GitEraseError(
            "refusing erase in a repository with configured remotes; active-history "
            "rewriting does not erase remote copies. Remove/export the remote under an "
            "explicit retention decision first."
        )

    worktrees = _require_success(
        _run_git(["worktree", "list", "--porcelain"], cwd=corpus), action="checking linked worktrees"
    )
    if sum(1 for line in worktrees.splitlines() if line.startswith("worktree ")) != 1:
        raise GitEraseError("refusing erase while linked Git worktrees exist")

    # Transaction journals, staging mirrors, and retained pre-erase Git
    # directories live below .alexandria.  That is safe only while the state
    # directory is untracked operational state, never corpus content.
    tracked_state = _require_success(
        _run_git(["ls-files", "--", ".alexandria"], cwd=corpus),
        action="checking that .alexandria is untracked operational state",
    )
    if tracked_state.strip():
        raise GitEraseError(
            "refusing erase because .alexandria is tracked by Git; transaction state "
            "must remain untracked so an erase never rewrites operational data into history"
        )

    hooks = git_dir / "hooks"
    if hooks.is_dir():
        active_hooks = [p.name for p in hooks.iterdir() if p.is_file() and not p.name.endswith(".sample")]
        if active_hooks:
            raise GitEraseError(
                "refusing erase in a repository with custom Git hooks "
                f"({', '.join(sorted(active_hooks))}); the supported shape has no hooks "
                "that a replacement .git directory could lose."
            )

    for name in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "BISECT_LOG", "rebase-merge", "rebase-apply"):
        if (git_dir / name).exists():
            raise GitEraseError(f"refusing erase during an active Git operation ({name})")
    return head_ref, refs


def _check_filter_repo() -> None:
    if shutil.which("git-filter-repo") is None:
        raise GitEraseError(
            "git-filter-repo is not installed. It is an optional external tool "
            "(like pdftotext for PDF ingest) -- install it with `brew install "
            "git-filter-repo` or `pip install git-filter-repo`, then retry. "
            "Nothing in the corpus was touched."
        )


def preflight_git_erase(corpus: Path, rel_path: str) -> ErasePreflight:
    """Fail closed on dependency, repository-shape, and blast-radius problems.

    This performs no mutation.  ``cmd_erase`` runs it once for preview and
    again while holding the corpus write lock before tombstoning anything.
    """
    corpus = Path(corpus).expanduser().resolve()
    rel_path = _normalise_rel_path(rel_path)
    # Repository-shape checks first: a non-repository, a dirty tree, or an
    # unsupported shape must be refused with its OWN diagnosis regardless of
    # whether the optional rewrite tool happens to be installed.
    head_ref, refs = _check_supported_repo_shape(corpus)
    head = _run_git(["rev-parse", "--verify", "HEAD"], cwd=corpus)
    if head.returncode == 0:
        head_oid: str | None = head.stdout.strip()
    elif head.returncode == 128 and not refs:
        # A symbolic unborn branch is supported for a never-committed source:
        # there is no history to rewrite, but the raw on-disk file must still
        # be removed by the terminal synchronization step.
        head_oid = None
    else:
        raise GitEraseError(f"could not resolve corpus HEAD: {head.stderr.strip()}")
    _refuse_historical_aliases(corpus, rel_path)
    target_blob_ids = _target_blob_ids(corpus, rel_path)
    _refuse_shared_blobs(corpus, rel_path, target_blob_ids)
    path_touching_commits = _commit_count_for_path(corpus, rel_path)
    if path_touching_commits:
        # The rewrite tool is only needed when history actually exists to
        # rewrite; a zero-history source removal never invokes it.
        _check_filter_repo()
    return ErasePreflight(
        rel_path=rel_path,
        path_touching_commits=path_touching_commits,
        head_ref=head_ref,
        head_oid=head_oid,
        refs=refs,
        user_name=_local_config(corpus, "user.name"),
        user_email=_local_config(corpus, "user.email"),
        target_blob_ids=target_blob_ids,
    )


def _tail_lines(path: Path, last: int) -> list[str]:
    """Read only the final ``last`` JSONL rows without loading the audit file."""
    if last <= 0:
        return []
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            pos = fh.tell()
            data = b""
            block = 64 * 1024
            while pos > 0 and data.count(b"\n") <= last:
                take = min(block, pos)
                pos -= take
                fh.seek(pos)
                data = fh.read(take) + data
    except OSError:
        return []
    if pos > 0:
        # Discard the partial oldest line introduced by the bounded read.
        _, _, data = data.partition(b"\n")
    return data.decode("utf-8", errors="replace").splitlines()[-last:]


def impact_report(corpus: Path, doc_id: str, *, last: int = 5000) -> list[str]:
    """Answer IDs citing ``doc_id`` in at most the most recent ``last`` rows.

    This is an audit-citation report, not backlink or reference-integrity
    analysis.  It deliberately reports a lower bound over a bounded recent
    audit window and does not mutate the retained audit trail.
    """
    from .auditlog import audit_log_dir

    answers_path = audit_log_dir(corpus) / "answers.jsonl"
    if not answers_path.exists():
        return []
    found: list[str] = []
    for line in _tail_lines(answers_path, last):
        if not line.strip():
            continue
        try:
            row = _json.loads(line)
        except _json.JSONDecodeError:
            continue
        citations = row.get("citations") or []
        if any(isinstance(c, dict) and c.get("doc_id") == doc_id for c in citations):
            found.append(str(row.get("id", "")))
    return found


def _transaction_root(corpus: Path) -> Path:
    """Untracked durable state for marker, staging, and retained backups."""
    return corpus / ".alexandria"


def _marker_path(corpus: Path) -> Path:
    return _transaction_root(corpus) / _TXN_FILE


def _fsync_directory(directory: Path) -> None:
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _write_marker(corpus: Path, payload: dict[str, object]) -> Path:
    marker = _marker_path(corpus)
    marker.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{_TXN_FILE}.", dir=marker.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            _json.dump(payload, fh, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, marker)
        _fsync_directory(marker.parent)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise
    return marker


def _remove_marker(corpus: Path) -> None:
    marker = _marker_path(corpus)
    try:
        marker.unlink()
    except FileNotFoundError:
        return
    _fsync_directory(marker.parent)


def _read_marker(corpus: Path) -> dict[str, object] | None:
    marker = _marker_path(corpus)
    if not marker.exists():
        return None
    try:
        payload = _json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, _json.JSONDecodeError) as exc:
        raise GitEraseError(
            f"found an unreadable interrupted-erase marker at {marker}; do not run a "
            "new erase until an operator inspects it"
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("rel_path"), str):
        raise GitEraseError(
            f"found an invalid interrupted-erase marker at {marker}; do not run a "
            "new erase until an operator inspects it"
        )
    return payload


def _validate_marker_backup(corpus: Path, payload: dict[str, object]) -> Path:
    raw = payload.get("backup_git_dir")
    if not isinstance(raw, str):
        raise GitEraseError("interrupted-erase marker lacks a safe backup location")
    backup = Path(raw)
    backup_root = _transaction_root(corpus) / "erase-backups"
    try:
        if backup.resolve().parent.parent != backup_root.resolve() or backup.name != "git":
            raise GitEraseError(
                "interrupted-erase marker names a backup outside the supported "
                ".alexandria/erase-backups/<generation>/git location"
            )
    except OSError as exc:
        raise GitEraseError("could not resolve interrupted-erase backup location") from exc
    return backup


def _sync_erased_worktree(corpus: Path, rel_path: str, *, timeout: float) -> None:
    """Install only the rewritten index and remove the one erased target.

    The clean tracked-tree preflight means every non-target worktree file is
    already equal to the rewritten HEAD tree.  ``git read-tree HEAD`` rebuilds
    the replacement Git directory's index *without writing those files*.  No
    repository-wide checkout or clean is used, so unrelated worktree and
    untracked ``.alexandria`` state cannot be overwritten or deleted.
    """
    head = _run_git(["rev-parse", "--verify", "HEAD"], cwd=corpus, timeout=timeout)
    if head.returncode == 0:
        read_tree = _run_git(["read-tree", "HEAD"], cwd=corpus, timeout=timeout)
        _require_success(read_tree, action="installing rewritten Git index after history rewrite")
    elif head.returncode != 128:
        raise GitEraseError(
            f"could not verify rewritten HEAD: {head.stderr.strip()}", history_changed=True
        )

    target = corpus / rel_path
    listed = _run_git(["ls-files", "--error-unmatch", "--", rel_path], cwd=corpus, timeout=timeout)
    if listed.returncode == 0:
        raise GitEraseError(
            f"rewritten history still tracks {rel_path}; refusing to leave a partial erase",
            history_changed=True,
        )
    if listed.returncode != 1:
        raise GitEraseError(
            f"could not determine whether {rel_path} is tracked after rewrite: {listed.stderr.strip()}",
            history_changed=True,
        )
    if target.exists() or target.is_symlink():
        if target.is_dir():
            raise GitEraseError(
                f"erased target {target} is unexpectedly a directory; refusing broad deletion",
                history_changed=True,
            )
        target.unlink()


def recover_interrupted_erase(corpus: Path, *, timeout: float = 600.0) -> str | None:
    """Recover a marker-backed interrupted cutover, if one exists.

    Phases: ``prepared`` (nothing moved, marker dropped), ``original-moved``
    (original renamed aside; a missing live ``.git`` is restored from the
    retained backup), and ``swapped`` / ``new_git_installed_needs_target_reconcile``
    (rewritten history active; only the erased target is reconciled, never a
    broad checkout).  Returns ``"rolled_back"`` when the original Git
    directory is (or remains) active, ``"completed"`` when rewritten history
    was active and its target reconciliation finished, otherwise ``None``.
    """
    corpus = Path(corpus).expanduser().resolve()
    payload = _read_marker(corpus)
    if payload is None:
        return None
    rel_path = _normalise_rel_path(str(payload["rel_path"]))
    backup_git_dir = _validate_marker_backup(corpus, payload)
    git_dir = corpus / ".git"
    phase = payload.get("phase")

    if not git_dir.exists():
        if not backup_git_dir.is_dir():
            raise GitEraseError(
                "interrupted erase left no active .git directory and its retained backup "
                f"is missing ({backup_git_dir}); manual recovery is required"
            )
        backup_git_dir.rename(git_dir)
        _remove_marker(corpus)
        try:
            backup_git_dir.parent.rmdir()
        except OSError:
            pass
        return "rolled_back"

    # A marker written before the first rename is harmless: the original
    # repository is still active and no rewrite exists to finish.  Do not
    # mistake it for a completed rewrite merely because a path count is zero.
    if phase == "prepared":
        _remove_marker(corpus)
        try:
            backup_git_dir.parent.rmdir()
        except OSError:
            pass
        return "rolled_back"

    # Phases original-moved / swapped / new_git_installed_needs_target_reconcile
    # with a live .git directory: determine which repository is active.  A
    # rewritten repository whose every commit was pruned is a deliberate
    # supported terminal state (zero commits, unborn HEAD), so the count
    # helper must be unborn-safe -- it is: `git log --all -- <path>` exits 0
    # with no output when there are no refs at all.
    remaining = _commit_count_for_path(corpus, rel_path)
    if remaining != 0:
        # The original repository is still active (for example a caught error
        # after moving it back, or an operator-assisted rollback).  Keep no
        # stale transaction marker around and never reconcile a worktree that
        # still has the target in history.
        _remove_marker(corpus)
        return "rolled_back"

    try:
        _sync_erased_worktree(corpus, rel_path, timeout=timeout)
    except GitEraseError:
        raise
    _remove_marker(corpus)
    return "completed"


def _validate_rewritten_mirror(
    mirror_git_dir: Path, preflight: ErasePreflight, *, timeout: float
) -> None:
    remaining = _commit_count_for_path(mirror_git_dir, preflight.rel_path)
    if remaining:
        raise GitEraseError(
            f"filter-repo reported success but {preflight.rel_path} still appears in "
            f"{remaining} path-touching commit(s); refusing cutover"
        )

    refs_after = _list_refs(mirror_git_dir)
    head = _run_git(["rev-parse", "--verify", "HEAD"], cwd=mirror_git_dir, timeout=timeout)
    if head.returncode == 0:
        if refs_after != preflight.refs:
            raise GitEraseError(
                "rewritten mirror changed the supported ref set unexpectedly: before "
                f"{preflight.refs}, after {refs_after}; refusing cutover"
            )
    elif head.returncode == 128:
        # Only the one supported branch can disappear, and only when it was
        # entirely composed of the erased path.
        if refs_after:
            raise GitEraseError(
                f"rewritten mirror has no HEAD but still has refs {refs_after}; refusing cutover"
            )
    else:
        raise GitEraseError(f"could not validate rewritten mirror HEAD: {head.stderr.strip()}")

    if preflight.target_blob_ids:
        batch = "".join(f"{blob}\n" for blob in preflight.target_blob_ids)
        try:
            checked = subprocess.run(
                ["git", "cat-file", "--batch-check=%(objectname) %(objecttype)"],
                cwd=str(mirror_git_dir),
                input=batch,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise GitEraseError(f"could not validate target blobs after filter-repo: {exc}") from exc
        if checked.returncode != 0:
            raise GitEraseError(f"post-rewrite blob validation failed: {checked.stderr.strip()}")
        survivors = [line for line in checked.stdout.splitlines() if not line.endswith(" missing")]
        if survivors:
            raise GitEraseError(
                "filter-repo left erased-path blob object(s) reachable in the rewritten "
                f"mirror: {', '.join(survivors)}"
            )


def _preserve_supported_local_config(mirror_git_dir: Path, preflight: ErasePreflight) -> None:
    """A clone does not inherit local author identity; retain only supported keys."""
    for key, value in (("user.name", preflight.user_name), ("user.email", preflight.user_email)):
        if value is None:
            continue
        out = _run_git(["config", "--local", key, value], cwd=mirror_git_dir)
        _require_success(out, action=f"preserving local Git config {key}")
    # A --mirror clone is bare.  The replacement is used as the live worktree's
    # .git directory, so make it a normal non-bare repository before cutover.
    out = _run_git(["config", "--local", "core.bare", "false"], cwd=mirror_git_dir)
    _require_success(out, action="preparing rewritten Git directory for a working tree")


def _new_backup_git_dir(corpus: Path) -> Path:
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    token = uuid.uuid4().hex
    return _transaction_root(corpus) / "erase-backups" / f"{stamp}-{token}" / "git"


def _assert_preflight_snapshot(
    corpus: Path, preflight: ErasePreflight, *, allow_target_dirty: bool
) -> None:
    """Detect external Git mutation between authoritative preflight and cutover.

    A CLI erase deliberately modifies only ``rel_path`` to tombstone it before
    its history is rewritten.  No other tracked path or staged content may
    change while the transaction prepares its mirror.
    """
    head_ref = _run_git(["symbolic-ref", "-q", "HEAD"], cwd=corpus)
    if head_ref.returncode != 0 or head_ref.stdout.strip() != preflight.head_ref:
        raise GitEraseError("corpus HEAD branch changed during erase preparation; refusing cutover")
    head = _run_git(["rev-parse", "--verify", "HEAD"], cwd=corpus)
    current_oid = head.stdout.strip() if head.returncode == 0 else None
    if current_oid != preflight.head_oid:
        raise GitEraseError("corpus HEAD changed during erase preparation; refusing cutover")
    if _list_refs(corpus) != preflight.refs:
        raise GitEraseError("corpus ref set changed during erase preparation; refusing cutover")

    staged = _run_git(["diff", "--cached", "--quiet"], cwd=corpus)
    if staged.returncode == 1:
        raise GitEraseError("staged changes appeared during erase preparation; refusing cutover")
    _require_success(staged, action="checking staged state before Git cutover")
    changed = _run_git(["diff", "--name-only"], cwd=corpus)
    changed_paths = [line for line in _require_success(changed, action="checking worktree state before Git cutover").splitlines() if line]
    allowed = {preflight.rel_path} if allow_target_dirty else set()
    if any(path not in allowed for path in changed_paths):
        raise GitEraseError("tracked files changed during erase preparation; refusing cutover")


def erase_from_git_history(
    corpus: Path,
    rel_path: str,
    *,
    timeout: float = 600.0,
    preflight: ErasePreflight | None = None,
    allow_target_dirty: bool = False,
) -> EraseResult:
    """Rewrite active Git history to remove one current source path.

    Public direct callers get a complete clean-state preflight.  ``cmd_erase``
    supplies one captured under its corpus write lock before tombstoning; this
    avoids treating that intentional tombstone write as unrelated dirty state.
    """
    corpus = Path(corpus).expanduser().resolve()
    rel_path = _normalise_rel_path(rel_path)
    if preflight is not None and preflight.rel_path != rel_path:
        raise GitEraseError("erase preflight path does not match requested path")

    recovered = recover_interrupted_erase(corpus, timeout=timeout)
    if recovered == "completed":
        # The requested path is already at its terminal post-erase state
        # (recovery finished a prior interrupted erase of this same path).
        return EraseResult(path_touching_commits=0, backup_git_dir=None, history_rewritten=True)

    if preflight is None:
        preflight = preflight_git_erase(corpus, rel_path)

    if preflight.path_touching_commits == 0:
        # A source can exist solely in the working tree.  There is no Git
        # history to rewrite, but leaving its raw text on disk would be a
        # silent and unsafe no-op.
        _sync_erased_worktree(corpus, rel_path, timeout=timeout)
        return EraseResult(path_touching_commits=0, backup_git_dir=None, history_rewritten=False)

    txn_root = _transaction_root(corpus)
    stage_parent = txn_root / "erase-staging"
    stage_parent.mkdir(parents=True, exist_ok=True)
    stage_root = Path(tempfile.mkdtemp(prefix="txn-", dir=stage_parent))
    mirror_git_dir = stage_root / "rewritten.git"
    backup_git_dir = _new_backup_git_dir(corpus)
    git_dir = corpus / ".git"
    cutover_started = False
    marker_written = False
    try:
        # --mirror preserves every ref from the supported one-branch source;
        # --no-local avoids source-object hardlinks that filter-repo could harm.
        clone = _run_git(
            ["clone", "--no-local", "--mirror", str(corpus), str(mirror_git_dir)],
            cwd=stage_root,
            timeout=timeout,
        )
        _require_success(clone, action="cloning a disposable mirror for history erasure")
        _assert_preflight_snapshot(corpus, preflight, allow_target_dirty=allow_target_dirty)

        try:
            filtered = subprocess.run(
                ["git", "filter-repo", "--force", "--path", rel_path, "--invert-paths"],
                cwd=str(mirror_git_dir),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise GitEraseError(f"git filter-repo failed to run: {exc}") from exc
        if filtered.returncode != 0:
            detail = filtered.stderr.strip() or filtered.stdout.strip()
            raise GitEraseError(f"git filter-repo failed: {detail}")

        _validate_rewritten_mirror(mirror_git_dir, preflight, timeout=timeout)
        _preserve_supported_local_config(mirror_git_dir, preflight)
        _assert_preflight_snapshot(corpus, preflight, allow_target_dirty=allow_target_dirty)

        # The durable state root must be untracked (validated by preflight) and
        # on the exact device as the live Git directory.  A corpus can itself
        # be a mount point, so its parent is intentionally never assumed safe.
        if os.stat(stage_root).st_dev != os.stat(git_dir).st_dev:
            raise GitEraseError("same-filesystem staging check failed; refusing Git cutover")
        backup_git_dir.parent.mkdir(mode=0o700, parents=True)
        if os.stat(backup_git_dir.parent).st_dev != os.stat(git_dir).st_dev:
            raise GitEraseError("same-filesystem backup check failed; refusing Git cutover")
        if backup_git_dir.exists():
            raise GitEraseError(f"refusing to overwrite existing pre-erase backup {backup_git_dir}")

        payload: dict[str, object] = {
            "version": 1,
            "phase": "prepared",
            "rel_path": rel_path,
            "backup_git_dir": str(backup_git_dir),
        }
        _write_marker(corpus, payload)
        marker_written = True

        # Each individual rename is atomic; the marker turns their unavoidable
        # gap into a recoverable transaction after process death or power loss.
        git_dir.rename(backup_git_dir)
        cutover_started = True
        payload["phase"] = "original-moved"
        _write_marker(corpus, payload)
        try:
            mirror_git_dir.rename(git_dir)
        except OSError as exc:
            # Catchable error: restore immediately, then remove the marker.
            recovery = recover_interrupted_erase(corpus, timeout=timeout)
            if recovery != "rolled_back":
                raise GitEraseError(
                    "the rewritten Git directory could not be installed and automatic "
                    "rollback did not establish the original repository",
                    history_changed=True,
                ) from exc
            raise GitEraseError(
                f"Git cutover failed before rewritten history became active; original history was restored: {exc}"
            ) from exc

        payload["phase"] = "swapped"
        _write_marker(corpus, payload)
        # The rewritten .git is installed and active.  The remaining step is
        # narrowly-scoped work-tree reconciliation; recovery must run ONLY
        # that (never a broad checkout/clean), so give the journal its own
        # phase name and write it before starting.
        payload["phase"] = "new_git_installed_needs_target_reconcile"
        _write_marker(corpus, payload)
        _sync_erased_worktree(corpus, rel_path, timeout=timeout)
        _remove_marker(corpus)
        marker_written = False
        return EraseResult(
            path_touching_commits=preflight.path_touching_commits,
            backup_git_dir=backup_git_dir,
            history_rewritten=True,
        )
    except GitEraseError:
        raise
    except Exception as exc:
        # If the first rename landed, do not falsely promise unchanged history.
        raise GitEraseError(
            f"unexpected Git erasure failure: {exc}", history_changed=cutover_started and git_dir.exists()
        ) from exc
    finally:
        shutil.rmtree(stage_root, ignore_errors=True)
        if not cutover_started and backup_git_dir.parent.exists() and not marker_written:
            try:
                backup_git_dir.parent.rmdir()
            except OSError:
                pass
