"""Bounded, timed loading for opaque third-party model constructors
(sentence-transformers, mlx-embeddings) that make their own unbounded network
calls with no native timeout parameter.

The failure this guards: CrossEncoder(model_name) / SentenceTransformer(model_name)
/ mlx_embeddings.load(model_name) issue several sequential HTTP requests
(config, tokenizer, weights), each individually bounded by huggingface_hub's
own ~10s per-request timeout, but the TOTAL across files and retries is not
bounded at all -- so a slow (not absent) network hangs a caller's first
`alexandria search`/`index` for minutes with zero indication anything is
wrong. HF_HUB_OFFLINE=1 already fails fast (~3s) when nothing is cached; this
module exists for the case that constant does not cover: present-but-slow.
"""

import threading
import time

import pytest

from alexandria.model_load import ModelLoadTimeout, load_with_timeout


def test_a_fast_load_returns_the_result():
    assert load_with_timeout(lambda: 42, timeout=5.0, description="thing") == 42


def test_a_load_that_raises_propagates_the_real_exception():
    def boom():
        raise ValueError("model not found")

    with pytest.raises(ValueError, match="model not found"):
        load_with_timeout(boom, timeout=5.0, description="thing")


def test_a_load_that_hangs_past_the_timeout_raises_a_named_error():
    """The load never returns (simulating a stalled network) -- the CALLER must
    get control back within the timeout, not wait for the hang to resolve."""
    def hangs():
        time.sleep(30)
        return "never reached in the test"

    started = time.monotonic()
    with pytest.raises(ModelLoadTimeout, match="thing"):
        load_with_timeout(hangs, timeout=0.2, description="thing")
    elapsed = time.monotonic() - started
    assert elapsed < 5.0, f"caller was blocked for {elapsed:.1f}s, not returned at the timeout"


def test_the_timeout_error_names_the_provider_model_and_likely_cause():
    def hangs():
        time.sleep(30)

    with pytest.raises(ModelLoadTimeout) as exc_info:
        load_with_timeout(hangs, timeout=0.1, description="local embedder model 'Qwen/Qwen3-Embedding-0.6B'")
    msg = str(exc_info.value)
    assert "Qwen/Qwen3-Embedding-0.6B" in msg
    assert "0.1" in msg or "timed out" in msg.lower()
    assert "network" in msg.lower() or "offline" in msg.lower() or "cach" in msg.lower()


def test_a_hung_load_does_not_leave_a_lingering_result_if_it_finishes_later():
    """The background thread eventually finishes (the network call returns after
    all) -- a second call must not somehow receive a stale/wrong result; each
    call to load_with_timeout is independent."""
    call_count = [0]

    def slow_but_finite():
        call_count[0] += 1
        time.sleep(0.05)
        return call_count[0]

    with pytest.raises(ModelLoadTimeout):
        load_with_timeout(slow_but_finite, timeout=0.01, description="x")
    # let the background thread actually finish before the next call
    time.sleep(0.2)
    result = load_with_timeout(slow_but_finite, timeout=5.0, description="x")
    assert result == 2  # the SECOND call's own result, not a leaked first one


def test_default_timeout_is_generous_but_finite():
    import inspect

    sig = inspect.signature(load_with_timeout)
    default = sig.parameters["timeout"].default
    assert default is not None
    assert 10.0 <= default <= 120.0, (
        f"default timeout {default}s should be generous (real models take "
        f"seconds) but finite (a hang must not masquerade as normal operation)")
