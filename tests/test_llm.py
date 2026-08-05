"""Retry/backoff: be a good citizen against a shared, rate-limited endpoint."""

import re
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


# ---- known-bad model+temperature combo, found live 2026-08-05: gpt-5.6-sol at
# temperature=0 returns cross-contaminated responses from unrelated earlier requests
# (confirmed via raw curl, bypassing this client entirely -- ruled out concurrency,
# ruled out blanket gateway-level caching, ruled out a generic prompt-attractor; narrowed
# to this exact model+temp combo specifically, likely OpenAI's own flex/priority
# service_tier routing for this connection). Guard against it at the client level so a
# future caller can't silently trust a corrupted response. ----


@pytest.mark.parametrize("model", ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5"])
def test_codex_fast_tier_models_at_temperature_zero_are_refused(model):
    """Confirmed live 2026-08-05: gpt-5.6-sol AND gpt-5.6-terra both return responses
    cross-contaminated from unrelated earlier requests at temperature=0 -- terra was
    initially (wrongly) assumed clean off a 2-case probe; a larger run caught it too.
    All four models sharing the same fast-tier eligibility list are the same suspect
    class until the gateway's own service-tier setting is confirmed/fixed, not just
    the two empirically caught so far."""
    with pytest.raises(LLMError, match=re.escape(model)):
        LLMClient(model=model).complete("s", "u", temperature=0.0)


def test_prefixed_forms_of_a_bad_model_are_also_refused():
    """The bad aliases are reachable through provider-prefixed forms too (cu/gpt-5.6-sol,
    cc/gpt-5.6-terra, openrouter/anthropic/... style routing) -- match on suffix, not
    exact string, so a routing prefix doesn't quietly bypass the guard."""
    with pytest.raises(LLMError, match="gpt-5.6-terra"):
        LLMClient(model="cu/gpt-5.6-terra").complete("s", "u", temperature=0.0)


def test_codex_fast_tier_model_at_nonzero_temperature_is_allowed(monkeypatch):
    monkeypatch.setattr(LLMClient, "_once", lambda self, s, u, temperature=0.0: "ok")
    assert LLMClient(model="gpt-5.6-sol").complete("s", "u", temperature=0.3) == "ok"
    assert LLMClient(model="gpt-5.6-terra").complete("s", "u", temperature=0.3) == "ok"


def test_models_outside_the_fast_tier_list_are_unaffected(monkeypatch):
    monkeypatch.setattr(LLMClient, "_once", lambda self, s, u, temperature=0.0: "ok")
    assert LLMClient(model="claude-sonnet-5").complete("s", "u", temperature=0.0) == "ok"
    assert LLMClient(model="claude-fable-5").complete("s", "u", temperature=0.0) == "ok"
