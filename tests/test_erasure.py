"""#6 erasure-core, tail: alexandria.erasure -- git-history rewriting.

Tests operate on DISPOSABLE synthetic git repos created per-test in tmp_path,
never anywhere near the real corpus. Each safety property this module's own
docstring claims is proven live, not asserted."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from alexandria.erasure import GitEraseError, erase_from_git_history, impact_report

requires_git_filter_repo = pytest.mark.skipif(
    shutil.which("git-filter-repo") is None,
    reason="git-filter-repo not installed (pip install git-filter-repo, or brew "
           "install git-filter-repo) -- required for the actual history rewrite; "
           "impact_report() and the argument-validation paths still run without it")


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                            text=True, timeout=30, check=True)
    return result.stdout


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@test.com")
    _git(repo, "config", "user.name", "Test")


def _commit_all(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


@requires_git_filter_repo
def test_erase_removes_the_document_from_history_and_disk_but_keeps_others(tmp_path: Path):
    repo = tmp_path / "corpus"
    _init_repo(repo)
    (repo / "sources").mkdir()
    (repo / "sources" / "keep.md").write_text("Keep this.\n")
    (repo / "sources" / "secret.md").write_text("Secret v1.\n")
    _commit_all(repo, "initial")
    (repo / "sources" / "secret.md").write_text("Secret v2, edited.\n")
    _commit_all(repo, "edit secret")

    commits_before = len(_git(repo, "log", "--oneline", "--all", "--",
                              "sources/secret.md").splitlines())
    assert commits_before == 2  # premise: real history exists to rewrite

    result = erase_from_git_history(repo, "sources/secret.md")
    assert result.path_touching_commits == 2
    assert result.history_rewritten is True
    assert result.backup_git_dir is not None

    commits_after = _git(repo, "log", "--oneline", "--all", "--", "sources/secret.md")
    assert commits_after.strip() == ""
    assert not (repo / "sources" / "secret.md").exists()
    assert (repo / "sources" / "keep.md").exists()
    assert (repo / "sources" / "keep.md").read_text() == "Keep this.\n"


def test_erase_of_a_path_with_no_history_is_an_honest_zero_not_an_error(tmp_path: Path):
    repo = tmp_path / "corpus"
    _init_repo(repo)
    (repo / "sources").mkdir()
    (repo / "sources" / "a.md").write_text("A.\n")
    _commit_all(repo, "initial")

    result = erase_from_git_history(repo, "sources/never-existed.md")
    assert result.path_touching_commits == 0
    assert result.history_rewritten is False
    assert result.backup_git_dir is None
    # nothing was touched -- confirm the repo is unaffected
    assert (repo / "sources" / "a.md").exists()


def test_erase_refuses_a_non_git_directory(tmp_path: Path):
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()
    with pytest.raises(GitEraseError, match="not a git repository"):
        erase_from_git_history(not_a_repo, "sources/a.md")


def test_a_failed_filter_repo_leaves_the_original_history_completely_unchanged(
        tmp_path: Path, monkeypatch):
    """The load-bearing safety property: erase_from_git_history NEVER
    mutates the real corpus repo until the very last (swap) step, and
    every step before that operates on a disposable clone.

    Deliberately does NOT require the real git-filter-repo binary --
    subprocess.run itself is faked below, so this test proves the ROLLBACK
    contract regardless of whether the real tool happens to be installed on
    the machine running it. shutil.which is mocked to report the binary as
    present so the code path under test is reached even when it is not."""
    repo = tmp_path / "corpus"
    _init_repo(repo)
    (repo / "sources").mkdir()
    (repo / "sources" / "a.md").write_text("A.\n")
    _commit_all(repo, "initial")
    before_head = _git(repo, "rev-parse", "HEAD").strip()
    before_log = _git(repo, "log", "--oneline", "--all")

    import alexandria.erasure as erasure_mod
    monkeypatch.setattr(erasure_mod.shutil, "which",
                        lambda name: "/fake/git-filter-repo" if name == "git-filter-repo" else None)
    original_run = subprocess.run

    def failing_filter_repo(cmd, **kwargs):
        if "filter-repo" in cmd:
            class FakeResult:
                returncode = 1
                stderr = "simulated filter-repo failure"
                stdout = ""
            return FakeResult()
        return original_run(cmd, **kwargs)

    monkeypatch.setattr(erasure_mod.subprocess, "run", failing_filter_repo)
    with pytest.raises(GitEraseError, match="filter-repo failed"):
        erase_from_git_history(repo, "sources/a.md")

    monkeypatch.undo()
    after_head = _git(repo, "rev-parse", "HEAD").strip()
    after_log = _git(repo, "log", "--oneline", "--all")
    assert after_head == before_head
    assert after_log == before_log
    assert (repo / "sources" / "a.md").exists()


@requires_git_filter_repo
def test_a_mid_swap_failure_restores_the_original_git_directory(tmp_path: Path, monkeypatch):
    """The narrowest, scariest failure window: a crash between renaming the
    ORIGINAL .git aside and renaming the REWRITTEN .git into place. The
    recovery path must restore the original .git immediately, never leave
    the corpus with no .git directory at all."""
    repo = tmp_path / "corpus"
    _init_repo(repo)
    (repo / "sources").mkdir()
    (repo / "sources" / "a.md").write_text("A.\n")
    _commit_all(repo, "initial")
    before_head = _git(repo, "rev-parse", "HEAD").strip()

    from pathlib import Path as PathType
    original_rename = PathType.rename
    call_count = [0]

    def failing_rename(self, target):
        call_count[0] += 1
        if call_count[0] == 2:  # the SECOND rename is clone_git_dir -> git_dir
            raise OSError("simulated rename failure mid-swap")
        return original_rename(self, target)

    monkeypatch.setattr(PathType, "rename", failing_rename)
    with pytest.raises(Exception):
        erase_from_git_history(repo, "sources/a.md")
    monkeypatch.undo()

    assert (repo / ".git").is_dir()
    assert _git(repo, "rev-parse", "HEAD").strip() == before_head
    assert (repo / "sources" / "a.md").exists()


@requires_git_filter_repo
def test_erase_leaves_a_recoverable_backup_of_the_pre_erase_git_directory(tmp_path: Path):
    """The swap never DELETES the pre-erase .git -- it is renamed aside,
    matching #30 P2a's retention idiom. Verified directly: after a
    successful erase, the backup directory exists and is a real git repo
    that still contains the erased document's full history."""
    repo = tmp_path / "corpus"
    _init_repo(repo)
    (repo / "sources").mkdir()
    (repo / "sources" / "a.md").write_text("A.\n")
    _commit_all(repo, "initial")

    result = erase_from_git_history(repo, "sources/a.md")

    # The swap never DELETES the pre-erase .git -- it is renamed aside into
    # the verified-untracked durable state root, matching #30 P2a's retention
    # idiom. Verified directly: after a successful erase, the backup
    # directory exists and is a real git repo that still contains the erased
    # document's full history.
    assert result.backup_git_dir is not None
    backup_dir = result.backup_git_dir
    assert backup_dir.is_dir()
    assert backup_dir.parent.parent == repo / ".alexandria" / "erase-backups"
    assert backup_dir.name == "git"
    # the backup is a real, independently-usable git repo carrying the
    # ORIGINAL (pre-erase) history
    log = subprocess.run(["git", "--git-dir", str(backup_dir), "log", "--oneline"],
                         capture_output=True, text=True, timeout=10)
    assert "initial" in log.stdout


