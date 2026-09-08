"""Git-trackable source history for published staged generations."""
from __future__ import annotations

import shutil
from pathlib import Path


class HistorySnapshotError(RuntimeError):
    pass


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
