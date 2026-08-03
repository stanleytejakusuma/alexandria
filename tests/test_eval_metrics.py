from alexandria.eval.metrics import EvalResult, mrr, recall_at_k, reciprocal_rank, summarize


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
