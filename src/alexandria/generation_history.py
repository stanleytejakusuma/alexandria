"""Git-trackable source history for published staged generations."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


class HistorySnapshotError(RuntimeError):
    pass


def snapshot_and_commit_history(control_root: str | Path, staged: str | Path, *, generation_id: str) -> Path:
    """Create a source snapshot and commit it before a pointer can flip."""
    control = Path(control_root)
    target = snapshot_source_history(control, staged, generation_id=generation_id)
    relative = target.relative_to(control)
    try:
        subprocess.run(["git", "-C", str(control), "add", "--", str(relative)], check=True)
        subprocess.run(
            ["git", "-C", str(control), "commit", "-m", f"weekly generation {generation_id}"],
            check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise HistorySnapshotError(f"history snapshot was not committed: {exc.stderr or exc}") from exc
    return target


def snapshot_source_history(control_root: str | Path, staged: str | Path, *, generation_id: str) -> Path:
    """Copy staged source/wiki inputs to a new, non-live history candidate.

    Git owns this destination at the control root; readers never do. A failed
    copy is removed, and an existing generation is never overwritten.
    """
    control, staged = Path(control_root), Path(staged)
    target = control / "history" / "generations" / generation_id
    if target.exists():
        raise HistorySnapshotError(f"history generation already exists: {generation_id}")
    try:
        target.mkdir(parents=True)
        for name in ("sources", "wiki"):
            source = staged / name
            if source.exists():
                shutil.copytree(source, target / name)
        return target
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise
