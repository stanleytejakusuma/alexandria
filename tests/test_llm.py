"""Retry/backoff: be a good citizen against a shared, rate-limited endpoint."""

import random
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


# ---- cache-busting nonce, found live 2026-08-05: the gateway's semantic
# (similarity-based) response cache serves a DIFFERENT earlier request's answer
# for a genuinely different prompt when the two are similar enough -- confirmed
# via the gateway's own cache_metrics table showing claude-sonnet-5 (not just the
# Codex-family models already refused above) with 1073 semantic-cache entries and
# 142 real cache hits. A gateway-side fix is being handled in a separate session;
# this is the client-side mitigation that doesn't wait on it: make every outgoing
# request unique enough that a similarity cache can't false-match it against an
# unrelated earlier call. ----


def test_two_calls_with_identical_system_and_user_send_different_payloads(monkeypatch):
    """The whole point: two logically-different calls (or even the same call run
    twice) must never be similar enough to collide in a similarity cache."""
    seen_systems = []

    def capture(self, system, user, temperature=0.0):
        seen_systems.append(system)
        return "ok"

    monkeypatch.setattr(LLMClient, "_once", capture)
    client = LLMClient()
    client.complete("same system prompt", "same user prompt")
    client.complete("same system prompt", "same user prompt")
    assert seen_systems[0] != seen_systems[1]


def test_the_nonce_does_not_corrupt_the_original_system_content():
    """Whatever gets appended must not lose or mangle the caller's actual
    instructions -- a cache-busting marker is worthless if it breaks grading."""
    client = LLMClient()
    busted = client._with_cache_buster("You are a careful grader.")
    assert busted.startswith("You are a careful grader.")
    assert busted != "You are a careful grader."


def test_the_nonce_is_actually_unique_per_call():
    client = LLMClient()
    a = client._with_cache_buster("system text")
    b = client._with_cache_buster("system text")
    assert a != b


def test_open_with_deadline_fires_on_silent_stream(monkeypatch):
    """The stall class found 2026-08-07: a connection that stays alive but
    sends nothing must still hit ONE hard deadline for urlopen+read (socket
    timeouts turn it into an unbounded retry cycle instead)."""
    import time
    import urllib.request
    from alexandria import llm as llm_mod
    from alexandria.llm import _open_with_deadline

    class SilentResponse:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            time.sleep(30)  # never returns data

    monkeypatch.setattr(llm_mod.urllib.request, "urlopen", lambda req, timeout: SilentResponse())
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        _open_with_deadline(urllib.request.Request("http://x"), timeout=1)
    assert time.monotonic() - started < 5


def test_open_with_deadline_returns_body(monkeypatch):
    import urllib.request
    from alexandria import llm as llm_mod
    from alexandria.llm import _open_with_deadline

    class FastResponse:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"ok": true}'

    monkeypatch.setattr(llm_mod.urllib.request, "urlopen", lambda req, timeout: FastResponse())
    body = _open_with_deadline(urllib.request.Request("http://x"), timeout=5)
    assert body == b'{"ok": true}'


def test_a_truncated_response_says_so_instead_of_yielding_broken_json(monkeypatch):
    """finish_reason=length means the model was cut off mid-stream. Returning the
    partial content makes the CALLER fail with 'Unterminated string starting at
    column 30744', which reads like a parser bug and hides the real cause -- how
    14 of 465 session bursts failed on 2026-08-11."""
    import json as _json

    from alexandria import llm as _llm

    body = _json.dumps({
        "choices": [{"finish_reason": "length",
                     "message": {"content": '{"observations": [{"title": "half a str'}}],
        "usage": {"completion_tokens": 8192},
    })
    monkeypatch.setattr(_llm, "_open_with_deadline", lambda req, timeout: body)
    client = LLMClient(base_url="http://x/v1", model="m")

    with pytest.raises(LLMError) as caught:
        client._once("sys", "user")
    assert "truncated" in str(caught.value)
    assert "8192" in str(caught.value), "should report how much came back"
    assert caught.value.retryable is False, "an identical request truncates identically"


def test_a_complete_response_is_returned_untouched(monkeypatch):
    import json as _json

    from alexandria import llm as _llm

    body = _json.dumps({"choices": [{"finish_reason": "stop",
                                     "message": {"content": "full answer"}}]})
    monkeypatch.setattr(_llm, "_open_with_deadline", lambda req, timeout: body)
    assert LLMClient(base_url="http://x/v1", model="m")._once("s", "u") == "full answer"


