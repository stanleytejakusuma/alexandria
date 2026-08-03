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
