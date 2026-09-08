"""Run required weekly work against an unpublished generation only."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from .generation_history import snapshot_and_commit_history
from .generation_publisher import publish_generation, stage_generation


class StagedLoopError(RuntimeError):
    pass


def run_staged_loop(
    control_root: str | Path,
    *,
    generation_id: str,
    work: Callable[[Path], None],
    snapshot: Callable[[Path], None] | None = None,
) -> Path:
    """Stage, run required work, commit history, then publish on success."""
    control_root = Path(control_root)
    staged = stage_generation(control_root, generation_id)
    try:
        work(staged)
    except Exception as exc:
        raise StagedLoopError(f"required staged work failed; generation was not published: {exc}") from exc
    if snapshot is None:
        snapshot = lambda candidate: snapshot_and_commit_history(
            control_root, candidate, generation_id=generation_id
        )
    return publish_generation(control_root, staged, snapshot=snapshot)