def test_a_stalling_gateway_cannot_exceed_the_total_call_budget(monkeypatch):
    """#28: the per-attempt deadline is not the same as a bound on complete().

    `_open_with_deadline` (2026-08-07) bounds ONE urlopen+read, so a single
    silent stall costs one deadline. But complete() then retries it: with the
    shipped defaults a fully stalled gateway burns
    (max_retries + 1) x timeout = 5 x 120s of wall clock, plus backoff, for ONE
    complete() call -- and a single /answer chains many of them (gather gap,
    write, per-claim audits, two coverage graders). That is how a stalled
    gateway still turns into a multi-hour request even though every individual
    read was bounded. total_timeout caps the whole call, retries included.
    """
    slept = {"total": 0.0}
    monkeypatch.setattr(time, "sleep", lambda s: slept.__setitem__("total", slept["total"] + s))

    now = {"t": 0.0}
    monkeypatch.setattr(time, "monotonic", lambda: now["t"])

    def always_stalls(self, system, user, temperature=0.0):
        now["t"] += self.timeout          # each attempt burns its full deadline
        err = LLMError(f"urlopen+read exceeded {self.timeout}s deadline")
        err.retryable = True
        raise err

    monkeypatch.setattr(LLMClient, "_once", always_stalls)

    client = LLMClient(timeout=120, max_retries=4, base_delay=2.0, total_timeout=300)
    with pytest.raises(LLMError) as exc:
        client.complete("sys", "user", temperature=0.1)

    assert "total budget" in str(exc.value)
    assert now["t"] <= 300 + 120, (
        f"call ran {now['t']}s against a 300s budget -- the aggregate cap did not hold")
    assert now["t"] < 600, "budget did not stop the full 5-attempt retry cycle"


def test_the_total_budget_does_not_interfere_with_a_healthy_call(monkeypatch):
    """A bounded budget must not break the ordinary retry-then-succeed path."""
    calls = {"n": 0}
    monkeypatch.setattr(time, "sleep", lambda s: None)

    def flaky(self, system, user, temperature=0.0):
        calls["n"] += 1
        if calls["n"] < 3:
            err = LLMError("HTTP 429: slow down"); err.retryable = True
            raise err
        return "ok"

    monkeypatch.setattr(LLMClient, "_once", flaky)
    assert LLMClient(base_delay=0.0).complete("sys", "user", temperature=0.1) == "ok"
    assert calls["n"] == 3


def test_total_timeout_defaults_to_a_bounded_multiple_of_the_attempt_deadline():
    """The default must be finite: an unbounded default is the bug itself."""
    client = LLMClient()
    assert client.total_timeout is not None
    assert 0 < client.total_timeout < client.timeout * (client.max_retries + 1)


def test_budget_expiry_on_the_FINAL_attempt_still_reports_a_non_retryable_budget_error(monkeypatch):
    """Red round 1 on #28: the flag contract broke exactly where it matters.

    The budget check originally sat AFTER `attempt == self.max_retries: raise`,
    so when the budget expired on the last attempt the escaping error was the
    per-attempt RETRYABLE one, not the non-retryable budget error. The wall-clock
    bound still held, but any wrapper trusting `retryable` would retry a call
    that had already spent its whole budget -- nullifying the invariant in the
    one case the flag exists to defend.
    """
    monkeypatch.setattr(time, "sleep", lambda s: None)
    now = {"t": 0.0}
    monkeypatch.setattr(time, "monotonic", lambda: now["t"])

    def always_stalls(self, system, user, temperature=0.0):
        now["t"] += self.timeout
        err = LLMError(f"urlopen+read exceeded {self.timeout}s deadline")
        err.retryable = True
        raise err

    monkeypatch.setattr(LLMClient, "_once", always_stalls)

    # 2 retries x 100s = 300s: the budget lands exactly on the final attempt.
    client = LLMClient(timeout=100, max_retries=2, base_delay=0.0, total_timeout=300)
    with pytest.raises(LLMError) as exc:
        client.complete("sys", "user", temperature=0.1)

    assert "total budget" in str(exc.value)
    assert getattr(exc.value, "retryable", False) is False, (
        "a budget-exhausted call escaped as retryable -- a wrapper would retry it")


def test_backoff_sleep_is_clamped_so_it_cannot_itself_overrun_the_budget(monkeypatch):
    """The clamp was implemented but nothing pinned it (Red: silently removable)."""
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", slept.append)
    # Pin the jitter to its UPPER bound: with random draws, a removed clamp could
    # still pass by luck. Asserting the max draw is clamped is strictly stronger.
    monkeypatch.setattr(random, "uniform", lambda a, b: b)
    now = {"t": 0.0}
    monkeypatch.setattr(time, "monotonic", lambda: now["t"])

    def stall_briefly(self, system, user, temperature=0.0):
        now["t"] += 1.0
        err = LLMError("stall"); err.retryable = True
        raise err

    monkeypatch.setattr(LLMClient, "_once", stall_briefly)

    # base_delay 60s would dwarf the 5s budget if the clamp were removed.
    client = LLMClient(timeout=1, max_retries=5, base_delay=60.0, total_timeout=5)
    with pytest.raises(LLMError):
        client.complete("sys", "user", temperature=0.1)

    assert slept, "no backoff was attempted, so the clamp was never exercised"
    for i, s in enumerate(slept):
        assert s <= 5.0, f"backoff #{i} slept {s}s against a 5s total budget"


