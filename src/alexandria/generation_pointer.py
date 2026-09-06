"""Atomic pointer for a complete, staged corpus generation.

This primitive deliberately has no callers yet. It is the narrow cutover
mechanism required before the weekly loop can publish staged sources, state,
and index together.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

__all__ = ["GenerationPointerError", "activate_generation", "resolve_generation"]


class GenerationPointerError(RuntimeError):
    """A candidate is not a complete generation under this corpus staging root."""


def _root(corpus: str | Path) -> Path:
    return Path(corpus) / ".alexandria" / "generations"


def _pointer(corpus: str | Path) -> Path:
    return Path(corpus) / ".alexandria" / "current-generation.json"


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def activate_generation(corpus: str | Path, generation: str | Path) -> Path:
    """Durably select an existing same-root generation with one pointer swap."""
    corpus = Path(corpus)
    root = _root(corpus)
    candidate = Path(generation).resolve()
    root_resolved = root.resolve()
    if candidate.parent != root_resolved or not candidate.is_dir():
        raise GenerationPointerError(
            f"generation must be a direct child of staging root {root_resolved}"
        )

    pointer = _pointer(corpus)
    pointer.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        dir=pointer.parent, prefix=".current-generation.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"generation": candidate.name}, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, pointer)
        _fsync_dir(pointer.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return candidate


def resolve_generation(corpus: str | Path) -> Path:
    """Resolve the active generation and reject a malformed/escaping pointer."""
    corpus = Path(corpus)
    try:
        value = json.loads(_pointer(corpus).read_text(encoding="utf-8"))
        name = value["generation"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise GenerationPointerError("no valid active generation pointer") from exc
    if not isinstance(name, str) or Path(name).name != name:
        raise GenerationPointerError("generation pointer contains an invalid name")
    candidate = (_root(corpus) / name).resolve()
    if candidate.parent != _root(corpus).resolve() or not candidate.is_dir():
        raise GenerationPointerError("generation pointer does not name a staged generation")
    return candidate