@requires_git_filter_repo
def test_erasing_the_only_document_leaves_a_valid_empty_but_functional_repo(tmp_path: Path):
    """Edge case found live during development: if the erased document was
    the ONLY content in the ONLY commit, filter-repo correctly prunes that
    now-empty commit -- the rewritten history has ZERO commits and no
    resolvable HEAD. `git reset --hard HEAD` fails in that case (nothing to
    reset TO), not because the erase failed. Must be handled distinctly,
    not surfaced as a spurious error."""
    repo = tmp_path / "corpus"
    _init_repo(repo)
    (repo / "sources").mkdir()
    (repo / "sources" / "only.md").write_text("The only document.\n")
    _commit_all(repo, "initial")

    result = erase_from_git_history(repo, "sources/only.md")
    assert result.path_touching_commits == 1
    assert result.history_rewritten is True
    assert not (repo / "sources" / "only.md").exists()
    assert (repo / ".git").is_dir()  # the repo itself is still valid

    # a subsequent normal commit must work fine against this now-empty history
    (repo / "sources").mkdir(exist_ok=True)
    (repo / "sources" / "new.md").write_text("A fresh document.\n")
    _commit_all(repo, "first commit after erasure")
    assert "first commit after erasure" in _git(repo, "log", "--oneline")


