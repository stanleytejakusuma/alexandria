"""Durable writes belong to the control root; generations are projections.

Three separate incidents in two days shared one root cause: `load_config`
resolved the generation pointer for *every* verb, so code that needed the
durable corpus silently received a disposable generation directory.

  - 2026-09-09 (a4b4131): `remember` appended to `<generation>/inbox/`, which
    staging did not copy. Un-promoted entries vanished at the next publish.
  - 2026-09-09 (a7f8290): the drain wrote `<generation>/.alexandria/liveness.json`,
    so /health reported a healthy drain dead after every publish.
  - open: `serve --add-token` writes a token into a generation, where the next
    publish drops it.

Each was fixed at its own call site. This pins the invariant itself: an
`AppConfig` exposes BOTH roots, so a verb states which one it means instead of
inheriting whatever `load_config` happened to resolve.
"""
from __future__ import annotations

import json
from pathlib import Path

from alexandria.config import load_config
from alexandria.generation_pointer import activate_generation


def _corpus_with_generation(tmp_path: Path) -> tuple[Path, Path]:
    control = tmp_path / "corpus"
    generation = control / ".alexandria" / "generations" / "gen-a"
    (generation / "sources").mkdir(parents=True)
    (generation / ".alexandria" / "state").mkdir(parents=True)
    activate_generation(control, generation)
    return control, generation


def test_config_exposes_the_control_root_alongside_the_resolved_generation(
    tmp_path: Path,
) -> None:
    """Both roots must be reachable; today only the resolved one survives."""
    control, generation = _corpus_with_generation(tmp_path)

    config = load_config(corpus_override=control)

    assert config.corpus_path == generation, "reads still resolve to the generation"
    assert config.control_root == control, (
        "the durable root must survive resolution, or every write verb has to "
        "reconstruct it by hand -- the bug behind a4b4131 and a7f8290"
    )


def test_control_root_is_the_corpus_itself_without_a_pointer(tmp_path: Path) -> None:
    """Legacy corpora have no generations; both roots are the same path."""
    corpus = tmp_path / "legacy"
    (corpus / "sources").mkdir(parents=True)

    config = load_config(corpus_override=corpus)

    assert config.corpus_path == corpus
    assert config.control_root == corpus


def test_control_root_survives_a_cutover(tmp_path: Path) -> None:
    """After a publish the control root is unchanged; only the read path moves."""
    control, first = _corpus_with_generation(tmp_path)
    second = control / ".alexandria" / "generations" / "gen-b"
    (second / "sources").mkdir(parents=True)
    (second / ".alexandria" / "state").mkdir(parents=True)
    activate_generation(control, second)

    config = load_config(corpus_override=control)

    assert config.corpus_path == second
    assert config.control_root == control


def test_passing_a_generation_directly_still_reports_a_usable_control_root(
    tmp_path: Path,
) -> None:
    """Scripts pass generation paths (the stage runner does).

    A generation has no pointer of its own, so it resolves to itself -- but its
    control root is the corpus that owns it, not the generation. Without this,
    `backup` and the drain re-acquire the wrong root the moment a caller passes
    a generation path.
    """
    control, generation = _corpus_with_generation(tmp_path)

    config = load_config(corpus_override=generation)

    assert config.corpus_path == generation
    assert config.control_root == control


def test_remember_appends_to_the_control_root_inbox(tmp_path: Path) -> None:
    """The a4b4131 incident, pinned at the config layer.

    `stage_generation` now copies `inbox/`, so a lost entry is no longer
    silent -- but the entry still belongs in the durable root, not in whichever
    generation happened to be active when it arrived.
    """
    control, generation = _corpus_with_generation(tmp_path)
    config = load_config(corpus_override=control)

    inbox = config.control_root / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "2026-09-09.md").write_text("an entry")

    assert (control / "inbox" / "2026-09-09.md").exists()
    assert not (generation / "inbox" / "2026-09-09.md").exists()


def test_serve_token_path_resolves_against_the_control_root(tmp_path: Path) -> None:
    """A token minted into a generation is dropped by the next publish."""
    control, generation = _corpus_with_generation(tmp_path)
    config = load_config(corpus_override=control)

    token_file = config.control_root / ".alexandria" / "serve-tokens.txt"
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text("hash\n")

    assert token_file.is_relative_to(control)
    assert not token_file.is_relative_to(generation)
    assert json.loads(
        (control / ".alexandria" / "current-generation.json").read_text()
    )["generation"].endswith("gen-a")
