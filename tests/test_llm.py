"""Retry/backoff: be a good citizen against a shared, rate-limited endpoint."""

import time
import pytest
from alexandria.llm import LLMClient, LLMError


def test_client_retries_retryable_errors(monkeypatch):
    calls = {"n": 0}

    def flaky(self, system, user, temperature=0.0):
        calls["n"] += 1
        if calls["n"] < 3:
            err = LLMError("HTTP 429: slow down"); err.retryable = True
            raise err
        return "ok"

    monkeypatch.setattr(LLMClient, "_once", flaky)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    assert LLMClient(base_delay=0.01).complete("s", "u") == "ok"
    assert calls["n"] == 3


def test_client_does_not_retry_a_bad_request(monkeypatch):
    """A 400 will stay 400; retrying only burns someone's quota."""
    calls = {"n": 0}

    def bad(self, system, user, temperature=0.0):
        calls["n"] += 1
        err = LLMError("HTTP 400: bad model"); err.retryable = False
        raise err

    monkeypatch.setattr(LLMClient, "_once", bad)
    with pytest.raises(LLMError, match="400"):
        LLMClient(base_delay=0.01).complete("s", "u")
    assert calls["n"] == 1


def test_retries_are_bounded(monkeypatch):
    calls = {"n": 0}

    def always(self, system, user, temperature=0.0):
        calls["n"] += 1
        err = LLMError("HTTP 503"); err.retryable = True
        raise err

    monkeypatch.setattr(LLMClient, "_once", always)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    with pytest.raises(LLMError):
        LLMClient(max_retries=2, base_delay=0.01).complete("s", "u")
    assert calls["n"] == 3          # initial + 2 retries, never unbounded


def test_min_interval_throttles(monkeypatch):
    slept = []
    monkeypatch.setattr(LLMClient, "_once", lambda self, s, u, t=0.0: "ok")
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    c = LLMClient(min_interval=0.5)
    c.complete("s", "u"); c.complete("s", "u")
    assert any(s > 0 for s in slept)