def test_impact_report_finds_citing_answer_ids(tmp_path: Path):
    """#9 makes this free: a real pre-erase report joins against the
    durable citation tuples answers.jsonl already carries."""
    import json

    corpus = tmp_path / "corpus"
    audit_dir = corpus / ".alexandria" / "audit"
    audit_dir.mkdir(parents=True)
    rows = [
        {"id": "answer-1", "citations": [{"doc_id": "sources/target", "chunk_id": "sources/target#1"}]},
        {"id": "answer-2", "citations": [{"doc_id": "sources/other", "chunk_id": "sources/other#1"}]},
        {"id": "answer-3", "citations": [
            {"doc_id": "sources/target", "chunk_id": "sources/target#2"},
            {"doc_id": "sources/other", "chunk_id": "sources/other#1"},
        ]},
    ]
    with open(audit_dir / "answers.jsonl", "w") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")

    report = impact_report(corpus, "sources/target")
    assert report == ["answer-1", "answer-3"]


def test_impact_report_returns_empty_for_an_uncited_document(tmp_path: Path):
    import json

    corpus = tmp_path / "corpus"
    audit_dir = corpus / ".alexandria" / "audit"
    audit_dir.mkdir(parents=True)
    with open(audit_dir / "answers.jsonl", "w") as fh:
        fh.write(json.dumps({"id": "answer-1",
                             "citations": [{"doc_id": "sources/other"}]}) + "\n")

    assert impact_report(corpus, "sources/never-cited") == []


