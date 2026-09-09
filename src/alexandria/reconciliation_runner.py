"""Stage and verify a reconciliation candidate without publishing it."""
from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path

from .generation_pointer import resolve_generation
from .generation_validation import validate_generation
from .parity import ReconciliationPlan
from .reconciliation_import import stage_reconciliation


class ReconciliationStageError(RuntimeError):
    """A staged reconciliation candidate is not ready for human publication."""


def _generation_number(root: Path) -> int:
    try:
        return int(json.loads((root / ".alexandria" / "index" / "generation.json").read_text())["generation"])
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ReconciliationStageError("candidate has no readable index generation") from exc


def _clear_marker(stage: Path) -> None:
    marker = stage / ".alexandria" / "reconciliation-required.json"
    marker.unlink()
    directory_fd = os.open(marker.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def stage_and_verify(
    control_root: str | Path,
    generation_id: str,
    plan: ReconciliationPlan,
    *,
    fetch: Callable[[str], bytes],
    run_index: Callable[[Path], None],
    verify: Callable[[Path], None],
) -> Path:
    """Stage, fresh-index, and verify an unpublished reconciliation candidate.

    `run_index` and `verify` are injected so the live shell can be thin and the
    invariant is unit-testable. This function deliberately never invokes
    `publish_generation` or `activate_generation`.
    """
    control_root = Path(control_root)
    active = resolve_generation(control_root)
    baseline = validate_generation(active, docs_before=0, generation_before=0)
    staged = stage_reconciliation(control_root, generation_id, plan, fetch=fetch)
    try:
        run_index(staged)
        generation = _generation_number(staged)
        expected_documents = baseline["documents"] + len(plan.add_to_local)
        documents = sum(
            1
            for directory in ("sources", "wiki")
            for path in (staged / directory).rglob("*.md")
            if not path.name.startswith("._")
        )
        if generation <= baseline["generation"]:
            raise ReconciliationStageError("candidate index generation did not advance")
        if documents < expected_documents:
            raise ReconciliationStageError("candidate is missing reviewed NAS-only documents")
        verify(staged)
        # A candidate must still descend from the generation whose evidence we
        # measured. If another publisher moved the pointer meanwhile, leave the
        # marker intact; fresh publication will reject rather than blessing a
        # candidate against an obsolete baseline.
        if resolve_generation(control_root) != active:
            raise ReconciliationStageError("active generation changed during staged verification")
        _clear_marker(staged)
        return staged
    except Exception as exc:
        if isinstance(exc, ReconciliationStageError):
            raise
        raise ReconciliationStageError(f"candidate verification failed: {exc}") from exc
