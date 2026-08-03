"""Offline reranking preserves fusion order exactly."""

from alexandria.retrieval.rerank import IdentityReranker, RerankCandidate


def test_identity_reranker_keeps_order_and_obeys_top_k():
    candidates = [RerankCandidate("a", "alpha", 0.9), RerankCandidate("b", "beta", 0.8)]

    assert IdentityReranker().rerank("query", candidates, k=1) == candidates[:1]
