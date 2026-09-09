from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from alexandria.parity import ReconciliationPlan


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "stage-reconciliation.py"


def _load():
    spec = importlib.util.spec_from_file_location("stage_reconciliation_script", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_remote_fetcher_uses_only_the_approved_ids_and_a_real_safe_host(tmp_path, monkeypatch) -> None:
    module = _load()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    ssh = fake_bin / "ssh"
    ssh.write_text("#!/bin/sh\ncat > \"$SSH_IDS\"\nprintf '%s' '{\"sources/remote\": \"cmVtb3Rl\"}'\n")
    ssh.chmod(0o755)
    ids = tmp_path / "ids.json"
    monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("SSH_IDS", str(ids))

    fetch = module._remote_fetcher("nas.example", "/remote/corpus", {"sources/remote"})

    assert fetch("sources/remote") == b"remote"
    assert json.loads(ids.read_text()) == ["sources/remote"]
    with pytest.raises(RuntimeError, match="invalid remote host"):
        module._remote_fetcher("-oProxyCommand=bad", "/remote/corpus", set())


def test_script_passes_real_verifier_baseline_and_never_publishes(tmp_path, monkeypatch, capsys) -> None:
    module = _load()
    plan = ReconciliationPlan(
        add_to_remote=(), add_to_local=("sources/remote",), conflicts=(),
        local_addition_hashes={}, remote_addition_hashes={"sources/remote": "a" * 64},
        local_git_head="local", remote_git_head="remote",
    )
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan.to_json()))
    packet = tmp_path / "packet.md"
    packet.write_text("reviewed")
    stage = tmp_path / "stage"
    calls: list[list[str]] = []

    monkeypatch.setattr(module, "resolve_generation", lambda _: tmp_path / "active")
    monkeypatch.setattr(module, "validate_generation", lambda *_args, **_kwargs: {"documents": 7, "generation": 11})
    monkeypatch.setattr(module, "_remote_fetcher", lambda *_args: lambda _id: b"remote")
    monkeypatch.setattr(module, "subprocess", SimpleNamespace(
        run=lambda command, **_kwargs: calls.append(command) or SimpleNamespace(returncode=0)
    ))

    def fake_stage(*_args, run_index, verify, **_kwargs):
        run_index(stage)
        verify(stage)
        return stage

    monkeypatch.setattr(module, "stage_and_verify", fake_stage)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--control-root", str(tmp_path), "--plan", str(plan_path),
                                       "--remote-host", "nas", "--remote-corpus", "/corpus",
                                       "--generation-id", "candidate", "--review-packet", str(packet)])

    assert module.main() == 0
    verify_call = next(call for call in calls if "verify-loop-run.py" in " ".join(call))
    assert verify_call[verify_call.index("--docs-before") + 1] == "7"
    assert verify_call[verify_call.index("--generation-before") + 1] == "11"
    assert "--binary" in verify_call
    assert not any("publish-staged-generation.py" in " ".join(call) for call in calls)
    assert "Human-fired publish command:" in capsys.readouterr().out
