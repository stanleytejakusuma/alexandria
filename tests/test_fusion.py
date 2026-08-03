"""Fusion is deterministic and layer boosting cannot turn into hard routing."""

from alexandria.retrieval.fusion import apply_layer_boost, rrf


def test_rrf_accumulates_reciprocal_ranks_deterministically():
    scores = rrf([["a", "b"], ["b", "c", "a"]], k=60)

    assert scores["b"] > scores["a"] > scores["c"]
    assert scores["a"] == 1 / 61 + 1 / 63


def test_layer_boost_is_a_pure_score_multiplier_without_dropping_sources():
    boosted = apply_layer_boost({"source": 1.0, "wiki": 0.9},
                                {"source": "sources", "wiki": "wiki"}, wiki_boost=1.25)

    assert boosted == {"source": 1.0, "wiki": 1.125}
    assert set(boosted) == {"source", "wiki"}
