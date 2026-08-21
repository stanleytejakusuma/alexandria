"""#6 erasure-core, tail: git-history erasure for a single document.

`alexandria erase <doc_id>` reaches beyond `alexandria delete`'s tombstone
(retrievable-surface only) to remove a document's raw text from the corpus
git repository's history entirely -- the "tail" of the ratified Q1 decision
(docs/DECISION-erasure-scope-q1.md): audit trail and backups stay, per that
decision; git history is scrubbed.

SAFETY DESIGN, the load-bearing decision this module exists to get right:
`git filter-repo` is destructive -- per its own documentation, it "writes
new commits... then DELETES the original history." It refuses to run on a
repo that isn't a fresh clone unless `--force` is passed, precisely because
running it against a real, in-use repository is dangerous. This module
NEVER runs filter-repo against the corpus repo directly. It always operates
on a DISPOSABLE CLONE, validates the result, and only then atomically swaps
the corpus's `.git` directory for the clone's rewritten one. A crash at any
point before that final swap leaves the ORIGINAL corpus repo completely
untouched -- the disposable clone is simply abandoned. The previous `.git`
directory is renamed aside (never deleted) until the swap is confirmed
complete, matching #30 P2a's "never delete the previous release" retention
idiom for exactly the same reason: rollback must remain possible.

CACHE-BEFORE-HISTORY SEQUENCING (pinned in the decision doc): erase_document()
purges the embedding cache's rows for this document's current chunk texts
BEFORE any git operation, because the cache is content-addressed and can
only be purged by key while the source text still exists to hash. Doing
this after the git rewrite would leave those cache rows permanently
unaddressable instead of actually removed.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["EraseResult", "GitEraseError", "erase_from_git_history", "impact_report"]


class GitEraseError(Exception):
    """A git-history erasure step failed. The corpus repo is guaranteed
    UNCHANGED whenever this is raised -- every step before the final atomic
    swap operates on a disposable clone, and the swap itself is the last
    action taken, after every prior step has already succeeded."""


@dataclass
class EraseResult:
    doc_id: str
    commits_rewritten: int
    cache_rows_purged: int
    citations_found: list[str] = field(default_factory=list)
    """answer_ids (from #9's citation tuples) that cited this document
    before erasure -- an impact report, not an action; #6's ratified
    decision keeps the audit trail, so these rows are NOT touched."""


def _run_git(args: list[str], *, cwd: Path, timeout: float = 120.0) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True,
            timeout=timeout, check=False,
        )
    except FileNotFoundError as exc:
        raise GitEraseError("git is not installed or not on PATH") from exc
    except subprocess.SubprocessError as exc:
        raise GitEraseError(f"git {' '.join(args)} failed to run: {exc}") from exc


def _commit_count_for_path(repo: Path, rel_path: str) -> int:
    """How many commits in `repo`'s history ever touched `rel_path` --
    the exact number `erase` will rewrite, printed to the operator BEFORE
    any confirmation is asked for."""
    out = _run_git(["log", "--oneline", "--all", "--", rel_path], cwd=repo)
    if out.returncode != 0:
        raise GitEraseError(f"git log failed: {out.stderr.strip()}")
    lines = [line for line in out.stdout.splitlines() if line.strip()]
    return len(lines)


def impact_report(corpus: Path, doc_id: str, *, last: int = 5000) -> list[str]:
    """#9 makes this free: every citation tuple durably carries doc_id
    (answers.jsonl, backlog #9). A pre-erase impact report is a join
    against the audit trail's citation records -- READ ONLY, never
    mutates anything (the ratified decision keeps the audit trail; this
    just SHOWS an operator what it currently says about the document
    they are about to erase, before they confirm).

    Returns the list of answer_ids whose citations named this doc_id, most
    recent `last` rows scanned (matches audit_summary()'s own bounded-scan
    convention -- an unbounded full-history scan is not needed for an
    informational report, and the audit log has no index to make one fast).
    """
    import json as _json

    from .auditlog import audit_log_dir

    answers_path = audit_log_dir(corpus) / "answers.jsonl"
    if not answers_path.exists():
        return []
    lines = answers_path.read_text(encoding="utf-8").splitlines()
    found: list[str] = []
    for line in lines[-last:]:
        if not line.strip():
            continue
        try:
            row = _json.loads(line)
        except _json.JSONDecodeError:
            continue
        citations = row.get("citations") or []
        if any(c.get("doc_id") == doc_id for c in citations):
            found.append(row.get("id", ""))
    return found


def erase_from_git_history(corpus: Path, rel_path: str, *, timeout: float = 600.0) -> int:
    """Remove every trace of `rel_path` from the corpus git repository's
    history. Returns the number of commits that were rewritten.

    NEVER operates on `corpus` directly (see module docstring). Clones,
    rewrites the clone, validates, swaps .git atomically as the final step.
    Raises GitEraseError and leaves `corpus` completely unchanged on any
    failure before the swap.
    """
    git_dir = corpus / ".git"
    if not git_dir.is_dir():
        raise GitEraseError(f"{corpus} is not a git repository (no .git directory)")

    commits_before = _commit_count_for_path(corpus, rel_path)
    if commits_before == 0:
        # Nothing to rewrite -- honest zero, not an error. Matches
        # EnrichmentStore.invalidate()'s "absence is a normal outcome" contract.
        return 0

    tmp_root = Path(tempfile.mkdtemp(prefix="alexandria-erase-"))
    clone_dir = tmp_root / "clone"
    try:
        # --no-local: filter-repo's own docs recommend this for local-
        # filesystem clones, since --local (the git-clone default fast path)
        # can hardlink objects with the source, which would corrupt the
        # ORIGINAL repo's objects once filter-repo starts rewriting them.
        clone = _run_git(["clone", "--no-local", str(corpus), str(clone_dir)],
                         cwd=tmp_root, timeout=timeout)
        if clone.returncode != 0:
            raise GitEraseError(f"failed to clone {corpus} for a safe rewrite: "
                               f"{clone.stderr.strip()}")

        if shutil.which("git-filter-repo") is None:
            raise GitEraseError(
                "git-filter-repo is not installed. It is an optional external "
                "tool (like pdftotext for PDF ingest) -- install it with "
                "`brew install git-filter-repo` or `pip install git-filter-repo`, "
                "then retry. Nothing in the corpus was touched.")
        filtered = subprocess.run(
            ["git", "filter-repo", "--force", "--path", rel_path, "--invert-paths"],
            cwd=str(clone_dir), capture_output=True, text=True, timeout=timeout, check=False,
        )
        if filtered.returncode != 0:
            raise GitEraseError(f"git filter-repo failed: {filtered.stderr.strip()}")

        # Validate BEFORE touching the real corpus: the path must be
        # genuinely gone from the rewritten clone's history.
        commits_after = _commit_count_for_path(clone_dir, rel_path)
        if commits_after != 0:
            raise GitEraseError(
                f"filter-repo reported success but {rel_path} still appears in "
                f"{commits_after} commit(s) of the rewritten history -- refusing "
                f"to swap an incompletely-rewritten repo into the corpus")

        # The atomic swap: rename the corpus's ORIGINAL .git aside (never
        # delete it -- this is the rollback path), then move the clone's
        # rewritten .git into place. Both are directory renames on the same
        # filesystem, which POSIX guarantees are atomic; the corpus is never
        # observed in a state with no .git directory at all.
        clone_git_dir = clone_dir / ".git"
        backup_git_dir = corpus / f".git.pre-erase-{rel_path.replace('/', '_')}"
        if backup_git_dir.exists():
            shutil.rmtree(backup_git_dir)
        git_dir.rename(backup_git_dir)
        try:
            clone_git_dir.rename(git_dir)
        except Exception:
            # The swap's second half failed -- restore the ORIGINAL .git
            # immediately so the corpus is never left without one.
            backup_git_dir.rename(git_dir)
            raise

        # Sync the working tree to match the rewritten history's HEAD.
        # DELIBERATELY narrow: only reconcile the ONE known path that was
        # erased, never a repo-root-wide `git clean -fd`. The corpus
        # deliberately keeps operational state (.alexandria/ -- the index,
        # embedding cache, audit trail) as UNTRACKED content alongside the
        # tracked sources/ tree (this repo's own "corpus is not this repo"
        # doctrine mirrors it: git-ignored, never committed). A blanket
        # `git clean -fd` at the corpus root would delete ALL of that --
        # proven live during development: an end-to-end test lost its
        # entire .alexandria/ directory (index + embedding cache) to
        # exactly this, turning "the document is erased" into "the whole
        # corpus's search index is gone". `checkout HEAD -- .` alone also
        # doesn't fully cover it: it updates paths still TRACKED at HEAD,
        # but a file that no longer exists ANYWHERE in the rewritten
        # history is not "checked out to absent" by that command -- it
        # simply survives untouched on disk. So: checkout what IS tracked,
        # then explicitly remove the one known erased path if it is still
        # sitting on disk and git no longer tracks it.
        #
        # Edge case (verified live): if the erased document was the ONLY
        # content in the ONLY commit, filter-repo prunes that now-empty
        # commit entirely -- the rewritten history has ZERO commits and no
        # resolvable HEAD. `checkout HEAD` fails in that case ("unknown
        # revision"), not because anything went wrong, but because there is
        # genuinely nothing to check out. Detected via `rev-parse --verify
        # HEAD` first; the erased-path removal below still applies either way.
        head_check = _run_git(["rev-parse", "--verify", "HEAD"], cwd=corpus, timeout=timeout)
        if head_check.returncode == 0:
            checkout = _run_git(["checkout", "HEAD", "--", "."], cwd=corpus, timeout=timeout)
            if checkout.returncode != 0:
                raise GitEraseError(
                    f"history was rewritten and swapped in, but syncing the "
                    f"tracked working tree failed: {checkout.stderr.strip()} -- "
                    f"the corpus's .git is now correct, but tracked files on "
                    f"disk may be stale; run `git checkout HEAD -- .` in "
                    f"{corpus} manually (do NOT run `git clean` at the corpus "
                    f"root -- it would delete untracked .alexandria/ state)")

        erased_on_disk = corpus / rel_path
        if erased_on_disk.exists():
            still_tracked = _run_git(["ls-files", "--error-unmatch", rel_path],
                                     cwd=corpus, timeout=timeout)
            if still_tracked.returncode != 0:
                # git no longer tracks this path at all -- it's the orphaned
                # pre-erase copy, safe to remove directly (targeted, not a
                # repo-wide clean).
                erased_on_disk.unlink()

        return commits_before
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