def test_impact_report_handles_a_missing_audit_log_gracefully(tmp_path: Path):
    """No answers.jsonl at all (a fresh corpus that has never run `answer`)
    -- must return an empty report, never raise."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    assert impact_report(corpus, "sources/anything") == []


def test_impact_report_tolerates_a_malformed_line(tmp_path: Path):
    """One corrupted JSONL line must not abort the whole report -- matches
    this codebase's general 'a partial failure never blocks the rest'
    posture (e.g. AuditLogger._append's own never-raise contract)."""
    import json

    corpus = tmp_path / "corpus"
    audit_dir = corpus / ".alexandria" / "audit"
    audit_dir.mkdir(parents=True)
    with open(audit_dir / "answers.jsonl", "w") as fh:
        fh.write("{not valid json\n")
        fh.write(json.dumps({"id": "answer-good",
                             "citations": [{"doc_id": "sources/target"}]}) + "\n")

    assert impact_report(corpus, "sources/target") == ["answer-good"]


def test_erase_gives_a_clean_actionable_refusal_when_git_filter_repo_is_missing(
        tmp_path: Path, monkeypatch):
    """Matches this codebase's existing 'optional external binary, absent ->
    clean refusal' contract (pdftotext for PDF ingest, documented in
    docs/HTTP-API.md) rather than an opaque subprocess error."""
    import shutil as _shutil

    repo = tmp_path / "corpus"
    _init_repo(repo)
    (repo / "sources").mkdir()
    (repo / "sources" / "a.md").write_text("A.\n")
    _commit_all(repo, "initial")

    import alexandria.erasure as erasure_mod
    monkeypatch.setattr(erasure_mod.shutil, "which",
                        lambda name: None if name == "git-filter-repo" else _shutil.which(name))

    with pytest.raises(GitEraseError, match="git-filter-repo is not installed"):
        erase_from_git_history(repo, "sources/a.md")

    # nothing was touched
    assert (repo / "sources" / "a.md").exists()
    assert _git(repo, "log", "--oneline", "--all", "--", "sources/a.md").strip() != ""


# ---------------------------------------------------------------------------
# #70-#73 (Red-remediation): durable crash-phase recovery, preflight refusals
# before any mutation, same-device/snapshot checks, alias/blob limits.
# ---------------------------------------------------------------------------


def _pretend_filter_repo_installed(monkeypatch):
    """Preflight-only tests run even where the real binary is absent: the
    refusal being proven happens strictly before any rewrite would."""
    import shutil as _shutil
    import alexandria.erasure as erasure_mod
    monkeypatch.setattr(
        erasure_mod.shutil, "which",
        lambda name: "/fake/git-filter-repo" if name == "git-filter-repo" else _shutil.which(name))


def _make_committed_repo(tmp_path, files=None, name="corpus"):
    repo = tmp_path / name
    _init_repo(repo)
    (repo / "sources").mkdir(exist_ok=True)
    for rel, text in (files or {"sources/a.md": "A content.\n"}).items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    _commit_all(repo, "initial")
    return repo


def _write_marker_manual(repo, phase, rel_path, backup_git_dir):
    from alexandria.erasure import _write_marker
    _write_marker(repo, {
        "version": 1, "phase": phase, "rel_path": rel_path,
        "backup_git_dir": str(backup_git_dir),
    })


def _make_backup_of_git(repo):
    backup = repo / ".alexandria" / "erase-backups" / "20260101T000000Z-deadbeef" / "git"
    backup.parent.mkdir(parents=True)
    shutil.copytree(repo / ".git", backup)
    return backup


# --- #71 preflight refusals: every one must fire BEFORE any mutation. ------

def test_preflight_refuses_staged_unrelated_changes(tmp_path: Path, monkeypatch):
    _pretend_filter_repo_installed(monkeypatch)
    from alexandria.erasure import GitEraseError, preflight_git_erase
    repo = _make_committed_repo(tmp_path, {"sources/a.md": "A\n", "sources/b.md": "B\n"})
    (repo / "sources" / "b.md").write_text("B edited.\n")
    _git(repo, "add", "sources/b.md")
    with pytest.raises(GitEraseError, match="tracked or staged changes"):
        preflight_git_erase(repo, "sources/a.md")
    # nothing was mutated
    assert (repo / "sources" / "a.md").exists()


def test_preflight_refuses_unstaged_unrelated_changes(tmp_path: Path, monkeypatch):
    _pretend_filter_repo_installed(monkeypatch)
    from alexandria.erasure import GitEraseError, preflight_git_erase
    repo = _make_committed_repo(tmp_path, {"sources/a.md": "A\n", "sources/b.md": "B\n"})
    (repo / "sources" / "b.md").write_text("B edited, unstaged.\n")
    with pytest.raises(GitEraseError, match="tracked or staged changes"):
        preflight_git_erase(repo, "sources/a.md")


def test_preflight_refuses_a_tracked_alexandria_state_directory(tmp_path: Path, monkeypatch):
    _pretend_filter_repo_installed(monkeypatch)
    from alexandria.erasure import GitEraseError, preflight_git_erase
    repo = _make_committed_repo(tmp_path)
    (repo / ".alexandria").mkdir()
    (repo / ".alexandria" / "state.md").write_text("operational state\n")
    _commit_all(repo, "track operational state")
    with pytest.raises(GitEraseError, match="tracked by Git"):
        preflight_git_erase(repo, "sources/a.md")


def test_preflight_refuses_active_git_operations(tmp_path: Path, monkeypatch):
    _pretend_filter_repo_installed(monkeypatch)
    from alexandria.erasure import GitEraseError, preflight_git_erase
    repo = _make_committed_repo(tmp_path)
    (repo / ".git" / "MERGE_HEAD").write_text("deadbeef\n")
    with pytest.raises(GitEraseError, match="active Git operation"):
        preflight_git_erase(repo, "sources/a.md")


def test_preflight_refuses_custom_hooks(tmp_path: Path, monkeypatch):
    _pretend_filter_repo_installed(monkeypatch)
    from alexandria.erasure import GitEraseError, preflight_git_erase
    repo = _make_committed_repo(tmp_path)
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    (hooks / "pre-commit").write_text("#!/bin/sh\n")
    with pytest.raises(GitEraseError, match="custom Git hooks"):
        preflight_git_erase(repo, "sources/a.md")


def test_preflight_refuses_tags_and_extra_refs(tmp_path: Path, monkeypatch):
    _pretend_filter_repo_installed(monkeypatch)
    from alexandria.erasure import GitEraseError, preflight_git_erase
    repo = _make_committed_repo(tmp_path)
    _git(repo, "tag", "v1")
    with pytest.raises(GitEraseError, match="refs beyond"):
        preflight_git_erase(repo, "sources/a.md")


def test_preflight_refuses_configured_remotes(tmp_path: Path, monkeypatch):
    _pretend_filter_repo_installed(monkeypatch)
    from alexandria.erasure import GitEraseError, preflight_git_erase
    repo = _make_committed_repo(tmp_path)
    _git(repo, "remote", "add", "origin", "https://example.invalid/repo.git")
    with pytest.raises(GitEraseError, match="remotes"):
        preflight_git_erase(repo, "sources/a.md")


def test_preflight_refuses_detached_head(tmp_path: Path, monkeypatch):
    _pretend_filter_repo_installed(monkeypatch)
    from alexandria.erasure import GitEraseError, preflight_git_erase
    repo = _make_committed_repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "checkout", "-q", "--detach", head)
    with pytest.raises(GitEraseError, match="detached"):
        preflight_git_erase(repo, "sources/a.md")


