"""Atomic selection of a complete staged corpus generation."""
from __future__ import annotations

from pathlib import Path

import pytest

from alexandria.generation_pointer import (
    GenerationPointerError,
    activate_generation,
    resolve_generation,
)


def _generation(corpus: Path, name: str) -> Path:
    path = corpus / ".alexandria" / "generations" / name
    path.mkdir(parents=True)
    (path / "sentinel").write_text(name, encoding="utf-8")
    return path


def test_activation_switches_only_the_pointer(tmp_path: Path) -> None:
    old = _generation(tmp_path, "old")
    new = _generation(tmp_path, "new")

    activate_generation(tmp_path, old)
    assert resolve_generation(tmp_path) == old
    activate_generation(tmp_path, new)

    assert resolve_generation(tmp_path) == new
    assert (old / "sentinel").read_text() == "old"
    assert (new / "sentinel").read_text() == "new"


def test_failed_pointer_replace_keeps_the_previous_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import alexandria.generation_pointer as pointer

    old = _generation(tmp_path, "old")
    new = _generation(tmp_path, "new")
    activate_generation(tmp_path, old)

    def fail_replace(src: str | Path, dst: str | Path) -> None:
        raise OSError("simulated power loss before cutover")

    monkeypatch.setattr(pointer.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated power loss"):
        activate_generation(tmp_path, new)

    assert resolve_generation(tmp_path) == old
    assert list((tmp_path / ".alexandria").glob(".current-generation.*.tmp")) == []


def test_activation_refuses_a_generation_outside_the_staging_root(tmp_path: Path) -> None:
    outside = tmp_path / "not-a-generation"
    outside.mkdir()

    with pytest.raises(GenerationPointerError, match="staging root"):
        activate_generation(tmp_path, outside)
