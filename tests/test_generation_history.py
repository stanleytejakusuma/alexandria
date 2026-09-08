from pathlib import Path

import pytest

from alexandria.generation_history import HistorySnapshotError, snapshot_source_history


def test_snapshot_copies_staged_sources_outside_the_live_control_tree(tmp_path: Path) -> None:
    control = tmp_path / "control"
    staged = tmp_path / "stage"
    (control / "sources").mkdir(parents=True)
    (control / "sources" / "live.md").write_text("live")
    (staged / "sources").mkdir(parents=True)
    (staged / "sources" / "new.md").write_text("staged")
    (staged / "wiki").mkdir()

    snapshot = snapshot_source_history(control, staged, generation_id="g-2")

    assert (snapshot / "sources" / "new.md").read_text() == "staged"
    assert (control / "sources" / "live.md").read_text() == "live"
    assert not (control / "sources" / "new.md").exists()


def test_history_snapshot_refuses_to_overwrite_a_generation(tmp_path: Path) -> None:
    staged = tmp_path / "stage"
    (staged / "sources").mkdir(parents=True)
    (staged / "wiki").mkdir()
    snapshot_source_history(tmp_path, staged, generation_id="g-2")

    with pytest.raises(HistorySnapshotError, match="already exists"):
        snapshot_source_history(tmp_path, staged, generation_id="g-2")
