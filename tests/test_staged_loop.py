from pathlib import Path

import pytest

from alexandria.generation_pointer import resolve_generation
from alexandria.staged_loop import StagedLoopError, run_staged_loop


def _root(tmp_path: Path) -> Path:
    (tmp_path / "sources").mkdir()
    (tmp_path / "sources" / "old.md").write_text("old")
    (tmp_path / "wiki").mkdir()
    return tmp_path


def _build(stage: Path) -> None:
    (stage / ".alexandria" / "index").mkdir(parents=True)
    (stage / ".alexandria" / "index" / "generation.json").write_text('{"generation": 1}')


def test_required_work_runs_only_in_stage_then_publishes(tmp_path: Path) -> None:
    root, seen = _root(tmp_path), []
    def work(stage: Path) -> None:
        seen.append(stage)
        (stage / "sources" / "new.md").write_text("new")
        _build(stage)

    active = run_staged_loop(root, generation_id="g-1", work=work, snapshot=lambda _stage: None)

    assert seen == [active]
    assert resolve_generation(root) == active
    assert not (root / "sources" / "new.md").exists()


def test_required_work_failure_cannot_publish(tmp_path: Path) -> None:
    root = _root(tmp_path)
    with pytest.raises(StagedLoopError, match="required staged work failed"):
        run_staged_loop(root, generation_id="g-1", work=lambda _stage: (_ for _ in ()).throw(RuntimeError("timeout")), snapshot=lambda _stage: None)
    assert not (root / ".alexandria" / "current-generation.json").exists()
