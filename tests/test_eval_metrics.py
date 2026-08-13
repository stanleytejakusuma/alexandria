import json

import pytest

from alexandria.eval.metrics import (by_overlap_band, EvalResult, EvalSummary, mcnemar_exact, mrr,
                                     recall_at_k, reciprocal_rank, summarize, wilson_interval)


def test_recall_at_k_is_any_of_and_handles_boundaries_and_duplicates():
    assert recall_at_k([], ("wanted",), 5) is False
    assert recall_at_k(["other", "wanted"], ("wanted",), 10) is True
    assert recall_at_k(["wanted"], ("wanted",), 0) is False
    assert recall_at_k(["other", "wanted", "wanted"], ("wanted",), 2) is True
    assert recall_at_k(["wanted"], ("other", "wanted", "wanted"), 1) is True


def test_reciprocal_rank_uses_the_first_matching_result_and_zero_for_a_miss():
    assert reciprocal_rank([], ("wanted",)) == 0.0
    assert reciprocal_rank(["other", "wanted", "wanted"], ("wanted",)) == 0.5
    assert reciprocal_rank(["other"], ("wanted",)) == 0.0
    assert mrr([1.0, 0.5, 0.0]) == 0.5
    assert mrr([]) == 0.0


def test_summarize_keeps_target_errors_distinct_from_retrieval_misses_and_errors():
    summary = summarize([
        EvalResult("hit", "q", True, 2, ["other", "wanted"], 1.0),
        EvalResult("miss", "q", False, 0, ["other"], 1.0),
        EvalResult("target", "q", False, 0, [], 0.0, target_error=True),
        EvalResult("error", "q", False, 0, [], 0.0, error="RuntimeError: broken"),
    ])

    assert summary.n == 4
    assert summary.hits == 1
    assert summary.misses == ["miss", "error"]
    assert summary.target_errors == ["target"]
    assert summary.errors == 1
    assert summary.error_ids == ["error"]
    assert summary.recall_at_k == 1 / 3
    assert summary.mrr == 1 / 6


def test_eval_result_carries_overlap_band():
    r = EvalResult("id1", "q", True, 1, ["d1"], 10.0, overlap_band="zero")
    assert r.overlap_band == "zero"
    assert EvalResult.from_dict(r.to_dict()).overlap_band == "zero"


def test_by_overlap_band_groups_and_summarizes_each_band():
    results = [
        EvalResult("a", "q", True, 1, ["x"], 1.0, overlap_band="literal"),
        EvalResult("b", "q", True, 2, ["x"], 1.0, overlap_band="literal"),
        EvalResult("c", "q", False, 0, [], 1.0, overlap_band="zero"),
        EvalResult("d", "q", True, 1, ["x"], 1.0, overlap_band="zero"),
        EvalResult("e", "q", True, 1, ["x"], 1.0, overlap_band=None),
    ]
    bands = by_overlap_band(results)
    assert bands["literal"].recall_at_k == 1.0
    assert bands["zero"].recall_at_k == 0.5
    assert bands["literal"].n == 2 and bands["zero"].n == 2
    assert "None" not in bands and None not in bands   # untagged entries excluded, not miscounted


def test_by_overlap_band_empty_when_nothing_tagged():
    results = [EvalResult("a", "q", True, 1, ["x"], 1.0)]
    assert by_overlap_band(results) == {}


def test_wilson_interval_brackets_recall_and_never_leaves_the_unit_range():
    # The gate's own scale: 33/49 hits, the number a reader would otherwise read
    # as exact. Hand-computed Wilson bounds to 4dp.
    low, high = wilson_interval(33, 49)
    assert (round(low, 4), round(high, 4)) == (0.5338, 0.7879)
    assert low < 33 / 49 < high

    # Where the normal approximation breaks: p at the boundary. Wilson must stay
    # inside [0, 1] instead of reporting an impossible bound.
    assert wilson_interval(10, 10) == pytest.approx((0.7225, 1.0), abs=1e-4)
    assert wilson_interval(0, 10) == pytest.approx((0.0, 0.2775), abs=1e-4)

    # No sample is no information: report the whole range, not a precise zero.
    assert wilson_interval(0, 0) == (0.0, 1.0)

    # Same proportion, more evidence -> narrower interval.
    narrow = wilson_interval(500, 1000)
    wide = wilson_interval(5, 10)
    assert (narrow[1] - narrow[0]) < (wide[1] - wide[0])


def test_mcnemar_exact_only_counts_discordant_pairs_and_needs_real_evidence():
    # Nothing changed verdict: no evidence of a difference, not proof of none.
    assert mcnemar_exact(0, 0) == 1.0
    # A single flipped query is a coin toss; it must not read as significant.
    assert mcnemar_exact(1, 0) == 1.0
    assert mcnemar_exact(1, 1) == 1.0
    # 2 * P(X <= 0 | Binomial(5, 0.5)) = 2/32; the first count that clears 0.05
    # is six one-way flips.
    assert mcnemar_exact(5, 0) == pytest.approx(0.0625)
    assert mcnemar_exact(6, 0) == pytest.approx(0.03125)
    assert mcnemar_exact(6, 0) < 0.05 <= mcnemar_exact(5, 0)
    # 2 * (C(10,0) + C(10,1) + C(10,2)) / 2**10, and direction-independent.
    assert mcnemar_exact(8, 2) == pytest.approx(0.109375)
    assert mcnemar_exact(2, 8) == mcnemar_exact(8, 2)


def test_summary_carries_the_interval_and_survives_a_json_round_trip():
    summary = summarize([
        EvalResult("hit", "q", True, 1, ["wanted"], 1.0),
        EvalResult("miss", "q", False, 0, ["other"], 1.0),
        EvalResult("target", "q", False, 0, [], 0.0, target_error=True),
    ])
    assert summary.scored == 2
    assert summary.recall_ci == wilson_interval(1, 2)
    restored = EvalSummary.from_dict(json.loads(json.dumps(summary.to_dict())))
    assert restored == summary


def test_summary_predating_the_interval_is_recomputed_not_faked():
    # Runs already in eval_runs.jsonl carry no recall_ci key. Loading one must not
    # report a confident (0, 0) -- the counts are there, so derive the interval.
    legacy = {"recall_at_k": 0.5, "mrr": 0.5, "n": 3, "hits": 1,
              "misses": ["miss"], "target_errors": ["target"]}
    assert EvalSummary.from_dict(legacy).recall_ci == wilson_interval(1, 2)
