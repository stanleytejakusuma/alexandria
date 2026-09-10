"""Reclaim disk from old corpus generations, without lying about the cost.

`stage_generation` clones the index and cache with APFS copy-on-write (`cp
-c`), so `du`-style apparent size wildly overstates what deleting a generation
actually frees: most blocks are shared with generations that stay. This module
answers "what would actually come back" before anything is removed, and never
deletes without an explicit `apply=True`.

Retention policy (operator-confirmed): keep the N newest *published*
generations (ones with a control-root history commit -- see
`generation_history.py`) plus the M newest *unpublished* candidates (staged
but never made live -- crash, timeout, or failed validation). The active
generation is never eligible, regardless of N/M.
"""
from __future__ import annotations

import fcntl
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .generation_pointer import GenerationPointerError, resolve_generation

__all__ = [
    "GenerationGcError",
    "GcCandidate",
    "GcPlan",
    "plan_generation_gc",
    "run_generation_gc",
]

_F_LOG2PHYS_EXT = 65  # macOS fcntl.h: ask the filesystem which physical
                       # extent backs a logical file offset. Two files whose
                       # extents match are APFS clones sharing blocks.


class GenerationGcError(RuntimeError):
    """A generation GC plan could not be safely applied."""


@dataclass(frozen=True)
class GcCandidate:
    generation_id: str
    published: bool
    apparent_bytes: int
    unique_bytes: int


@dataclass(frozen=True)
class GcPlan:
    active_generation_id: str | None
    keep: list[GcCandidate] = field(default_factory=list)
    reclaim: list[GcCandidate] = field(default_factory=list)

    @property
    def reclaimable_bytes(self) -> int:
        return sum(c.unique_bytes for c in self.reclaim)


def _generations_root(control_root: Path) -> Path:
    return control_root / ".alexandria" / "generations"


def _history_root(control_root: Path) -> Path:
    return control_root / "history" / "generations"


def _iter_files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file() and not p.is_symlink()]


def _physical_extent(path: Path) -> tuple[int, int] | None:
    """Best-effort clone-sharing fingerprint for a file's first block.

    Returns None (treat as unique / not shareable) on any platform or I/O
    failure -- failing to detect a clone only makes GC conservative, it never
    makes it over-delete, which is the direction that matters here.
    """
    try:
        import fcntl as _fcntl
        import struct

        fd = os.open(path, os.O_RDONLY)
        try:
            buf = struct.pack("=IIqq", 0, 0, 1 << 20, 0)
            raw = _fcntl.fcntl(fd, _F_LOG2PHYS_EXT, buf)
        finally:
            os.close(fd)
        _flags, _pad, contig, devoffset = struct.unpack("=IIqq", raw)
        return (contig, devoffset)
    except (OSError, AttributeError, struct.error):  # not APFS / not macOS / empty file
        return None


def _size_key(path: Path) -> int:
    return path.stat().st_size


def _classify_generations(control_root: Path) -> tuple[list[str], list[str]]:
    """Return (published_ids, unpublished_ids), both oldest-first by mtime."""
    gen_root = _generations_root(control_root)
    if not gen_root.is_dir():
        return [], []
    history_ids = set()
    if _history_root(control_root).is_dir():
        history_ids = {p.name for p in _history_root(control_root).iterdir() if p.is_dir()}
    entries = sorted(
        (p for p in gen_root.iterdir() if p.is_dir()),
        key=lambda p: p.stat().st_mtime,
    )
    published = [p.name for p in entries if p.name in history_ids]
    unpublished = [p.name for p in entries if p.name not in history_ids]
    return published, unpublished


