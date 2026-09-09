from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from alexandria.generation_pointer import activate_generation, resolve_generation
from alexandria.generation_publisher import PublishError, publish_generation, stage_generation
from alexandria.parity import ReconciliationPlan
from alexandria.reconciliation_import import ReconciliationImportError, stage_reconciliation


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _control_root(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "control"
    active = root / ".alexandria" / "generations" / "active"
    (active / "sources").mkdir(parents=True)
    (active / "wiki").mkdir()
    (active / ".alexandria" / "state").mkdir(parents=True)
    (active / ".alexandria" / "index").mkdir(parents=True)
    (active / ".alexandria" / "index" / "generation.json").write_text('{"generation": 1}')
    (active / "sources" / "local.md").write_text("local only")
    (active / "sources" / "conflict.md").write_text("mac version")
    activate_generation(root, active)
    return root, active


def _plan() -> ReconciliationPlan:
    return ReconciliationPlan(
        add_to_remote=("sources/local",),
        add_to_local=("sources/remote",),
        conflicts=(("sources/conflict", _sha("mac version"), _sha("nas version")),),
        local_addition_hashes={"sources/local": _sha("local only")},
        remote_addition_hashes={"sources/remote": _sha("nas only")},
        local_git_head="mac",
        remote_git_head="nas",
    )


def test_stage_reconciliation_preserves_local_adds_nas_and_replaces_approved_conflict(tmp_path: Path) -> None:
    root, active = _control_root(tmp_path)
    payloads = {"sources/remote": "nas only", "sources/conflict": "nas version"}

    staged = stage_reconciliation(root, "reconciled", _plan(), fetch=lambda source_id: payloads[source_id])

    assert (staged / "sources" / "local.md").read_text() == "local only"
    assert (staged / "sources" / "remote.md").read_text() == "nas only"
    assert (staged / "sources" / "conflict.md").read_text() == "nas version"
    assert (active / "sources" / "conflict.md").read_text() == "mac version"
    assert resolve_generation(root) == active
    assert (staged / ".alexandria" / "reconciliation-required.json").exists()
    with pytest.raises(PublishError, match="reindex"):
        publish_generation(root, staged, snapshot=lambda _stage: None)


def test_hash_mismatch_removes_candidate_and_never_switches_pointer(tmp_path: Path) -> None:
    root, active = _control_root(tmp_path)

    with pytest.raises(ReconciliationImportError, match="hash mismatch"):
        stage_reconciliation(root, "bad", _plan(), fetch=lambda _source_id: "tampered")

    assert not (root / ".alexandria" / "generations" / "bad").exists()
    assert resolve_generation(root) == active