def test_preflight_refuses_historical_rename_aliases(tmp_path: Path, monkeypatch):
    """#73: path-history erasure cannot cover a historical rename; the
    command must fail closed rather than overclaim raw-text removal."""
    _pretend_filter_repo_installed(monkeypatch)
    from alexandria.erasure import GitEraseError, preflight_git_erase
    repo = tmp_path / "corpus"
    _init_repo(repo)
    (repo / "sources").mkdir()
    (repo / "sources" / "old.md").write_text("Secret text.\n")
    _commit_all(repo, "old name")
    _git(repo, "mv", "sources/old.md", "sources/current.md")
    _commit_all(repo, "rename to current")
    with pytest.raises(GitEraseError, match="rename/copy"):
        preflight_git_erase(repo, "sources/current.md")


def test_preflight_refuses_shared_current_blobs(tmp_path: Path, monkeypatch):
    """#73: an exact blob reachable under another current path must be
    refused, not silently retained elsewhere."""
    _pretend_filter_repo_installed(monkeypatch)
    from alexandria.erasure import GitEraseError, preflight_git_erase
    repo = _make_committed_repo(
        tmp_path,
        {"sources/a.md": "Identical raw text.\n", "sources/b.md": "Identical raw text.\n"})
    with pytest.raises(GitEraseError, match="also reachable"):
        preflight_git_erase(repo, "sources/a.md")


def test_sentinel_prior_backup_survives_a_refused_erase(tmp_path: Path, monkeypatch):
    """#71: a pre-existing retained backup must survive a refused erase --
    the refusal fires before any mutation, and backups are never deleted."""
    _pretend_filter_repo_installed(monkeypatch)
    from alexandria.erasure import GitEraseError, preflight_git_erase
    repo = _make_committed_repo(tmp_path, {"sources/a.md": "A\n", "sources/b.md": "B\n"})
    sentinel = repo / ".alexandria" / "erase-backups" / "20250101T000000Z-cafebabe" / "git"
    sentinel.mkdir(parents=True)
    (sentinel / "config").write_text("sentinel prior backup\n")
    (repo / "sources" / "b.md").write_text("B edited.\n")  # dirty tree -> refusal
    with pytest.raises(GitEraseError, match="tracked or staged changes"):
        preflight_git_erase(repo, "sources/a.md")
    assert sentinel.is_dir()
    assert (sentinel / "config").read_text() == "sentinel prior backup\n"