def test_total_timeout_zero_means_one_attempt_then_a_budget_error(monkeypatch):
    """0 is 'one attempt then budget error', NOT 'disabled' -- None disables.

    Implemented via the `if attempt:` guard at the loop top; a future
    simplification dropping that guard would refuse the FIRST attempt and report
    "after 0 attempt(s); last error: None". Pin the documented contract.
    """
    monkeypatch.setattr(time, "sleep", lambda s: None)
    calls = {"n": 0}

    def stalls(self, system, user, temperature=0.0):
        calls["n"] += 1
        err = LLMError("stall"); err.retryable = True
        raise err

    monkeypatch.setattr(LLMClient, "_once", stalls)

    client = LLMClient(max_retries=3, base_delay=0.0, total_timeout=0)
    with pytest.raises(LLMError) as exc:
        client.complete("sys", "user", temperature=0.1)

    assert calls["n"] == 1, "a zero budget must still permit exactly one attempt"
    assert "total budget" in str(exc.value)
    assert getattr(exc.value, "retryable", False) is False


# ---------------------------------------------------------------------------
# #47: per-CALL caps do not compose into a per-REQUEST cap.
#
# total_timeout bounds one complete(). But one /answer chains ~15 sequential
# stages (gather gap, write, per-claim audits, two coverage graders, up to two
# repair iterations each re-judging), so a fully dead gateway still costs
# ~15 x (300 + 120)s ~= 1.8 hours. Every unit is bounded; the total is not --
# the exact composition error that made #28 look already-fixed when it wasn't.
# ---------------------------------------------------------------------------

def test_a_shared_deadline_makes_N_chained_calls_cost_ONE_budget(monkeypatch):
    """The whole point: N stalled stages must cost one request budget, not N."""
    from alexandria.llm import RequestDeadline

    monkeypatch.setattr(time, "sleep", lambda s: None)
    now = {"t": 0.0}
    monkeypatch.setattr(time, "monotonic", lambda: now["t"])

    def stalls(self, system, user, temperature=0.0):
        now["t"] += self.timeout
        err = LLMError("stall"); err.retryable = True
        raise err

    monkeypatch.setattr(LLMClient, "_once", stalls)

    deadline = RequestDeadline(600)
    # Three roles, as the answer pipeline builds them: writer + two graders.
    clients = [LLMClient(timeout=100, max_retries=4, base_delay=0.0,
                         total_timeout=300, deadline=deadline) for _ in range(3)]

    failures = 0
    for stage in range(15):                      # ~one /answer worth of stages
        try:
            clients[stage % 3].complete("sys", "user", temperature=0.1)
        except LLMError:
            failures += 1

    assert failures == 15, "every stage should fail against a dead gateway"
    assert now["t"] <= 600 + 100, (
        f"15 stages burned {now['t']}s against a 600s request budget -- the "
        f"deadline did not compose")


def test_once_the_request_deadline_is_spent_later_calls_fail_fast_without_network(monkeypatch):
    """A spent budget must short-circuit BEFORE touching the transport.

    Otherwise each later stage still pays one attempt deadline, and 15 stages
    cost 15 x timeout even though the request was already over.
    """
    from alexandria.llm import RequestDeadline

    now = {"t": 0.0}
    monkeypatch.setattr(time, "monotonic", lambda: now["t"])
    attempts = {"n": 0}

    def counting(self, system, user, temperature=0.0):
        attempts["n"] += 1
        now["t"] += self.timeout
        err = LLMError("stall"); err.retryable = True
        raise err

    monkeypatch.setattr(LLMClient, "_once", counting)
    monkeypatch.setattr(time, "sleep", lambda s: None)

    # 100s budget, 100s attempt: one attempt spends it exactly.
    deadline = RequestDeadline(100)
    client = LLMClient(timeout=100, max_retries=0, base_delay=0.0, deadline=deadline)

    with pytest.raises(LLMError):
        client.complete("s", "u", temperature=0.1)      # burns the whole budget
    spent = attempts["n"]
    assert spent == 1

    with pytest.raises(LLMError) as exc:
        client.complete("s", "u", temperature=0.1)      # budget gone: no network

    assert attempts["n"] == spent, "a spent request budget still hit the transport"
    assert "request budget" in str(exc.value)
    assert getattr(exc.value, "retryable", False) is False


def test_the_per_call_cap_is_clamped_to_whatever_the_request_has_left(monkeypatch):
    """A 300s call cap must not overrun a request with 50s remaining."""
    from alexandria.llm import RequestDeadline

    now = {"t": 0.0}
    monkeypatch.setattr(time, "monotonic", lambda: now["t"])
    monkeypatch.setattr(time, "sleep", lambda s: None)

    def stalls(self, system, user, temperature=0.0):
        now["t"] += 20
        err = LLMError("stall"); err.retryable = True
        raise err

    monkeypatch.setattr(LLMClient, "_once", stalls)

    deadline = RequestDeadline(50)
    client = LLMClient(timeout=20, max_retries=10, base_delay=0.0,
                       total_timeout=300, deadline=deadline)
    with pytest.raises(LLMError):
        client.complete("s", "u", temperature=0.1)

    assert now["t"] <= 50 + 20, f"call ran to {now['t']}s against a 50s request remainder"


def test_no_deadline_means_unchanged_behaviour():
    """Backward compatible: every existing caller passes no deadline."""
    assert LLMClient().deadline is None
