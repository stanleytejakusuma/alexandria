"""Staged generation publication ordering, without touching a live corpus."""
from __future__ import annotations

from pathlib import Path

import pytest

from alexandria.generation_pointer import activate_generation, resolve_generation
from alexandria.generation_publisher import PublishError, publish_generation, stage_generation


def _control_root(tmp_path: Path) -> Path:
    (tmp_path / "sources").mkdir()
    (tmp_path / "sources" / "note.md").write_text("old source", encoding="utf-8")
    (tmp_path / "wiki").mkdir()
    (tmp_path / ".alexandria" / "state").mkdir(parents=True)
    (tmp_path / ".alexandria" / "state" / "connector.json").write_text("old state")
    return tmp_path


def _built(stage: Path, generation: int) -> None:
    (stage / ".alexandria" / "index").mkdir(parents=True)
    (stage / ".alexandria" / "index" / "generation.json").write_text(
        f'{{"generation": {generation}}}', encoding="utf-8"
    )


def test_stage_copies_reader_inputs_but_not_git_or_unrelated_runtime_state(tmp_path: Path) -> None:
    root = _control_root(tmp_path)
    (root / ".git").mkdir()
    (root / ".alexandria" / "cache").mkdir()
    (root / ".alexandria" / "cache" / "ignored").write_text("cache")
    staged = stage_generation(root, "g-1")
    assert (staged / "sources" / "note.md").read_text() == "old source"
    assert (staged / ".alexandria" / "state" / "connector.json").read_text() == "old state"
    assert not (staged / ".git").exists()
    assert not (staged / ".alexandria" / "cache").exists()


def test_processing_a_staged_source_cannot_mutate_the_control_root(tmp_path: Path) -> None:
    root = _control_root(tmp_path)
    staged = stage_generation(root, "g-1")
    (staged / "sources" / "note.md").write_text("new staged source", encoding="utf-8")
    assert (root / "sources" / "note.md").read_text(encoding="utf-8") == "old source"


def test_failed_snapshot_never_switches_the_live_pointer(tmp_path: Path) -> None:
    root = _control_root(tmp_path)
    old, new = stage_generation(root, "old"), stage_generation(root, "new")
    _built(old, 1); _built(new, 2); activate_generation(root, old)
    with pytest.raises(PublishError, match="snapshot failed"):
        publish_generation(root, new, snapshot=lambda _stage: (_ for _ in ()).throw(RuntimeError("git snapshot failed")))
    assert resolve_generation(root) == old


def test_unvalidated_generation_cannot_be_published(tmp_path: Path) -> None:
    root = _control_root(tmp_path)
    old, new = stage_generation(root, "old"), stage_generation(root, "new")
    _built(old, 1); activate_generation(root, old)
    with pytest.raises(PublishError, match="generation validation failed"):
        publish_generation(root, new, snapshot=lambda _stage: None)
    assert resolve_generation(root) == old


def test_crash_after_snapshot_leaves_old_generation_live(tmp_path: Path) -> None:
    root = _control_root(tmp_path)
    old, new = stage_generation(root, "old"), stage_generation(root, "new")
    _built(old, 1); _built(new, 2); activate_generation(root, old)

    with pytest.raises(SystemExit, match="137"):
        publish_generation(root, new, snapshot=lambda _stage: (_ for _ in ()).throw(SystemExit(137)))

    assert resolve_generation(root) == old


def test_snapshot_finishes_before_the_new_generation_becomes_live(tmp_path: Path) -> None:
    root = _control_root(tmp_path)
    old, new = stage_generation(root, "old"), stage_generation(root, "new")
    _built(old, 1); _built(new, 2); activate_generation(root, old)
    observed: list[Path] = []
    publish_generation(root, new, snapshot=lambda staged: observed.append(resolve_generation(root)))
    assert observed == [old]
    assert resolve_generation(root) == new
