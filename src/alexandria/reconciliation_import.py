"""Stage a hash-bound reconciliation candidate without publishing it."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path

from .generation_publisher import stage_generation
from .parity import ReconciliationPlan


class ReconciliationImportError(RuntimeError):
    """A reviewed reconciliation plan could not be staged safely."""


def plan_from_json(data: Mapping[str, object]) -> ReconciliationPlan:
    """Decode only the non-executable, hash-bound review artifact."""
    if not isinstance(data, Mapping):
        raise ReconciliationImportError("reconciliation plan must be a JSON object")
    if data.get("apply") is not False or data.get("requires_operator_confirmation") is not True:
        raise ReconciliationImportError("reconciliation plan must be a non-executable reviewed artifact")
    try:
        raw_remote, raw_local = data["add_to_remote"], data["add_to_local"]
        local_hashes, remote_hashes = data["local_addition_hashes"], data["remote_addition_hashes"]
        raw_conflicts, history = data["conflicts"], data["source_history"]
    except KeyError as exc:
        raise ReconciliationImportError(f"reconciliation plan missing {exc.args[0]}") from exc
    if (not isinstance(raw_remote, (list, tuple)) or not isinstance(raw_local, (list, tuple))
            or not all(isinstance(value, str) for value in (*raw_remote, *raw_local))
            or len(set(raw_remote)) != len(raw_remote) or len(set(raw_local)) != len(raw_local)):
        raise ReconciliationImportError("reconciliation plan IDs must be unique strings")
    if not isinstance(local_hashes, dict) or not isinstance(remote_hashes, dict) or not isinstance(raw_conflicts, list) or not isinstance(history, dict):
        raise ReconciliationImportError("reconciliation plan has invalid hash sections")
    def valid_hashes(values: dict[object, object]) -> bool:
        return all(isinstance(key, str) and isinstance(value, str) and len(value) == 64
                   and all(char in "0123456789abcdef" for char in value) for key, value in values.items())
    if not valid_hashes(local_hashes) or not valid_hashes(remote_hashes):
        raise ReconciliationImportError("reconciliation plan hashes must be lowercase sha256 values")
    try:
        conflicts = tuple(
            (entry["source_id"], entry["local_sha256"], entry["remote_sha256"])
            for entry in raw_conflicts if isinstance(entry, dict)
        )
        if len(conflicts) != len(raw_conflicts) or not all(
            isinstance(source_id, str) and isinstance(local_hash, str) and isinstance(remote_hash, str)
            and len(local_hash) == len(remote_hash) == 64
            for source_id, local_hash, remote_hash in conflicts
        ):
            raise TypeError
        plan = ReconciliationPlan(
            add_to_remote=tuple(raw_remote), add_to_local=tuple(raw_local), conflicts=conflicts,
            local_addition_hashes=local_hashes, remote_addition_hashes=remote_hashes,
            local_git_head=history.get("local"), remote_git_head=history.get("remote"),
        )
    except (KeyError, TypeError) as exc:
        raise ReconciliationImportError("reconciliation plan has invalid conflicts") from exc
    _validate_plan(plan)
    return plan


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
