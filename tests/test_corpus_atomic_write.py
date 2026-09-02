"""Crash-safe corpus document writes."""
from __future__ import annotations

from pathlib import Path

import pytest

from alexandria.corpus import Doc


def test_doc_write_keeps_the_previous_file_if_atomic_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A killed/failed sync must never leave torn Markdown in the corpus."""
    path = tmp_path / "sources" / "test.md"
    path.parent.mkdir()
    original = "---\ntitle: original\n---\nold body\n"
    path.write_text(original, encoding="utf-8")

    def fail_replace(src: str | Path, dst: str | Path) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr("alexandria.corpus.os.replace", fail_replace)
    with pytest.raises(OSError, match="simulated replace failure"):
        Doc("sources/test.md", {"title": "replacement"}, "new body\n").write(tmp_path)

    assert path.read_text(encoding="utf-8") == original
    assert list(path.parent.glob(".test.md.*.tmp")) == []
