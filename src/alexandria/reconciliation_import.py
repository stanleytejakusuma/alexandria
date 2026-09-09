"""Stage a hash-bound reconciliation candidate without publishing it."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Callable
from pathlib import Path

from .generation_publisher import stage_generation
from .parity import ReconciliationPlan


class ReconciliationImportError(RuntimeError):
    """A reviewed reconciliation plan could not be staged safely."""


def _destination(stage: Path, source_id: str) -> Path:
    relative = Path(source_id)
    if relative.suffix or relative.parts[0] not in {"sources", "wiki"}:
        raise ReconciliationImportError(f"invalid source id: {source_id!r}")
    destination = (stage / relative).with_suffix(".md").resolve()
    if stage.resolve() not in destination.parents:
        raise ReconciliationImportError(f"source id escapes staged generation: {source_id!r}")
    return destination


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _replace_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.reconcile-tmp")
    try:
        temporary.write_text(text)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def stage_reconciliation(
    control_root: str | Path,
    generation_id: str,
    plan: ReconciliationPlan,
    *,
    fetch: Callable[[str], str],
) -> Path:
    """Create an unpublished candidate from a human-approved, hash-bound plan.

    The caller supplies a read-only remote fetcher. This function has no pointer,
    Git, index, sync, or publish operation; it deliberately leaves a marker that
    requires a fresh index before the existing publisher may activate the candidate.
    """
    staged = stage_generation(control_root, generation_id)
    try:
        for source_id, expected in plan.local_addition_hashes.items():
            current = _destination(staged, source_id)
            if not current.exists() or _sha256(current.read_text()) != expected:
                raise ReconciliationImportError(f"local preservation hash mismatch: {source_id}")

        incoming = dict(plan.remote_addition_hashes)
        incoming.update({source_id: remote for source_id, _local, remote in plan.conflicts})
        for source_id, expected in incoming.items():
            text = fetch(source_id)
            if _sha256(text) != expected:
                raise ReconciliationImportError(f"remote hash mismatch: {source_id}")
            _replace_text(_destination(staged, source_id), text)

        marker = staged / ".alexandria" / "reconciliation-required.json"
        _replace_text(marker, json.dumps({
            "requires_reindex": True,
            "remote_additions": sorted(plan.remote_addition_hashes),
            "nas_conflicts": [source_id for source_id, _local, _remote in plan.conflicts],
        }, sort_keys=True) + "\n")
        return staged
    except Exception:
        shutil.rmtree(staged, ignore_errors=True)
        raise
