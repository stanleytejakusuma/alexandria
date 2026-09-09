"""Staging must not drop documents that live on only one side.

2026-09-09 incident: four standing-convention documents written to the control
root were never picked up by any generation, because `stage_generation` copies
from the *active generation* only. They sat on disk, absent from every index,
unreachable by search.

The naive fix -- copy from the control root instead -- is worse: 897 documents
(the reconciled NAS content) exist only in the active generation and would be
dropped on the next publish.

So staging takes the UNION: generation content first (it is what readers
currently see, and reconciliation writes land there), then control-root content
layered on top for anything the generation lacks. Raw `inbox/` and pending
markers travel too, so an un-promoted entry is never stranded by a cutover.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from alexandria.generation_pointer import activate_generation
from alexandria.generation_publisher import stage_generation


def _seed_generation(control: Path, generation_id: str, docs: dict[str, str]) -> Path:
    generation = control / ".alexandria" / "generations" / generation_id
    for relative, text in docs.items():
        path = generation / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    (generation / ".alexandria" / "state").mkdir(parents=True, exist_ok=True)
    return generation


def _control_doc(control: Path, relative: str, text: str) -> None:
    path = control / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_staging_keeps_generation_only_and_control_root_only_documents(tmp_path: Path) -> None:
    """The union case -- this is the regression that matters most.

    `generation-only.md` stands for the 897 reconciled documents.
    `control-only.md` stands for the four lost standing conventions.
    """
    control = tmp_path / "corpus"
    control.mkdir()
    _seed_generation(
        control,
        "gen-a",
        {
            "sources/generation-only.md": "reconciled from the NAS",
            "sources/shared.md": "generation copy",
        },
    )
    activate_generation(control, control / ".alexandria" / "generations" / "gen-a")

    _control_doc(control, "sources/control-only.md", "standing convention")
    _control_doc(control, "sources/shared.md", "control copy")

    staged = stage_generation(control, "gen-b")

    assert (staged / "sources" / "generation-only.md").read_text() == "reconciled from the NAS"
    assert (staged / "sources" / "control-only.md").read_text() == "standing convention"
    # The generation is what readers currently see, so it wins a collision.
    assert (staged / "sources" / "shared.md").read_text() == "generation copy"


def test_staging_carries_raw_inbox_and_pending_markers(tmp_path: Path) -> None:
    """An un-promoted entry must survive a cutover.

    `inbox/` and `.alexandria/pending/` were never in the copy list, so a
    /remember that had not yet been promoted was stranded in the abandoned
    generation.
    """
    control = tmp_path / "corpus"
    control.mkdir()
    _seed_generation(control, "gen-a", {"sources/keep.md": "x"})
    activate_generation(control, control / ".alexandria" / "generations" / "gen-a")

    _control_doc(control, "inbox/2026-09-09.md", "\u00a7\nan unpromoted entry\n")
    pending = control / ".alexandria" / "pending" / "abc123"
    pending.parent.mkdir(parents=True, exist_ok=True)
    pending.write_text("marker")

    staged = stage_generation(control, "gen-b")

    assert (staged / "inbox" / "2026-09-09.md").exists()
    assert (staged / ".alexandria" / "pending" / "abc123").exists()


def test_legacy_corpus_without_a_pointer_still_stages_from_the_root(tmp_path: Path) -> None:
    """Pre-migration corpora have no pointer; the root is the only source."""
    control = tmp_path / "corpus"
    _control_doc(control, "sources/legacy.md", "legacy content")

    staged = stage_generation(control, "gen-a")

    assert (staged / "sources" / "legacy.md").read_text() == "legacy content"


def test_staging_still_refuses_a_duplicate_generation(tmp_path: Path) -> None:
    control = tmp_path / "corpus"
    _control_doc(control, "sources/a.md", "a")
    stage_generation(control, "gen-a")
    with pytest.raises(Exception, match="already exists"):
        stage_generation(control, "gen-a")
