"""Golden sets are control-root state, not per-generation state.

Fourth instance of one bug class. The golden sets live in
`<control>/.alexandria/golden/`, `stage_generation` does not copy them, and
`cmd_eval` resolved them against `config.corpus_path` -- the generation. So
after the pointer migration the pre-commit eval gate could not read its own
golden set:

    UNUSABLE golden set: unable to read
    .../generations/recover-.../.alexandria/golden/golden-v1.jsonl

which the gate then reported as "a query that was HIT is now a MISS" -- a
retrieval regression that had not happened. A gate that cannot find its
evidence must not be able to masquerade as a quality signal.
"""
from __future__ import annotations

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


def test_golden_sets_resolve_against_the_control_root(tmp_path: Path) -> None:
    """The regression that matters most: the gate must find its golden set."""
    control, generation = _corpus_with_generation(tmp_path)
    golden = control / ".alexandria" / "golden" / "golden-v1.jsonl"
    golden.parent.mkdir(parents=True, exist_ok=True)
    golden.write_text('{"id": "q1", "query": "x", "doc_id": "d1"}\n')

    config = load_config(corpus_override=control)
    resolved = config.control_root / ".alexandria" / "golden" / "golden-v1.jsonl"

    assert resolved.exists(), "eval must read the golden set the operator maintains"
    assert not (
        generation / ".alexandria" / "golden" / "golden-v1.jsonl"
    ).exists(), "golden sets are never copied into a generation"


def test_a_legacy_corpus_resolves_golden_sets_unchanged(tmp_path: Path) -> None:
    corpus = tmp_path / "legacy"
    golden = corpus / ".alexandria" / "golden" / "golden-v1.jsonl"
    golden.parent.mkdir(parents=True, exist_ok=True)
    golden.write_text('{"id": "q1", "query": "x", "doc_id": "d1"}\n')

    config = load_config(corpus_override=corpus)

    assert (config.control_root / ".alexandria" / "golden" / "golden-v1.jsonl").exists()
