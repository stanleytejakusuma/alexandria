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
    # Red review, 2026-08-20: the message must guide a user with a CACHED
    # model toward offline mode -- for them the truth is the opposite of
    # "nothing is cached"; HF_HUB_OFFLINE=1 would SUCCEED instantly from the
    # local copy instead of re-checking the network for updates.
    assert "HF_HUB_OFFLINE" in msg
    assert "loaded successfully before" in msg


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


# ---------------------------------------------------------------------------
# Shared, keyed failure cooldown (Red review, 2026-08-20). This closes a real
# gap the first fix left open: the reranker got a module-level failure cache
# (a cooldown, correct for a long-lived process); the embedders got PER-
# INSTANCE memoization that assumed "a fresh instance is built per top-level
# operation." That assumption does NOT hold for serve: build_serve_context's
# own docstring says "Build once at startup, reused by every request," and
# _warm_embedder calls _load() proactively at boot -- so a startup network
# blip would have made self._load_error PERMANENT for the rest of the
# process's life, the opposite of the intended asymmetry (the component
# designed to never silently degrade would have been the LEAST resilient to
# a transient failure). Moving the cooldown into load_with_timeout itself, as
# a shared keyed facility, means every caller gets correct behavior by
# default instead of by an unenforced construction-shape assumption.
# ---------------------------------------------------------------------------

def test_a_keyed_failure_is_not_retried_within_the_cooldown():
    load_calls = []

    def hangs():
        load_calls.append(time.monotonic())
        time.sleep(30)

    started = time.monotonic()
    for _ in range(5):
        with pytest.raises(ModelLoadTimeout):
            load_with_timeout(hangs, timeout=0.05, description="x", key="probe-a", cooldown=5.0)
    elapsed = time.monotonic() - started

    assert elapsed < 0.5, f"5 calls within the cooldown took {elapsed:.2f}s"
    assert len(load_calls) == 1, (
        f"the network was attempted {len(load_calls)} times for 5 calls "
        f"within the cooldown -- only the first should ever attempt it")


def test_a_keyed_failure_retries_after_the_cooldown_expires():
    load_calls = []

    def hangs():
        load_calls.append(time.monotonic())
        time.sleep(30)

    with pytest.raises(ModelLoadTimeout):
        load_with_timeout(hangs, timeout=0.02, description="x", key="probe-b", cooldown=0.1)
    time.sleep(0.15)
    with pytest.raises(ModelLoadTimeout):
        load_with_timeout(hangs, timeout=0.02, description="x", key="probe-b", cooldown=0.1)

    assert len(load_calls) == 2, "a call after the cooldown must retry"


def test_a_keyed_success_clears_a_previous_failure_and_is_cached():
    """All callers for one key use the SAME cooldown in practice (it is a
    property of the call site, not the individual call) -- this test matches
    that realistic shape rather than mixing cooldowns for one key."""
    load_calls = []

    def flaky_then_ok():
        load_calls.append(time.monotonic())
        if len(load_calls) == 1:
            raise RuntimeError("first attempt fails")
        return "ok"

    with pytest.raises(RuntimeError):
        load_with_timeout(flaky_then_ok, timeout=5.0, description="x", key="probe-c", cooldown=0.05)
    time.sleep(0.08)  # past the cooldown
    result = load_with_timeout(flaky_then_ok, timeout=5.0, description="x", key="probe-c", cooldown=0.05)
    assert result == "ok"
    # a THIRD call, still within the SAME cooldown window, must reuse the
    # cached success and not re-attempt
    result2 = load_with_timeout(flaky_then_ok, timeout=5.0, description="x", key="probe-c", cooldown=0.05)
    assert result2 == "ok"
    assert len(load_calls) == 2, "a cached success must not be re-attempted either"


def test_different_keys_never_share_a_failure():
    load_calls = {"x": 0, "y": 0}

    def make_hang(name):
        def hangs():
            load_calls[name] += 1
            time.sleep(30)
        return hangs

    with pytest.raises(ModelLoadTimeout):
        load_with_timeout(make_hang("x"), timeout=0.02, description="x", key="probe-d-x", cooldown=5.0)
    with pytest.raises(ModelLoadTimeout):
        load_with_timeout(make_hang("y"), timeout=0.02, description="y", key="probe-d-y", cooldown=5.0)

    assert load_calls == {"x": 1, "y": 1}


def test_no_key_means_no_caching_at_all_backward_compatible():
    """The original API (no key) must behave exactly as before -- every
    caller that predates this change is unaffected."""
    load_calls = []

    def hangs():
        load_calls.append(time.monotonic())
        time.sleep(30)

    with pytest.raises(ModelLoadTimeout):
        load_with_timeout(hangs, timeout=0.02, description="x")
    with pytest.raises(ModelLoadTimeout):
        load_with_timeout(hangs, timeout=0.02, description="x")

    assert len(load_calls) == 2, "with no key, every call must attempt the network (unchanged behavior)"


def test_concurrent_callers_with_the_same_key_share_one_in_flight_attempt():
    """Red review, 2026-08-20: with no single-flight, N concurrent callers
    arriving during the first attempt's window each spawn their own loader
    thread -- a resource waste AND the exact multi-live-model condition this
    repo's own _MODEL_CACHE docstring warns destabilizes MPS to the point of
    segfault. The first caller's attempt must be AWAITED by concurrent
    callers, not duplicated."""
    import threading
    import time

    started = threading.Event()
    release = threading.Event()
    call_count = []

    def slow_but_single():
        call_count.append(time.monotonic())
        started.set()
        assert release.wait(timeout=5), "test sync stalled"
        return "loaded"

    results = []
    errors = []

    def worker():
        try:
            results.append(load_with_timeout(slow_but_single, timeout=5.0,
                                             description="x", key="single-flight-probe"))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for th in threads:
        th.start()
    assert started.wait(timeout=5), "the load never started"
    release.set()
    for th in threads:
        th.join(timeout=5)

    assert len(call_count) == 1, (
        f"the underlying load ran {len(call_count)} times for 5 concurrent "
        f"callers -- single-flight requires exactly one")
    assert len(results) == 5 and all(r == "loaded" for r in results)
    assert not errors


def test_env_override_raises_the_load_bound(monkeypatch):
    """ALEXANDRIA_MODEL_LOAD_TIMEOUT (seconds) overrides the 30s default, so a
    CPU-only NAS host can load a model that legitimately takes longer to
    construct. The override must WIN over the caller default."""
    import time as time_mod

    from alexandria import model_load as ml

    started = time_mod.monotonic()
    monkeypatch.setenv("ALEXANDRIA_MODEL_LOAD_TIMEOUT", "0.1")

    def slow_load():
        time_mod.sleep(5)
        return "done"

    with pytest.raises(ml.ModelLoadTimeout):
        ml.load_with_timeout(slow_load, description="slow model")
    assert time_mod.monotonic() - started < 3  # fired near the 0.1s override


def test_env_override_is_ignored_when_unset(monkeypatch):
    """Default behavior unchanged without the env var: the 30s bound applies."""
    import time as time_mod

    from alexandria import model_load as ml

    monkeypatch.delenv("ALEXANDRIA_MODEL_LOAD_TIMEOUT", raising=False)
    started = time_mod.monotonic()

    def fast_load():
        return "ok"

    assert ml.load_with_timeout(fast_load, description="fast") == "ok"
    assert time_mod.monotonic() - started < 3
