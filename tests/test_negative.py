"""Negative retrieval cases and score separation (BACKLOG #21, spec Q5)."""

from __future__ import annotations

import json

import pytest

from alexandria.eval.history import compare, regressions
from alexandria.eval.metrics import EvalResult, summarize
from alexandria.eval.negative import (
    NegativeEntry,
    load_negative,
    run_negative,
    separation,
)


def _write(tmp_path, rows):
    path = tmp_path / "negative.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    return path


def _result(entry_id, *, hit, scores):
    return EvalResult(
        id=entry_id, query=f"q-{entry_id}", hit=hit, rank=1 if hit else 0,
        retrieved_ids=["doc"] * len(scores), latency_ms=1.0, scores=tuple(scores),
    )


# --- loading ---------------------------------------------------------------

def test_a_valid_negative_entry_loads(tmp_path):
    path = _write(tmp_path, [{
        "id": "kafka", "query": "Kafka consumer lag",
        "note": "no Kafka anywhere in the corpus; top hits were unrelated",
        "verified_against": 46000,
    }])
    entries = load_negative(path)
    assert entries == [NegativeEntry("kafka", "Kafka consumer lag",
                                     "no Kafka anywhere in the corpus; top hits were unrelated",
                                     46000)]


def test_verified_against_is_optional(tmp_path):
    path = _write(tmp_path, [{"id": "a", "query": "q", "note": "checked"}])
    assert load_negative(path)[0].verified_against is None


def test_a_negative_entry_without_a_note_is_rejected(tmp_path):
    """A claim about absence across a whole corpus must record how it was established."""
    path = _write(tmp_path, [{"id": "a", "query": "q"}])
    with pytest.raises(ValueError, match="missing field"):
        load_negative(path)


def test_an_empty_note_is_rejected_not_treated_as_present(tmp_path):
    path = _write(tmp_path, [{"id": "a", "query": "q", "note": ""}])
    with pytest.raises(ValueError, match="how absence was established"):
        load_negative(path)


def test_an_unknown_field_is_rejected_with_its_line_number(tmp_path):
    path = _write(tmp_path, [
        {"id": "a", "query": "q", "note": "n"},
        {"id": "b", "query": "q", "note": "n", "must_retrieve": ["x"]},
    ])
    with pytest.raises(ValueError, match="negative line 2: unknown field"):
        load_negative(path)


def test_a_negative_set_cannot_smuggle_a_positive_assertion(tmp_path):
    """must_retrieve is precisely what a negative case must not carry."""
    path = _write(tmp_path, [{"id": "a", "query": "q", "note": "n", "must_retrieve": []}])
    with pytest.raises(ValueError, match="must_retrieve"):
        load_negative(path)


@pytest.mark.parametrize("bad", [-1, "many", 1.5, True])
def test_verified_against_must_be_a_non_negative_integer(tmp_path, bad):
    path = _write(tmp_path, [{"id": "a", "query": "q", "note": "n", "verified_against": bad}])
    with pytest.raises(ValueError, match="verified_against"):
        load_negative(path)


# --- running ---------------------------------------------------------------

class _FakeEngine:
    def __init__(self, results):
        self._results = results

    def search(self, query, k=5):
        if isinstance(self._results, Exception):
            raise self._results
        return self._results[:k]


class _Hit:
    def __init__(self, doc_id, score):
        self.doc_id, self.score, self.text = doc_id, score, "body"


def test_run_negative_records_scores_and_never_claims_a_hit():
    engine = _FakeEngine([_Hit("a", 0.4), _Hit("b", 0.3)])
    rows = run_negative(engine, [NegativeEntry("n1", "q", "note")], k=5)
    assert len(rows) == 1
    assert rows[0].scores == (0.4, 0.3)
    assert rows[0].retrieved_ids == ["a", "b"]
    assert rows[0].hit is False


def test_run_negative_respects_k():
    engine = _FakeEngine([_Hit(str(i), 1.0 - i / 10) for i in range(10)])
    rows = run_negative(engine, [NegativeEntry("n1", "q", "note")], k=3)
    assert len(rows[0].scores) == 3