# --- #70 crash-phase recovery: durable marker, only-target reconciliation. --

def test_recovery_phase_prepared_removes_the_marker_without_mutation(tmp_path: Path, monkeypatch):
    _pretend_filter_repo_installed(monkeypatch)
    from alexandria.erasure import _marker_path, recover_interrupted_erase
    repo = _make_committed_repo(tmp_path)
    # In the real flow the generation dir exists but is EMPTY at the
    # "prepared" phase: the original .git is only renamed into it later.
    backup = repo / ".alexandria" / "erase-backups" / "20260101T000000Z-deadbeef" / "git"
    backup.parent.mkdir(parents=True)
    _write_marker_manual(repo, "prepared", "sources/a.md", backup)
    assert recover_interrupted_erase(repo) == "rolled_back"
    assert not _marker_path(repo).exists()
    assert (repo / "sources" / "a.md").exists()
    assert (repo / ".git").is_dir()
    assert not backup.parent.exists()  # empty generation dir removed


def test_recovery_phase_original_moved_restores_the_original_git(tmp_path: Path, monkeypatch):
    _pretend_filter_repo_installed(monkeypatch)
    from alexandria.erasure import _marker_path, recover_interrupted_erase
    repo = _make_committed_repo(tmp_path)
    before_head = _git(repo, "rev-parse", "HEAD").strip()
    backup = _make_backup_of_git(repo)
    _write_marker_manual(repo, "original-moved", "sources/a.md", backup)
    shutil.rmtree(repo / ".git")  # simulate: original renamed aside, rewrite never landed
    assert recover_interrupted_erase(repo) == "rolled_back"
    assert not _marker_path(repo).exists()
    assert (repo / ".git").is_dir()
    assert _git(repo, "rev-parse", "HEAD").strip() == before_head
    assert (repo / "sources" / "a.md").exists()


@requires_git_filter_repo
def test_recovery_phase_swapped_reconciles_only_the_erased_target(tmp_path: Path, monkeypatch):
    """#70: with rewritten history active, recovery must remove ONLY the
    erased target -- never a broad checkout/clean of unrelated files."""
    _pretend_filter_repo_installed(monkeypatch)
    from alexandria.erasure import _marker_path, recover_interrupted_erase
    repo = _make_committed_repo(
        tmp_path, {"sources/a.md": "A\n", "sources/keep.md": "Keep\n"})
    backup = _make_backup_of_git(repo)
    _write_marker_manual(repo, "swapped", "sources/a.md", backup)
    subprocess.run(["git", "filter-repo", "--force", "--path", "sources/a.md", "--invert-paths"],
                   cwd=repo, check=True, capture_output=True, text=True)
    assert recover_interrupted_erase(repo) == "completed"
    assert not _marker_path(repo).exists()
    assert not (repo / "sources" / "a.md").exists()
    assert (repo / "sources" / "keep.md").exists()
    assert (repo / "sources" / "keep.md").read_text() == "Keep\n"


@requires_git_filter_repo
def test_recovery_phase_reconcile_handles_a_zero_commit_rewritten_repo(tmp_path: Path, monkeypatch):
    """#70: erasing the only document prunes every commit (unborn HEAD).
    Recovery must still complete -- target unlink only, no read-tree crash."""
    _pretend_filter_repo_installed(monkeypatch)
    from alexandria.erasure import _marker_path, recover_interrupted_erase
    repo = _make_committed_repo(tmp_path, {"sources/only.md": "Only\n"})
    backup = _make_backup_of_git(repo)
    _write_marker_manual(repo, "new_git_installed_needs_target_reconcile", "sources/only.md", backup)
    subprocess.run(["git", "filter-repo", "--force", "--path", "sources/only.md", "--invert-paths"],
                   cwd=repo, check=True, capture_output=True, text=True)
    head = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=repo,
                          capture_output=True, text=True)
    assert head.returncode == 128  # premise: zero commits, unborn HEAD
    assert recover_interrupted_erase(repo) == "completed"
    assert not _marker_path(repo).exists()
    assert not (repo / "sources" / "only.md").exists()
    assert (repo / ".git").is_dir()


