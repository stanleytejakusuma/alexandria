"""Bounded weekly-loop behavior.

A healthy CLI can still make the loop operationally dead: on 2026-09-02,
838 pending Pi-session bursts estimated 141+ minutes. The loop must bound that
work and must never snapshot a corpus after a required sync/index failure.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LOOP = REPO / "scripts" / "run-weekly-loop.sh"


def test_pi_session_sync_has_a_configurable_batch_limit_and_timeout() -> None:
    text = LOOP.read_text()
    assert "ALEXANDRIA_PI_SESSIONS_LIMIT" in text
    assert "--limit \"$PI_SESSIONS_LIMIT\"" in text
    assert "ALEXANDRIA_STEP_TIMEOUT_SECONDS" in text
    assert "gtimeout" in text or "timeout" in text


def test_required_sync_failure_never_snapshots_partial_corpus() -> None:
    """A valid CLI that fails pi-session sync must halt before git commit.

    This differs from the broken-interpreter test: it proves a runtime failure
    after preflight cannot recreate the Aug-30 shape (sources staged, snapshot
    committed, index absent).
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        corpus = tmp / "corpus"
        (corpus / ".alexandria" / "loop").mkdir(parents=True)
        (corpus / ".alexandria" / "index").mkdir()
        (corpus / ".alexandria" / "index" / "generation.json").write_text('{"generation": 1}')
        (corpus / "sources").mkdir()
        (corpus / "wiki").mkdir()
        (corpus / "sources" / "existing.md").write_text("# existing\n")
        subprocess.run(["git", "init", "-q", str(corpus)], check=True)
        subprocess.run(["git", "-C", str(corpus), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(corpus), "-c", "user.email=test@example.com", "-c", "user.name=test", "commit", "-qm", "baseline"],
            check=True,
        )

        fake_repo = tmp / "repo"
        bin_dir = fake_repo / ".venv" / "bin"
        bin_dir.mkdir(parents=True)
        cli = bin_dir / "alexandria"
        cli.write_text(
            "#!/bin/bash\n"
            "if [ \"$1\" = \"--help\" ]; then exit 0; fi\n"
            "if [ \"$3\" = \"sync\" ] && [ \"$4\" = \"pi-sessions\" ]; then\n"
            "  echo 'simulated connector failure' >&2; exit 75\n"
            "fi\n"
            "exit 0\n"
        )
        cli.chmod(0o755)

        env = {
            **os.environ,
            "HOME": str(tmp),
            "ALEXANDRIA_CORPUS": str(corpus),
            "ALEXANDRIA_BASE_URL": "http://127.0.0.1:1",
            "ALEXANDRIA_LLM_KEY": "not-a-real-key",
            "ALEXANDRIA_REPO": str(fake_repo),
            "ALEXANDRIA_NOTIFIER": "/nonexistent",
            "ALEXANDRIA_TIMEOUT": "/opt/homebrew/bin/timeout",
        }
        proc = subprocess.run(
            ["/bin/bash", str(LOOP)], env=env, capture_output=True, text=True, timeout=30
        )

        assert proc.returncode != 0
        digest = (corpus / ".alexandria" / "loop" / "weekly-digest.md").read_text()
        assert "[FAIL] sync pi-sessions" in digest
        assert "snapshot skipped" in digest
        log = subprocess.check_output(["git", "-C", str(corpus), "log", "--oneline"], text=True)
        assert len(log.strip().splitlines()) == 1, log
