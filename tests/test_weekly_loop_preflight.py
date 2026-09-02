"""The weekly loop must refuse to run on a broken interpreter.

2026-08-30 incident: every sync/index step died with
`ModuleNotFoundError: No module named 'alexandria'` while the loop still
reached its snapshot step and committed 3,053 files. The venv's editable
install pointed at `/private/tmp/alexandria-procurement-floor/src` -- a
throwaway worktree that macOS reaped from /tmp. The console script survived
(it hardcodes an absolute sys.path insert) but `python -m alexandria.cli`
did not.

Two independent defects made that a silent week:

1. The loop invoked the CLI via `python -m`, the one entry point that
   depends on the editable .pth resolving.
2. Nothing checked the interpreter BEFORE doing work, so five steps failed
   one after another and the run still produced a commit.

These tests pin the fix: a preflight that fails loud and early, and no
`python -m` invocation anywhere in the script.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
LOOP = REPO / "scripts" / "run-weekly-loop.sh"


def _script() -> str:
    return LOOP.read_text()


def test_the_loop_never_invokes_the_cli_through_dash_m() -> None:
    """`python -m alexandria.cli` depends on the editable install resolving.

    The console script (`.venv/bin/alexandria`) carries an absolute path and
    keeps working even when the .pth target is gone, so it is the only safe
    entry point for an unattended run.
    """
    offenders = [
        line.strip()
        for line in _script().splitlines()
        if not line.lstrip().startswith("#")
        and re.search(r"-m\s+alexandria(\.|\s|$)", line)
    ]
    assert offenders == [], (
        "run-weekly-loop.sh must call the console script, not `python -m`; "
        f"offending lines: {offenders}"
    )


def test_the_loop_preflights_the_cli_before_doing_any_work() -> None:
    """A broken interpreter must stop the run before the first sync.

    Without this, five steps fail in sequence and the snapshot step still
    commits -- the exact shape of the 2026-08-30 run.
    """
    text = _script()
    assert "preflight" in text.lower(), "no preflight step found"

    preflight_at = text.lower().index("preflight")
    first_sync_at = text.index("sync pi-sessions")
    assert preflight_at < first_sync_at, (
        "preflight must run BEFORE the first sync, otherwise a broken "
        "interpreter still burns through every step"
    )


def test_preflight_failure_exits_nonzero_and_skips_the_snapshot() -> None:
    """Run the real script against a deliberately broken interpreter.

    This is the vacuity-proof half: it executes the shipped script rather
    than reading it, so a preflight that is present but toothless fails here.
    """
    import os
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        corpus = tmp / "corpus"
        (corpus / ".alexandria" / "loop").mkdir(parents=True)
        (corpus / "sources").mkdir()
        (corpus / "wiki").mkdir()
        subprocess.run(["git", "init", "-q", str(corpus)], check=True)

        # A venv whose CLI is broken exactly the way the real one was: the
        # binary exists but fails on import.
        fake_repo = tmp / "repo"
        (fake_repo / ".venv" / "bin").mkdir(parents=True)
        cli = fake_repo / ".venv" / "bin" / "alexandria"
        cli.write_text(
            "#!/bin/bash\n"
            "echo \"ModuleNotFoundError: No module named 'alexandria'\" >&2\n"
            "exit 1\n"
        )
        cli.chmod(0o755)
        (fake_repo / "scripts").mkdir()

        env = {
            **os.environ,
            "HOME": str(tmp),
            "ALEXANDRIA_CORPUS": str(corpus),
            "ALEXANDRIA_BASE_URL": "http://127.0.0.1:1",
            "ALEXANDRIA_LLM_KEY": "not-a-real-key",
            "ALEXANDRIA_REPO": str(fake_repo),
            "ALEXANDRIA_NOTIFIER": "/nonexistent",
        }
        proc = subprocess.run(
            ["/bin/bash", str(LOOP)],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )

        assert proc.returncode != 0, (
            "a broken CLI must fail the run; instead it exited 0"
        )

        digest = (corpus / ".alexandria" / "loop" / "weekly-digest.md").read_text()
        assert "PREFLIGHT" in digest.upper(), (
            f"preflight failure must be recorded in the digest; got:\n{digest}"
        )

        log = subprocess.run(
            ["git", "-C", str(corpus), "log", "--oneline"],
            capture_output=True,
            text=True,
        )
        assert log.stdout.strip() == "", (
            "a preflight failure must not produce a corpus commit; "
            f"got: {log.stdout!r}"
        )


def test_the_repo_path_is_overridable_for_testing() -> None:
    """The script must accept ALEXANDRIA_REPO so the failure path is testable."""
    assert "ALEXANDRIA_REPO" in _script(), (
        "REPO must be overridable, otherwise the broken-interpreter path "
        "cannot be exercised without touching the real venv"
    )