def test_recovery_reconciles_a_never_tracked_source_without_broad_checkout(tmp_path: Path, monkeypatch):
    """#70: a source that was never in Git history is removed by the same
    target-only reconciliation; unrelated tracked files are untouched."""
    _pretend_filter_repo_installed(monkeypatch)
    from alexandria.erasure import _marker_path, recover_interrupted_erase
    repo = _make_committed_repo(tmp_path, {"sources/keep.md": "Keep\n"})
    (repo / "sources" / "stray.md").write_text("never tracked\n")
    backup = _make_backup_of_git(repo)
    _write_marker_manual(repo, "new_git_installed_needs_target_reconcile", "sources/stray.md", backup)
    assert recover_interrupted_erase(repo) == "completed"
    assert not _marker_path(repo).exists()
    assert not (repo / "sources" / "stray.md").exists()
    assert (repo / "sources" / "keep.md").exists()
    assert (repo / "sources" / "keep.md").read_text() == "Keep\n"


def test_recovery_refuses_a_backup_outside_the_supported_root(tmp_path: Path, monkeypatch):
    _pretend_filter_repo_installed(monkeypatch)
    from alexandria.erasure import GitEraseError, recover_interrupted_erase
    repo = _make_committed_repo(tmp_path)
    evil = tmp_path / "outside" / "git"
    evil.parent.mkdir()
    _write_marker_manual(repo, "swapped", "sources/a.md", evil)
    with pytest.raises(GitEraseError, match="outside the supported"):
        recover_interrupted_erase(repo)


def test_recovery_is_a_noop_without_a_marker(tmp_path: Path, monkeypatch):
    _pretend_filter_repo_installed(monkeypatch)
    from alexandria.erasure import recover_interrupted_erase
    repo = _make_committed_repo(tmp_path)
    assert recover_interrupted_erase(repo) is None


# --- #72 transaction checks: same-device staging, snapshot revalidation. ----

def test_erase_refuses_staging_on_a_different_filesystem(tmp_path: Path, monkeypatch):
    """The corpus can itself be a mount point; staging must be proven on the
    exact device as the live .git before cutover, never assumed safe."""
    _pretend_filter_repo_installed(monkeypatch)
    import os as _os
    from alexandria import erasure as erasure_mod
    from alexandria.erasure import GitEraseError, preflight_git_erase

    repo = _make_committed_repo(tmp_path)
    preflight = preflight_git_erase(repo, "sources/a.md")

    real_stat = _os.stat

    def fake_stat(path, *a, **k):
        st = real_stat(path, *a, **k)
        if _os.path.basename(str(path)).startswith("txn-"):  # the staging root
            # os.stat_result is a structseq without _replace; only .st_dev
            # is consulted by the same-filesystem check.
            return type("FakeStat", (), {"st_dev": st.st_dev + 1})()
        return st

    monkeypatch.setattr(erasure_mod.os, "stat", fake_stat)
    # The check under test sits AFTER filter-repo runs on the disposable
    # mirror; fake a successful rewrite so the flow reaches it (the real
    # binary is not required for this proof).
    original_run = subprocess.run

    def fake_filter_repo(cmd, **kwargs):
        if "filter-repo" in cmd:
            class FakeResult:
                returncode = 0
                stderr = ""
                stdout = ""
            return FakeResult()
        return original_run(cmd, **kwargs)

    monkeypatch.setattr(erasure_mod.subprocess, "run", fake_filter_repo)
    # Mirror validation is not under test here; the same-device staging gate
    # sits after it, so stub the validator and let the flow reach the gate.
    monkeypatch.setattr(erasure_mod, "_validate_rewritten_mirror", lambda *a, **k: None)
    with pytest.raises(GitEraseError, match="same-filesystem staging check failed"):
        erasure_mod.erase_from_git_history(repo, "sources/a.md", preflight=preflight)


