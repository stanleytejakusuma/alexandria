from alexandria.eval.metrics import by_overlap_band, EvalResult, mrr, recall_at_k, reciprocal_rank, summarize


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
