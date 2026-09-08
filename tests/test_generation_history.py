from pathlib import Path
import subprocess

import pytest

from alexandria.generation_history import HistorySnapshotError, snapshot_and_commit_history, snapshot_source_history


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


def test_history_snapshot_is_committed_before_publication(tmp_path: Path) -> None:
    control, staged = tmp_path / "control", tmp_path / "stage"
    control.mkdir()
    subprocess.run(["git", "-C", str(control), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(control), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(control), "config", "user.name", "Test"], check=True)
    (control / "README").write_text("history")
    subprocess.run(["git", "-C", str(control), "add", "README"], check=True)
    subprocess.run(["git", "-C", str(control), "commit", "-qm", "initial"], check=True)
    (staged / "sources").mkdir(parents=True)
    (staged / "sources" / "new.md").write_text("staged")
    (staged / "wiki").mkdir()

    target = snapshot_and_commit_history(control, staged, generation_id="g-2")

    assert (target / "sources" / "new.md").exists()
    assert "weekly generation g-2" in subprocess.run(
        ["git", "-C", str(control), "log", "-1", "--format=%s"], capture_output=True, text=True, check=True
    ).stdout


def test_history_snapshot_refuses_to_overwrite_a_generation(tmp_path: Path) -> None:
    staged = tmp_path / "stage"
    (staged / "sources").mkdir(parents=True)
    (staged / "wiki").mkdir()
    snapshot_source_history(tmp_path, staged, generation_id="g-2")

    with pytest.raises(HistorySnapshotError, match="already exists"):
        snapshot_source_history(tmp_path, staged, generation_id="g-2")
