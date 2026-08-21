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

    n = erase_from_git_history(repo, "sources/secret.md")
    assert n == 2

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

    n = erase_from_git_history(repo, "sources/never-existed.md")
    assert n == 0
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

    erase_from_git_history(repo, "sources/a.md")

    backup_dir = repo / ".git.pre-erase-sources_a.md"
    assert backup_dir.is_dir()
    # the backup is a real, independently-usable git repo carrying the
    # ORIGINAL (pre-erase) history
    result = subprocess.run(["git", "--git-dir", str(backup_dir), "log", "--oneline"],
                            capture_output=True, text=True, timeout=10)
    assert "initial" in result.stdout


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

    n = erase_from_git_history(repo, "sources/only.md")
    assert n == 1
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
