import json

import pytest

from alexandria.eval.history import append_run, compare, load_runs, regressions
from alexandria.eval.metrics import EvalResult, summarize
from alexandria.eval.runner import EvalReport


def _report(results):
    return EvalReport(
        results=results,
        summary=summarize(results),
        config={"embedder": "hash-24"},
        corpus_chunks=1,
        timestamp="2026-08-04T00:00:00+00:00",
        git_sha="abc123",
    )


def test_history_appends_reports_and_comparison_exposes_per_entry_transitions(tmp_path):
    previous = _report([
        EvalResult("regressed", "q", True, 1, ["sources/a"], 1.0),
        EvalResult("improved", "q", False, 0, [], 1.0),
    ])
    current = _report([
        EvalResult("regressed", "q", False, 0, [], 1.0),
        EvalResult("improved", "q", True, 2, ["sources/x", "sources/b"], 1.0),
    ])
    path = tmp_path / "nested" / "eval_runs.jsonl"

    append_run(path, previous)
    append_run(path, current)

    assert load_runs(path) == [previous, current]
    delta = compare(previous, current)
    assert delta.recall_at_k == 0.0
    assert delta.mrr == -0.25
    assert delta.hit_to_miss == ["regressed"]
    assert delta.miss_to_hit == ["improved"]
    assert regressions(delta) == ["regressed"]


def test_comparison_refuses_to_call_a_one_query_flip_significant():
    # One query moved each way. The aggregate delta is a real number and reads as
    # a finding; the paired evidence is a coin toss. The gate must say so.
    previous = _report([
        EvalResult("regressed", "q", True, 1, ["sources/a"], 1.0),
        EvalResult("improved", "q", False, 0, [], 1.0),
        EvalResult("steady", "q", True, 1, ["sources/c"], 1.0),
    ])
    current = _report([
        EvalResult("regressed", "q", False, 0, [], 1.0),
        EvalResult("improved", "q", True, 1, ["sources/b"], 1.0),
        EvalResult("steady", "q", True, 1, ["sources/c"], 1.0),
    ])
    delta = compare(previous, current)
    assert delta.p_value == 1.0
    assert delta.significant is False


def test_a_one_sided_sweep_of_flips_is_significant_and_concordant_pairs_are_ignored():
    # Six queries all break one way, plus twenty that never move. The concordant
    # twenty must not dilute the test -- excluding them is what gives it power.
    broke = [f"broke{i}" for i in range(6)]
    steady = [f"steady{i}" for i in range(20)]
    previous = _report(
        [EvalResult(name, "q", True, 1, ["sources/a"], 1.0) for name in broke + steady])
    current = _report(
        [EvalResult(name, "q", False, 0, [], 1.0) for name in broke]
        + [EvalResult(name, "q", True, 1, ["sources/a"], 1.0) for name in steady])
    delta = compare(previous, current)
    assert delta.p_value == pytest.approx(0.03125)
    assert delta.significant is True
    assert sorted(delta.hit_to_miss) == broke


def test_delta_survives_a_json_round_trip_with_its_p_value():
    delta = compare(
        _report([EvalResult("a", "q", True, 1, ["sources/a"], 1.0)]),
        _report([EvalResult("a", "q", False, 0, [], 1.0)]),
    )
    assert json.loads(json.dumps(delta.to_dict()))["p_value"] == delta.p_value
