"""Run required weekly work against an unpublished generation only."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from .generation_publisher import publish_generation, stage_generation


class StagedLoopError(RuntimeError):
    pass


def run_staged_loop(
    control_root: str | Path,
    *,
    generation_id: str,
    work: Callable[[Path], None],
    snapshot: Callable[[Path], None],
) -> Path:
    """Stage, run required work, then publish only if every gate succeeds."""
    staged = stage_generation(control_root, generation_id)
    try:
        work(staged)
    except Exception as exc:
        raise StagedLoopError(f"required staged work failed; generation was not published: {exc}") from exc
    return publish_generation(control_root, staged, snapshot=snapshot)
