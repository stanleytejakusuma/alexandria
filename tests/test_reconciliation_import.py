from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from alexandria.generation_pointer import activate_generation, resolve_generation
from alexandria.generation_publisher import PublishError, publish_generation
from alexandria.parity import ReconciliationPlan
from alexandria.reconciliation_import import ReconciliationImportError, plan_from_json, stage_reconciliation


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _control_root(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "control"
    active = root / ".alexandria" / "generations" / "active"
    (active / "sources").mkdir(parents=True)
    (active / "wiki").mkdir()
    (active / ".alexandria" / "state").mkdir(parents=True)
    (active / ".alexandria" / "index").mkdir(parents=True)
    (active / ".alexandria" / "index" / "generation.json").write_text('{"generation": 1}')
    (active / "sources" / "local.md").write_bytes(b"local only\r\n")
    (active / "sources" / "conflict.md").write_bytes(b"mac version\r\n")
    activate_generation(root, active)
    return root, active


def _plan() -> ReconciliationPlan:
    return ReconciliationPlan(
        add_to_remote=("sources/local",),
        add_to_local=("sources/photo.jpg",),
        conflicts=(("sources/conflict", _sha(b"mac version\r\n"), _sha(b"nas version\r\n")),),
        local_addition_hashes={"sources/local": _sha(b"local only\r\n")},
        remote_addition_hashes={"sources/photo.jpg": _sha(b"nas only\r\n")},
        local_git_head="mac",
        remote_git_head="nas",
    )


def test_stage_reconciliation_preserves_bytes_adds_nas_and_replaces_approved_conflict(tmp_path: Path) -> None:
    root, active = _control_root(tmp_path)
    payloads = {"sources/photo.jpg": b"nas only\r\n", "sources/conflict": b"nas version\r\n"}

    staged = stage_reconciliation(root, "reconciled", _plan(), fetch=lambda source_id: payloads[source_id])

    assert (staged / "sources" / "local.md").read_bytes() == b"local only\r\n"
    assert (staged / "sources" / "photo.jpg.md").read_bytes() == b"nas only\r\n"
    assert (staged / "sources" / "conflict.md").read_bytes() == b"nas version\r\n"
    assert (active / "sources" / "conflict.md").read_bytes() == b"mac version\r\n"
    assert resolve_generation(root) == active
    assert (staged / ".alexandria" / "reconciliation-required.json").exists()
    with pytest.raises(PublishError, match="reindex"):
        publish_generation(root, staged, snapshot=lambda _stage: None)


def test_conflict_local_hash_drift_removes_candidate_and_never_switches_pointer(tmp_path: Path) -> None:
    root, active = _control_root(tmp_path)
    (active / "sources" / "conflict.md").write_bytes(b"unreviewed drift")

    payloads = {"sources/photo.jpg": b"nas only\r\n", "sources/conflict": b"nas version\r\n"}
    with pytest.raises(ReconciliationImportError, match="conflict local hash mismatch"):
        stage_reconciliation(root, "drift", _plan(), fetch=lambda source_id: payloads[source_id])

    assert not (root / ".alexandria" / "generations" / "drift").exists()
    assert resolve_generation(root) == active


def test_remote_hash_mismatch_removes_candidate_and_never_switches_pointer(tmp_path: Path) -> None:
    root, active = _control_root(tmp_path)

    with pytest.raises(ReconciliationImportError, match="remote hash mismatch"):
        stage_reconciliation(root, "bad", _plan(), fetch=lambda _source_id: b"tampered")

    assert not (root / ".alexandria" / "generations" / "bad").exists()
    assert resolve_generation(root) == active


@pytest.mark.parametrize("source_id", ["", "sources/../.alexandria/state/x", "/sources/x", "sources/file.md"])
def test_invalid_source_ids_fail_closed(tmp_path: Path, source_id: str) -> None:
    root, active = _control_root(tmp_path)
    plan = ReconciliationPlan(
        add_to_remote=(), add_to_local=(source_id,), conflicts=(),
        local_addition_hashes={}, remote_addition_hashes={source_id: _sha(b"x")},
        local_git_head="mac", remote_git_head="nas",
    )
    with pytest.raises(ReconciliationImportError, match="invalid source id"):
        stage_reconciliation(root, "bad-id", plan, fetch=lambda _source_id: b"x")
    assert not (root / ".alexandria" / "generations" / "bad-id").exists()
    assert resolve_generation(root) == active


def test_plan_from_json_requires_hash_bound_non_executable_review() -> None:
    plan = _plan()
    raw = plan.to_json()
    parsed = plan_from_json(raw)
    assert parsed == plan
    raw["apply"] = True
    with pytest.raises(ReconciliationImportError, match="non-executable"):
        plan_from_json(raw)


def test_plan_hash_maps_must_exactly_match_reviewed_directional_ids(tmp_path: Path) -> None:
    root, active = _control_root(tmp_path)
    plan = ReconciliationPlan(
        add_to_remote=(), add_to_local=("sources/remote",), conflicts=(),
        local_addition_hashes={}, remote_addition_hashes={},
        local_git_head="mac", remote_git_head="nas",
    )
    with pytest.raises(ReconciliationImportError, match="remote addition IDs"):
        stage_reconciliation(root, "schema-drift", plan, fetch=lambda _source_id: b"x")
    assert not (root / ".alexandria" / "generations" / "schema-drift").exists()
    assert resolve_generation(root) == active