def _unique_bytes_for(target_id: str, generations: dict[str, Path]) -> int:
    """Bytes in `target_id` that are not clone-shared with any other kept-or-not generation.

    Compares against every *other* generation currently on disk (not just
    ones being kept) so the answer is correct regardless of GC order: if two
    reclaimed generations share a clone with each other but not with anything
    kept, that block is still real disk that comes back.
    """
    # Clones are matched by physical extent, not by filename: staging clones
    # a file into a differently-named/relocated path (e.g. into a per-
    # generation index tree), so a name/mtime key would miss real sharing.
    # Extent lookup is only cheap to bucket by size first.
    target_root = generations[target_id]
    others = [(gid, root) for gid, root in generations.items() if gid != target_id]

    other_by_size: dict[int, list[Path]] = {}
    for _gid, root in others:
        for f in _iter_files(root):
            other_by_size.setdefault(_size_key(f), []).append(f)

    total = 0
    for f in _iter_files(target_root):
        size = f.stat().st_size
        candidates = other_by_size.get(size, [])
        shared = False
        if candidates:
            mine = _physical_extent(f)
            if mine is not None:
                for other in candidates:
                    if _physical_extent(other) == mine:
                        shared = True
                        break
            else:
                # Same size exists elsewhere and clone-detection is
                # unavailable on this filesystem: assume shared rather than
                # silently reporting a bigger number than would really free.
                shared = True
        if not shared:
            total += size
    return total


def plan_generation_gc(
    control_root: str | Path,
    *,
    keep_published: int = 2,
    keep_unpublished: int = 1,
) -> GcPlan:
    """Report what GC would reclaim. Read-only; never touches the filesystem."""
    control_root = Path(control_root)
    gen_root = _generations_root(control_root)

    try:
        active = resolve_generation(control_root).name
    except GenerationPointerError:
        active = None

    # A bare `list[-0:]` slice means "whole list", not "last zero" (-0 == 0),
    # so keep_published/keep_unpublished == 0 must be handled explicitly.
    published, unpublished = _classify_generations(control_root)
    keep_ids = set(published[len(published) - keep_published:] if keep_published > 0 else [])
    keep_ids |= set(unpublished[len(unpublished) - keep_unpublished:] if keep_unpublished > 0 else [])
    if active is not None:
        keep_ids.add(active)

    all_ids = published + unpublished
    all_paths = {gid: gen_root / gid for gid in all_ids}

    keep: list[GcCandidate] = []
    reclaim: list[GcCandidate] = []
    for gid in all_ids:
        root = all_paths[gid]
        apparent = sum(f.stat().st_size for f in _iter_files(root))
        is_kept = gid in keep_ids
        # Unique-byte accounting is only meaningful (and only worth the I/O)
        # for candidates actually being reclaimed.
        unique = _unique_bytes_for(gid, all_paths) if not is_kept else apparent
        candidate = GcCandidate(
            generation_id=gid,
            published=gid in published,
            apparent_bytes=apparent,
            unique_bytes=unique,
        )
        (keep if is_kept else reclaim).append(candidate)

    return GcPlan(active_generation_id=active, keep=keep, reclaim=reclaim)


def run_generation_gc(
    control_root: str | Path,
    *,
    keep_published: int = 2,
    keep_unpublished: int = 1,
    apply: bool = False,
    _plan: GcPlan | None = None,
) -> GcPlan:
    """Plan generation GC, and only delete if `apply=True`.

    Re-resolves the live pointer immediately before every delete under the
    same publish lock the publisher uses, so a plan computed earlier can
    never be used to delete a generation that became active in the meantime.
    """
    control_root = Path(control_root)
    plan = _plan if _plan is not None else plan_generation_gc(
        control_root, keep_published=keep_published, keep_unpublished=keep_unpublished
    )
    if not apply:
        return plan

    lock_path = control_root / ".alexandria" / "generation-publish.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            try:
                live_active = resolve_generation(control_root).name
            except GenerationPointerError as exc:
                raise GenerationGcError(
                    "refusing to delete: the active generation pointer is unreadable, "
                    "so GC cannot prove it will not delete the live generation"
                ) from exc
            gen_root = _generations_root(control_root)
            for candidate in plan.reclaim:
                if candidate.generation_id == live_active:
                    continue  # became active after planning; do not delete
                target = gen_root / candidate.generation_id
                if target.is_dir():
                    shutil.rmtree(target)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return plan