@requires_git_filter_repo
def test_erase_revalidates_the_head_snapshot_before_cutover(tmp_path: Path, monkeypatch):
    """#72: a HEAD/ref mutation between authoritative preflight and cutover
    must abort -- the clone is disposable, the corpus is untouched."""
    from alexandria.erasure import GitEraseError, preflight_git_erase
    repo = _make_committed_repo(tmp_path, {"sources/a.md": "A\n", "sources/keep.md": "Keep\n"})
    preflight = preflight_git_erase(repo, "sources/a.md")
    (repo / "sources" / "keep.md").write_text("Keep edited.\n")
    _commit_all(repo, "external mutation after preflight")
    with pytest.raises(GitEraseError, match="HEAD changed during erase preparation"):
        # The clone runs first (fine), then the snapshot recheck refuses.
        from alexandria import erasure as erasure_mod
        erasure_mod.erase_from_git_history(repo, "sources/a.md", preflight=preflight)
    assert (repo / "sources" / "a.md").exists()


@requires_git_filter_repo
def test_erase_revalidates_the_ref_set_before_cutover(tmp_path: Path, monkeypatch):
    from alexandria.erasure import GitEraseError, preflight_git_erase
    repo = _make_committed_repo(tmp_path, {"sources/a.md": "A\n", "sources/keep.md": "Keep\n"})
    preflight = preflight_git_erase(repo, "sources/a.md")
    _git(repo, "tag", "v1")  # ref set mutation after preflight
    with pytest.raises(GitEraseError, match="ref set changed during erase preparation"):
        from alexandria import erasure as erasure_mod
        erasure_mod.erase_from_git_history(repo, "sources/a.md", preflight=preflight)
    assert (repo / "sources" / "a.md").exists()


# --- Red round 2, finding 1: crash between a rename and its marker advance.
# Recovery must branch on OBSERVED filesystem state, not on completed-phase
# marker values alone.


def test_recovery_crash_after_first_rename_but_before_marker_advance(tmp_path: Path, monkeypatch):
    """The original .git has already been renamed to the retained backup, but
    the marker still says 'prepared' (the crash happened before the
    'original-moved' transition was fsynced). Recovery must observe the
    missing live .git and restore the backup -- never drop the marker and
    leave the corpus without a .git."""
    _pretend_filter_repo_installed(monkeypatch)
    from alexandria.erasure import _marker_path, recover_interrupted_erase
    repo = _make_committed_repo(tmp_path)
    before_head = _git(repo, "rev-parse", "HEAD").strip()
    backup = _make_backup_of_git(repo)
    # Crash window: first rename done, marker still at "prepared".
    _write_marker_manual(repo, "prepared", "sources/a.md", backup)
    shutil.rmtree(repo / ".git")
    assert recover_interrupted_erase(repo) == "rolled_back"
    assert not _marker_path(repo).exists()
    assert (repo / ".git").is_dir()
    assert _git(repo, "rev-parse", "HEAD").strip() == before_head
    assert (repo / "sources" / "a.md").exists()


@requires_git_filter_repo
def test_recovery_crash_after_second_rename_but_before_marker_advance(tmp_path: Path, monkeypatch):
    """The rewritten .git is already installed, but the marker still says
    'original-moved' (the crash happened before the 'swapped' transition was
    fsynced). Recovery must observe that history no longer contains the path
    and complete ONLY the target reconciliation."""
    _pretend_filter_repo_installed(monkeypatch)
    from alexandria.erasure import _marker_path, recover_interrupted_erase
    repo = _make_committed_repo(tmp_path, {"sources/a.md": "A\n", "sources/keep.md": "Keep\n"})
    backup = _make_backup_of_git(repo)
    # Crash window: second rename done, marker still at "original-moved".
    _write_marker_manual(repo, "original-moved", "sources/a.md", backup)
    subprocess.run(["git", "filter-repo", "--force", "--path", "sources/a.md", "--invert-paths"],
                   cwd=repo, check=True, capture_output=True, text=True)
    assert recover_interrupted_erase(repo) == "completed"
    assert not _marker_path(repo).exists()
    assert not (repo / "sources" / "a.md").exists()
    assert (repo / "sources" / "keep.md").exists()
    assert (repo / "sources" / "keep.md").read_text() == "Keep\n"