def test_a_failing_negative_query_is_preserved_as_a_row_not_dropped():
    """Dropping failures would inflate separation by removing unusual queries."""
    engine = _FakeEngine(RuntimeError("boom"))
    rows = run_negative(engine, [NegativeEntry("n1", "q", "note")], k=5)
    assert len(rows) == 1
    assert rows[0].error == "RuntimeError: boom"
    assert rows[0].scores == ()


# --- separation ------------------------------------------------------------

def test_cleanly_separated_distributions_yield_full_clean_floor_recall():
    positives = [_result(f"p{i}", hit=True, scores=[0.9]) for i in range(5)]
    negatives = [_result(f"n{i}", hit=False, scores=[0.2]) for i in range(5)]
    report = separation(positives, negatives)
    assert report.clean_floor == 0.2
    assert report.clean_floor_recall == 1.0
    assert report.separable is True


def test_fully_overlapping_distributions_yield_zero_clean_floor_recall():
    """The Q5 'no floor is specifiable' outcome must be representable, not an error."""
    positives = [_result(f"p{i}", hit=True, scores=[0.5]) for i in range(5)]
    negatives = [_result("n0", hit=False, scores=[0.99])]
    report = separation(positives, negatives)
    assert report.clean_floor == 0.99
    assert report.clean_floor_recall == 0.0
    assert report.separable is False


def test_one_confident_negative_collapses_the_floor():
    """The floor is set by the WORST case, so a single bad negative dominates.

    This is the intended behaviour -- a threshold that admits one known-garbage
    result is not a clean floor -- and it is why the report also carries the
    negative median, so a lone outlier is visible rather than merely fatal.
    """
    positives = [_result(f"p{i}", hit=True, scores=[0.9]) for i in range(9)]
    negatives = [_result(f"n{i}", hit=False, scores=[0.1]) for i in range(9)]
    negatives.append(_result("outlier", hit=False, scores=[0.95]))
    report = separation(positives, negatives)
    assert report.clean_floor_recall == 0.0
    # The outlier is visible rather than merely fatal: median stays low, max exposes it.
    assert report.negative_top1_median == 0.1
    assert report.negative_top1_max == 0.95


def test_missed_positives_are_excluded_from_the_distribution():
    """A golden entry the engine MISSED says nothing about scoring a correct answer.

    Mutation check: including misses here would let a low-scoring wrong result
    drag the positive distribution down and understate separation.
    """
    positives = [
        _result("hit", hit=True, scores=[0.9]),
        _result("miss", hit=False, scores=[0.05]),
    ]
    negatives = [_result("n0", hit=False, scores=[0.2])]
    report = separation(positives, negatives)
    assert report.n_positive == 1
    assert report.positive_top1_min == 0.9
    assert report.clean_floor_recall == 1.0


def test_a_positive_contributes_the_score_of_its_hit_not_of_the_top_result():
    """`hit` only means the target was somewhere in top-k -- possibly not first.

    Scores descend, so reading scores[0] for a hit at rank 3 measures a document
    that was WRONG, inflating the positive distribution and overstating
    separation. Found by review after the first real run published a floor
    justified by a positive minimum of 0.1190; corrected, that minimum was
    0.0274 and the claim it supported ("retains 100%") was false.

    Mutation check: reverting to scores[0] makes positive_top1_min 0.95 and
    clean_floor_recall 1.0, and this test fails on both.
    """
    hit_at_rank_three = EvalResult(
        id="p0", query="q", hit=True, rank=3,
        retrieved_ids=["wrong-a", "wrong-b", "target"],
        latency_ms=1.0, scores=(0.95, 0.80, 0.30),
    )
    negatives = [_result("n0", hit=False, scores=[0.50])]
    report = separation([hit_at_rank_three], negatives)
    assert report.positive_top1_min == 0.30
    # The negative outscores the real hit, so no threshold separates them.
    assert report.clean_floor_recall == 0.0
    assert report.separable is False


def test_a_hit_whose_rank_falls_outside_its_scores_is_skipped_not_crashed():
    """Defensive: a fake engine may report a rank without a matching score list.

    Skipping is right -- fabricating a score would corrupt the distribution the
    floor is derived from -- but it must not take the whole run down.
    """
    inconsistent = EvalResult(
        id="p0", query="q", hit=True, rank=9,
        retrieved_ids=["target"], latency_ms=1.0, scores=(0.9,),
    )
    good = _result("p1", hit=True, scores=[0.8])
    report = separation([inconsistent, good], [_result("n0", hit=False, scores=[0.1])])
    assert report.n_positive == 1
    assert report.positive_top1_min == 0.8


