"""Drain liveness is control-root state, not per-generation state.

2026-09-09 incident: /health reported the promotion drain dead -- "no completed
drain cycle for 8898s" -- while the drain thread was ticking normally. Nothing
was wrong with the drain.

`record_success` wrote `<corpus>/.alexandria/liveness.json`, where `corpus` is
the *resolved generation*. `stage_generation` never copied that file, so every
publish handed the new generation either no heartbeat or an inherited stale one,
and `check()` -- reading the same per-generation path -- reported a healthy
drain as dead. Worse, a serve that bound before a cutover kept recording into
the generation it started on, so the active generation's heartbeat froze
permanently.

Same failure class as the staging data loss: durable state written into a
disposable projection. The heartbeat describes the *process*, which outlives any
single generation, so it belongs in the control root.
"""
from __future__ import annotations

import json
from pathlib import Path

from alexandria import liveness
from alexandria.generation_pointer import activate_generation
from alexandria.generation_publisher import stage_generation


def _generation(control: Path, name: str) -> Path:
    generation = control / ".alexandria" / "generations" / name
    (generation / "sources").mkdir(parents=True, exist_ok=True)
    (generation / "sources" / "a.md").write_text("a")
    (generation / ".alexandria" / "state").mkdir(parents=True, exist_ok=True)
    return generation


def test_heartbeat_from_a_generation_lands_in_the_control_root(tmp_path: Path) -> None:
    """A drain bound to a generation records against the corpus that owns it."""
    control = tmp_path / "corpus"
    generation = _generation(control, "gen-a")
    activate_generation(control, generation)

    liveness.record_success(generation, promoted_count=0, generation=7)

    state = control / ".alexandria" / liveness.STATE_FILE
    assert state.exists(), "heartbeat must be written to the control root"
    assert not (generation / ".alexandria" / liveness.STATE_FILE).exists(), (
        "a generation is disposable; a heartbeat written there is lost at the "
        "next publish"
    )
    assert json.loads(state.read_text())["generation"] == 7


def test_check_reads_the_control_root_heartbeat_after_a_cutover(tmp_path: Path) -> None:
    """The regression that matters most: publish must not fake a dead drain.

    Records a heartbeat, publishes a new generation, then asks a *generation*
    path whether the drain is alive. Before the fix this reported stale, because
    the fresh generation carried no heartbeat.
    """
    control = tmp_path / "corpus"
    first = _generation(control, "gen-a")
    activate_generation(control, first)
    liveness.record_success(first, promoted_count=0, generation=7)

    second = stage_generation(control, "gen-b")
    activate_generation(control, second)

    assert liveness.check(second).stale is False
    assert liveness.check(control).stale is False
    age = liveness.heartbeat_age(second)
    assert age is not None and age < 60


def test_a_stale_heartbeat_inside_a_generation_is_ignored(tmp_path: Path) -> None:
    """A leftover per-generation file must not mask the real heartbeat.

    Generations published before this fix still carry old `liveness.json`
    copies. The control root is the single source of truth.
    """
    control = tmp_path / "corpus"
    generation = _generation(control, "gen-a")
    activate_generation(control, generation)

    stale = generation / ".alexandria" / liveness.STATE_FILE
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text(json.dumps({
        "last_success_at": "2020-01-01T00:00:00+0700",
        "promoted_count": 0,
        "generation": 1,
    }) + "\n")

    liveness.record_success(generation, promoted_count=0, generation=9)

    assert liveness.check(generation).stale is False
    assert json.loads(
        (control / ".alexandria" / liveness.STATE_FILE).read_text()
    )["generation"] == 9


def test_a_legacy_corpus_without_a_pointer_is_unchanged(tmp_path: Path) -> None:
    """Pre-migration corpora have no generations; behaviour must not shift."""
    corpus = tmp_path / "legacy"
    (corpus / ".alexandria").mkdir(parents=True)

    liveness.record_success(corpus, promoted_count=2, generation=3)

    assert (corpus / ".alexandria" / liveness.STATE_FILE).exists()
    assert liveness.check(corpus).stale is False
