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
    if (
        not relative.parts
        or relative.is_absolute()
        or relative.parts[0] not in {"sources", "wiki"}
        or any(part in {".", ".."} for part in relative.parts)
        or source_id.endswith(".md")
    ):
        raise ReconciliationImportError(f"invalid source id: {source_id!r}")
    # Parity strips only the final .md: `sources/photo.jpg.md` is correctly
    # represented as `sources/photo.jpg`, so append rather than reject suffixes.
    destination = (stage / f"{source_id}.md").resolve()
    allowed_root = (stage / relative.parts[0]).resolve()
    if allowed_root not in destination.parents:
        raise ReconciliationImportError(f"source id escapes source root: {source_id!r}")
    return destination


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _replace_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.reconcile-tmp")
    try:
        temporary.write_bytes(content)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _validate_plan(plan: ReconciliationPlan) -> None:
    local_additions = set(plan.add_to_remote)
    remote_additions = set(plan.add_to_local)
    conflicts = {source_id for source_id, _local, _remote in plan.conflicts}
    if local_additions != set(plan.local_addition_hashes):
        raise ReconciliationImportError("local addition IDs do not match their reviewed hashes")
    if remote_additions != set(plan.remote_addition_hashes):
        raise ReconciliationImportError("remote addition IDs do not match their reviewed hashes")
    if local_additions & remote_additions or local_additions & conflicts or remote_additions & conflicts:
        raise ReconciliationImportError("reconciliation plan assigns a source to conflicting actions")


def stage_reconciliation(
    control_root: str | Path,
    generation_id: str,
    plan: ReconciliationPlan,
    *,
    fetch: Callable[[str], bytes],
) -> Path:
    """Create an unpublished candidate from a human-approved, hash-bound plan.

    The caller supplies a read-only remote byte fetcher. This function has no
    pointer, Git, index, sync, or publish operation. It writes the reindex marker
    *before* fetching/mutating: an interrupted or partly-cleaned candidate cannot
    be activated with its cloned stale index.
    """
    _validate_plan(plan)
    staged = stage_generation(control_root, generation_id)
    try:
        marker = staged / ".alexandria" / "reconciliation-required.json"
        _replace_bytes(marker, json.dumps({
            "requires_reindex": True,
            "remote_additions": sorted(plan.remote_addition_hashes),
            "nas_conflicts": [source_id for source_id, _local, _remote in plan.conflicts],
        }, sort_keys=True).encode() + b"\n")

        for source_id, expected in plan.local_addition_hashes.items():
            current = _destination(staged, source_id)
            if not current.exists() or _sha256(current.read_bytes()) != expected:
                raise ReconciliationImportError(f"local preservation hash mismatch: {source_id}")

        for source_id, expected in plan.remote_addition_hashes.items():
            content = fetch(source_id)
            if _sha256(content) != expected:
                raise ReconciliationImportError(f"remote hash mismatch: {source_id}")
            _replace_bytes(_destination(staged, source_id), content)

        for source_id, local_expected, remote_expected in plan.conflicts:
            current = _destination(staged, source_id)
            if not current.exists() or _sha256(current.read_bytes()) != local_expected:
                raise ReconciliationImportError(f"conflict local hash mismatch: {source_id}")
            content = fetch(source_id)
            if _sha256(content) != remote_expected:
                raise ReconciliationImportError(f"remote hash mismatch: {source_id}")
            _replace_bytes(current, content)
        return staged
    except BaseException:
        shutil.rmtree(staged, ignore_errors=True)
        raise