def test_rows_with_no_results_are_skipped_rather_than_scored_as_zero():
    positives = [_result("p0", hit=True, scores=[0.9])]
    negatives = [_result("n0", hit=False, scores=[0.2]), _result("empty", hit=False, scores=[])]
    report = separation(positives, negatives)
    assert report.n_negative == 1


def test_separation_refuses_to_report_on_an_empty_side():
    """Silently returning 1.0 here would be the R4 failure: a gate green by construction."""
    with pytest.raises(ValueError, match="at least one scored positive"):
        separation([], [_result("n0", hit=False, scores=[0.2])])
    with pytest.raises(ValueError, match="at least one scored positive"):
        separation([_result("p0", hit=True, scores=[0.9])], [])


def test_median_is_correct_for_an_even_count():
    positives = [_result("p0", hit=True, scores=[0.9])]
    negatives = [_result(f"n{i}", hit=False, scores=[s]) for i, s in enumerate([0.1, 0.2, 0.3, 0.4])]
    assert separation(positives, negatives).negative_top1_median == pytest.approx(0.25)


# --- regression gate -------------------------------------------------------

def _report(negatives, positives=(), separation_dict=None):
    from alexandria.eval.runner import EvalReport
    return EvalReport(
        results=list(positives), summary=summarize(list(positives)), config={},
        corpus_chunks=None, timestamp="t", git_sha="sha",
        negatives=list(negatives), separation=separation_dict,
    )


def test_a_negative_growing_more_confident_is_a_named_regression():
    """The precision counterpart to hit_to_miss, at the same granularity."""
    before = _report([_result("stripe", hit=False, scores=[0.20])])
    after = _report([_result("stripe", hit=False, scores=[0.85])])
    delta = compare(before, after)
    assert delta.negative_confidence_rose == ["stripe"]
    assert regressions(delta) == ["negative:stripe"]


def test_a_small_rise_is_noise_not_a_regression():
    before = _report([_result("stripe", hit=False, scores=[0.20])])
    after = _report([_result("stripe", hit=False, scores=[0.25])])
    assert compare(before, after).negative_confidence_rose == []
    assert regressions(compare(before, after)) == []


def test_a_negative_becoming_less_confident_is_never_a_regression():
    before = _report([_result("stripe", hit=False, scores=[0.85])])
    after = _report([_result("stripe", hit=False, scores=[0.05])])
    assert compare(before, after).negative_confidence_rose == []


def test_a_newly_added_negative_cannot_trip_the_gate_on_its_first_run():
    """Without this, adding a case to the set would fail the very commit that adds it."""
    before = _report([])
    after = _report([_result("brand-new", hit=False, scores=[0.99])])
    assert compare(before, after).negative_confidence_rose == []


def test_recall_and_precision_regressions_are_reported_together_and_distinguishably():
    before = _report(
        [_result("stripe", hit=False, scores=[0.10])],
        positives=[_result("q1", hit=True, scores=[0.9])],
    )
    after = _report(
        [_result("stripe", hit=False, scores=[0.90])],
        positives=[_result("q1", hit=False, scores=[0.1])],
    )
    assert regressions(compare(before, after)) == ["q1", "negative:stripe"]


def test_clean_floor_recall_delta_is_carried_for_visibility():
    before = _report([], separation_dict={"clean_floor_recall": 0.9})
    after = _report([], separation_dict={"clean_floor_recall": 0.6})
    assert compare(before, after).clean_floor_recall == pytest.approx(-0.3)


def test_history_written_before_negatives_existed_still_loads():
    """The ~1MB of existing eval history must not become unreadable."""
    from alexandria.eval.runner import EvalReport
    legacy = {
        "results": [], "summary": {"recall_at_k": 0.5, "mrr": 0.4, "n": 1, "hits": 1,
                                   "misses": [], "target_errors": [], "errors": 0,
                                   "error_ids": []},
        "config": {}, "corpus_chunks": 10, "timestamp": "t", "git_sha": "sha",
    }
    report = EvalReport.from_dict(legacy)
    assert report.negatives == []
    assert report.separation is None
