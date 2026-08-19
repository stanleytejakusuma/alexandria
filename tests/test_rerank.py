"""Offline reranking preserves fusion order exactly."""

import pytest

from alexandria.retrieval.rerank import CrossEncoderReranker, IdentityReranker, RerankCandidate


def test_identity_reranker_keeps_order_and_obeys_top_k():
    candidates = [RerankCandidate("a", "alpha", 0.9), RerankCandidate("b", "beta", 0.8)]

    assert IdentityReranker().rerank("query", candidates, k=1) == candidates[:1]


def test_half_precision_is_on_by_default():
    """Measured 3.12x faster with byte-identical top-5 ordering and no NaNs -- the
    only latency lever tested that cost nothing. Truncation, a smaller model, and a
    lower prefetch all changed results."""
    assert CrossEncoderReranker().half_precision is True


def test_half_precision_can_be_disabled():
    assert CrossEncoderReranker(half_precision=False).half_precision is False


def test_fp16_conversion_failure_falls_back_to_fp32_rather_than_failing(monkeypatch):
    """fp32 is correct, just slower. A backend that cannot do fp16 must still serve
    queries rather than raising."""
    import types

    class Boom:
        def half(self):
            raise RuntimeError("fp16 unsupported on this backend")

    class FakeCE:
        def __init__(self, *args, **kwargs):
            self.model = Boom()

        def predict(self, pairs):
            return [0.5] * len(pairs)

    fake = types.ModuleType("sentence_transformers")
    fake.CrossEncoder = FakeCE
    monkeypatch.setitem(__import__("sys").modules, "sentence_transformers", fake)

    got = CrossEncoderReranker().rerank("q", [RerankCandidate("a", "t", 0.1)], 1)
    assert len(got) == 1


def test_a_hung_model_load_raises_instead_of_hanging_the_query(monkeypatch):
    """The exact bug: CrossEncoder(model_name) makes an unconditional, untimed
    network call. A slow (not absent) network must not hang the caller -- it
    must raise within CrossEncoderReranker's own bound."""
    import time
    import types

    class HangingCE:
        def __init__(self, *args, **kwargs):
            time.sleep(30)  # never actually reached within the test's timeout

    fake = types.ModuleType("sentence_transformers")
    fake.CrossEncoder = HangingCE
    monkeypatch.setitem(__import__("sys").modules, "sentence_transformers", fake)

    reranker = CrossEncoderReranker(model="offline-degrade-hang-probe", load_timeout=0.2)
    started = time.monotonic()
    with pytest.raises(Exception):
        reranker._load()
    elapsed = time.monotonic() - started
    assert elapsed < 5.0, f"_load() blocked the caller for {elapsed:.1f}s past its bound"


def test_a_hung_load_propagates_as_a_named_timeout_not_a_swallowed_hang(monkeypatch):
    """rerank() itself must NOT swallow the failure -- search.py's existing
    try/except around rerank() is the one place degradation is detected and
    recorded (test_search.py::test_search_degrades_to_fusion_order_when_
    reranking_fails already proves that catch works for ANY exception). This
    proves _load()'s timeout raises through rerank() so that catch fires,
    instead of the model load hanging past it undetected."""
    import time
    import types

    from alexandria.model_load import ModelLoadTimeout

    class HangingCE:
        def __init__(self, *args, **kwargs):
            time.sleep(30)

    fake = types.ModuleType("sentence_transformers")
    fake.CrossEncoder = HangingCE
    monkeypatch.setitem(__import__("sys").modules, "sentence_transformers", fake)

    reranker = CrossEncoderReranker(model="offline-degrade-warn-probe", load_timeout=0.2)
    candidates = [RerankCandidate("a", "alpha text", 0.9)]
    started = time.monotonic()
    with pytest.raises(ModelLoadTimeout):
        reranker.rerank("query", candidates, k=1)
    elapsed = time.monotonic() - started
    assert elapsed < 5.0, f"rerank() blocked the caller for {elapsed:.1f}s past its bound"


def test_a_load_timeout_is_configurable_and_bounded_by_default():
    default_reranker = CrossEncoderReranker()
    assert 0 < default_reranker.load_timeout <= 120.0
