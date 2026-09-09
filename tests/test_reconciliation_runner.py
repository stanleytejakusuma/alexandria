from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from alexandria.generation_pointer import activate_generation, resolve_generation
from alexandria.parity import ReconciliationPlan
from alexandria.reconciliation_runner import ReconciliationStageError, stage_and_verify


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _root(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "control"
    active = root / ".alexandria" / "generations" / "active"
    (active / "sources").mkdir(parents=True)
    (active / "wiki").mkdir()
    (active / ".alexandria" / "state").mkdir(parents=True)
    (active / ".alexandria" / "index").mkdir(parents=True)
    (active / ".alexandria" / "index" / "generation.json").write_text('{"generation": 7}')
    (active / "sources" / "local.md").write_bytes(b"local")
    activate_generation(root, active)
    return root, active


def _plan() -> ReconciliationPlan:
    return ReconciliationPlan(
        add_to_remote=("sources/local",),
        add_to_local=("sources/remote",),
        conflicts=(),
        local_addition_hashes={"sources/local": _sha(b"local")},
        remote_addition_hashes={"sources/remote": _sha(b"nas")},
        local_git_head="mac", remote_git_head="nas",
    )


def _advance(stage: Path) -> None:
    (stage / ".alexandria" / "index").mkdir(parents=True, exist_ok=True)
    (stage / ".alexandria" / "index" / "generation.json").write_text('{"generation": 8}')


def test_stage_and_verify_requires_fresh_index_then_returns_unpublished_candidate(tmp_path: Path) -> None:
    root, active = _root(tmp_path)
    verified: list[Path] = []

    staged = stage_and_verify(
        root, "candidate", _plan(), fetch=lambda _id: b"nas", run_index=_advance,
        verify=lambda stage: verified.append(stage),
    )

    assert (staged / "sources" / "remote.md").read_bytes() == b"nas"
    assert not (staged / ".alexandria" / "reconciliation-required.json").exists()
    assert verified == [staged]
    assert resolve_generation(root) == active


def test_failed_reindex_keeps_marked_candidate_and_old_pointer(tmp_path: Path) -> None:
    root, active = _root(tmp_path)

    with pytest.raises(ReconciliationStageError, match="did not advance"):
        stage_and_verify(
            root, "stale", _plan(), fetch=lambda _id: b"nas",
            run_index=lambda _stage: None, verify=lambda _stage: None,
        )

    staged = root / ".alexandria" / "generations" / "stale"
    assert (staged / ".alexandria" / "reconciliation-required.json").exists()
    assert resolve_generation(root) == active


def test_failed_verification_keeps_marked_candidate_and_old_pointer(tmp_path: Path) -> None:
    root, active = _root(tmp_path)

    with pytest.raises(ReconciliationStageError, match="candidate verification failed"):
        stage_and_verify(root, "bad-verify", _plan(), fetch=lambda _id: b"nas", run_index=_advance,
                         verify=lambda _stage: (_ for _ in ()).throw(RuntimeError("retrieval missing")))

    staged = root / ".alexandria" / "generations" / "bad-verify"
    assert (staged / ".alexandria" / "reconciliation-required.json").exists()
    assert resolve_generation(root) == active


def test_pointer_change_during_verification_keeps_marker(tmp_path: Path) -> None:
    root, active = _root(tmp_path)

    def move_pointer(_stage: Path) -> None:
        newer = root / ".alexandria" / "generations" / "other"
        (newer / "sources").mkdir(parents=True)
        (newer / "wiki").mkdir()
        (newer / ".alexandria" / "state").mkdir(parents=True)
        (newer / ".alexandria" / "index").mkdir(parents=True)
        (newer / ".alexandria" / "index" / "generation.json").write_text('{"generation": 9}')
        (newer / "sources" / "local.md").write_bytes(b"local")
        activate_generation(root, newer)

    with pytest.raises(ReconciliationStageError, match="active generation changed"):
        stage_and_verify(root, "raced", _plan(), fetch=lambda _id: b"nas", run_index=_advance,
                         verify=move_pointer)

    staged = root / ".alexandria" / "generations" / "raced"
    assert (staged / ".alexandria" / "reconciliation-required.json").exists()
    assert resolve_generation(root) != active
