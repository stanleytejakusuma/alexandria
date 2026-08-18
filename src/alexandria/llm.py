"""One LLM dialect: OpenAI-compatible chat completions against any base_url.

`ScriptedClient` replays canned responses so prompt-parsing and failure posture are
testable offline with no API key -- the deterministic half of the eval split.
"""

from __future__ import annotations

import json
import os
import random
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field

__all__ = ["BudgetExhausted", "LLMClient", "RequestDeadline", "ScriptedClient", "LLMError"]


class RequestDeadline:
    """One wall-clock budget shared by every LLM call in a single request.

    #28 capped a single `complete()`. That does NOT compose: one /answer chains
    ~15 sequential stages (gather gap, write, per-claim audits, two coverage
    graders, up to two repair iterations each re-judging), so a fully dead
    gateway still cost ~15 x (total_timeout + timeout) ~= 1.8 hours. Every unit
    was bounded and the total was not -- the same composition error that made
    backlog #28 look already-fixed when it wasn't.

    Passed to every client the pipeline builds, so the writer and both graders
    draw down ONE budget. Once it is spent, later stages fail fast without
    touching the transport instead of each paying another attempt deadline.

    Deliberately monotonic-clock based and thread-safe by construction: it
    holds a fixed start time and reads `time.monotonic()`, so parallel claim
    audits sharing one instance need no lock.
    """

    def __init__(self, budget_seconds: float | None) -> None:
        # None disables the budget, matching total_timeout's convention from
        # #28 (where None means unlimited). Without this, answer_timeout=None
        # would TypeError inside remaining() -- a silent contradiction of the
        # convention the operator already learned one change earlier.
        if budget_seconds is not None and budget_seconds < 0:
            raise ValueError("request budget must be non-negative, or None to disable")
        self.budget_seconds = budget_seconds
        self.started = time.monotonic()

    def remaining(self) -> float | None:
        """Seconds left (floored at 0), or None when no budget is set."""
        if self.budget_seconds is None:
            return None
        return max(0.0, self.budget_seconds - (time.monotonic() - self.started))

    def expired(self) -> bool:
        remaining = self.remaining()
        return remaining is not None and remaining <= 0.0


def _open_with_deadline(req, timeout: int) -> bytes:
    """Perform urlopen + full body read under ONE hard deadline.

    The socket timeout bounds each syscall, but a gateway that stalls
    repeatedly (observed live 2026-08-07: v4 leg, 120s-poll cycles for
    hours with zero output) turns bounded reads into an unbounded retry
    cycle. Wrapping the entire call means one stall costs exactly one
    deadline, then raises retryable -- an attempt ends in minutes, not
    hours. The read runs on a daemon thread and is abandoned at the
    deadline; the underlying socket is never closed (abandoned threads
    are harmless, they die with the process)."""
    result: dict = {}

    def work() -> None:
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                result["body"] = r.read()
        except BaseException as exc:  # propagate urlopen's own error types
            result["error"] = exc

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    worker.join(timeout=timeout)
    if "error" in result:
        raise result["error"]
    if "body" not in result:
        raise TimeoutError(f"urlopen+read exceeded {timeout}s deadline")
    return result["body"]


class LLMError(RuntimeError):
    pass


class BudgetExhausted(LLMError):
    """A call stopped because a time budget ran out, not because of its content.

    A distinct TYPE, not a message prefix: the per-claim audit path treats an
    LLMError as "this claim failed its check", so a budget expiry that arrives
    as a plain LLMError would be recorded as a CONTENT-quality failure -- the
    answer would then report failed_claims that were never actually judged, and
    a repair loop could burn iterations against a budget that is already spent.
    Callers must be able to tell "we ran out of time" from "this claim is bad".
    """


@dataclass
class LLMClient:
    base_url: str = "http://127.0.0.1:20128/v1"
    model: str = "deepseek-v4-pro"
    api_key_env: str = "ALEXANDRIA_LLM_KEY"
    timeout: int = 120
    max_retries: int = 4
    base_delay: float = 2.0
    # #28: `_open_with_deadline` bounds ONE urlopen+read, so a single silent
    # stall costs one deadline -- but complete() retries it, so a fully stalled
    # gateway still burns (max_retries + 1) * timeout plus backoff for ONE call,
    # and a single /answer chains many calls (gather gap, write, per-claim
    # audits, two coverage graders). That is how bounded reads still added up to
    # a multi-hour request. This caps the WHOLE call, retries included. Chosen
    # as ~2 attempt-deadlines: long enough that one slow-but-alive response plus
    # a retry succeeds, short enough that a dead gateway fails in minutes.
    # None disables the cap; 0 means "one attempt, then budget error" (not
    # "disabled"). Two properties are deliberate, not oversights:
    #  - a slow-but-alive attempt that SUCCEEDS past the budget is still
    #    returned: the budget is enforced on the failure path only, so
    #    `timeout` stays the binding constraint for a legitimately slow model;
    #  - a call may overrun the budget by up to `timeout` (plus any
    #    `min_interval` reserve wait), because abandoning an in-flight read is
    #    worse than a bounded overrun -- it turns would-succeed responses into
    #    guaranteed failures.
    total_timeout: float | None = 300.0
    # #47: an optional request-scoped budget SHARED across clients. None keeps
    # the pre-existing per-call behaviour for every caller that does not opt in.
    deadline: "RequestDeadline | None" = None
    min_interval: float = 0.0     # floor between calls from one client; be a good citizen
    _last_call: float = field(default=0.0, repr=False)
    # Reserve rate-limit start slots and update diagnostic usage under small
    # critical sections. Network I/O deliberately remains outside these locks.
    _rate_limit_lock: threading.Lock = field(default_factory=threading.Lock,
                                              init=False, repr=False)
    _usage_lock: threading.Lock = field(default_factory=threading.Lock,
                                        init=False, repr=False)
    # Prompt-cache accounting: the gateway decides actual prompt-cache hits
    # (OpenRouter/Anthropic cache billing is server-side); we record what we
    # see so the audit trail can show prompt-token drift. Populated by _once.
    last_usage: dict = field(default_factory=dict, repr=False)
    last_usage_error: str = field(default="", repr=False)

    # Retry only what retrying can fix. A 400 is a bad request and will stay bad;
    # hammering it wastes somebody's quota to no purpose.
    RETRY_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})

    # Known-bad model+temperature combo, found live 2026-08-05: gpt-5.6-sol at
    # temperature=0 returns cross-contaminated responses copied from unrelated
    # earlier requests -- confirmed via raw curl bypassing this client entirely,
    # concurrency and blanket caching both ruled out by direct test. Initially
    # assumed gpt-5.6-terra was unaffected off a 2-case probe; a larger real
    # workload caught terra exhibiting the identical bug, so the whole class is
    # suspect, not just sol. All four models below share the same eligibility
    # for a flex/priority service-tier routing feature in the gateway's own
    # source (unconfirmed as the actual mechanism -- that's a live runtime
    # setting, not visible from source, and out of scope to chase further
    # inside a shared third-party service without a concrete target to change).
    # gpt-5.6-luna and gpt-5.5 are untested directly but share the same
    # eligibility list, so blocked on the same suspicion, not confirmed
    # innocence. At nonzero temperature, both tested models (sol, terra) are
    # clean. Refuse at temperature=0 rather than trust an answer nothing here
    # can tell is corrupted.
    _CODEX_FAST_TIER_MODELS = ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5")

    def complete(self, system: str, user: str, temperature: float = 0.0) -> str:
        if temperature == 0.0 and self.model.endswith(self._CODEX_FAST_TIER_MODELS):
            raise LLMError(
                f"refusing to call {self.model!r} at temperature=0: this model is in "
                f"the fast-tier-eligible class confirmed (sol, terra) or suspected "
                f"(luna, gpt-5.5) to return cross-contaminated responses from "
                f"unrelated earlier requests at temperature=0 (see llm.py comment). "
                f"Use a nonzero temperature or a different model.")
        system = self._with_cache_buster(system)
        # #47: refuse before touching the transport when the REQUEST budget is
        # already spent. Without this each later stage still pays one attempt
        # deadline, so 15 stages cost 15 x timeout even though the request was
        # over -- the per-call cap composing into nothing.
        if self.deadline is not None and self.deadline.expired():
            err = BudgetExhausted(
                f"request budget exhausted ({self.deadline.budget_seconds:.0f}s) before "
                f"this call started (spent by earlier stages in this request)")
            err.retryable = False
            raise err
        last: Exception | None = None
        started = time.monotonic()
        for attempt in range(self.max_retries + 1):
            # Budget check at loop TOP so it dominates the last-attempt raise
            # below: with the check only in the backoff path, a budget expiring
            # on the FINAL attempt escaped as the per-attempt RETRYABLE error,
            # and a wrapper trusting that flag would retry an already-spent call.
            # Checking here also refuses to START an attempt with no budget left.
            if attempt:
                budget_error = self._budget_error(started, attempt, last)
                if budget_error is not None:
                    raise budget_error from last
            self._reserve_call_slot()
            try:
                out = self._once(system, user, temperature)
                return out
            except LLMError as exc:
                last = exc
                if not getattr(exc, "retryable", False):
                    raise
                if attempt == self.max_retries:
                    # Retries are exhausted. If the budget is ALSO spent, say so
                    # and make it non-retryable: otherwise a call that burned its
                    # whole budget escapes as retryable and a wrapper retries it.
                    # Explicit assign-check-raise rather than `x or exc`: it does
                    # not depend on LLMError never defining __bool__, and it lets
                    # the budget error name its direct cause like the loop-top exit.
                    budget_error = self._budget_error(started, attempt + 1, exc)
                    if budget_error is not None:
                        raise budget_error from exc
                    raise
                # Exponential backoff with full jitter: synchronized retries from a
                # worker pool are how one rate limit becomes a thundering herd.
                delay = self.base_delay * (2 ** attempt)
                # Never sleep past the budget: a long jittered backoff must not
                # be the thing that overruns it. The loop-top check then converts
                # an exhausted budget into the non-retryable budget error.
                elapsed = time.monotonic() - started
                if self.total_timeout is not None:
                    delay = min(delay, max(0.0, self.total_timeout - elapsed))
                time.sleep(random.uniform(0, delay))
        raise last if last else LLMError("unreachable")

    def _budget_error(self, started: float, attempts: int, last: Exception | None) -> "LLMError | None":
        """The non-retryable error for a call that spent its whole budget, or None.

        Shared by BOTH exits -- the loop-top check (refuse to start another
        attempt) and retry exhaustion (the budget landed on the final attempt).
        Covering only the first left the last-attempt path escaping as retryable,
        which is precisely the case the flag exists to defend against.
        """
        if self.deadline is not None and self.deadline.expired():
            # The shared request budget binds first: report THAT, since retrying
            # this call under a spent request budget is pointless.
            err = BudgetExhausted(
                f"request budget exhausted ({self.deadline.budget_seconds:.0f}s) after "
                f"{attempts} attempt(s); last error: {last}")
            err.retryable = False
            return err
        if self.total_timeout is None:
            return None
        elapsed = time.monotonic() - started
        if elapsed < self.total_timeout:
            return None
        err = BudgetExhausted(
            f"call exceeded its {self.total_timeout:.0f}s total budget after "
            f"{attempts} attempt(s) ({elapsed:.0f}s elapsed); last error: {last}")
        err.retryable = False
        return err

    def _reserve_call_slot(self) -> None:
        """Reserve one outbound-call start time without holding a network lock.

        Advancing the reservation before sleeping closes the race where parallel
        callers all read the same ``_last_call`` and start together. A retry gets
        its own slot; the actual HTTP call stays outside the critical section.
        """
        if not self.min_interval:
            return
        with self._rate_limit_lock:
            now = time.monotonic()
            start_at = max(now, self._last_call + self.min_interval)
            self._last_call = start_at
        delay = start_at - now
        if delay > 0:
            time.sleep(delay)

    # Found live 2026-08-05: the gateway's own semantic (similarity-based) response
    # cache serves an unrelated earlier request's answer when two prompts are similar
    # enough -- confirmed via the gateway's own cache_metrics table (claude-sonnet-5
    # alone: 1073 semantic-cache entries, 142 real cache hits served; not limited to
    # the Codex-family models refused above). A gateway-side fix is being handled
    # separately; this doesn't wait on it. Appending a unique marker to every outgoing
    # system prompt means no two real requests -- even two literally identical ones --
    # are ever similar enough to false-match in a similarity cache. The cost (losing a
    # legitimate exact-duplicate cache hit, if any ever existed) is nothing next to the
    # benefit (a wrong answer can never silently look like a right one).
    def _with_cache_buster(self, system: str) -> str:
        return f"{system}\n\n[internal request id, not part of the task: {uuid.uuid4()}]"

    def _once(self, system: str, user: str, temperature: float = 0.0) -> str:
        payload = json.dumps({
            "model": self.model,
            "temperature": temperature,
            # Explicit: some OpenAI-compatible gateways stream by DEFAULT when the flag
            # is absent, returning SSE frames that json.loads cannot parse.
            "stream": False,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
        }).encode()
        req = urllib.request.Request(
            f"{self.base_url.rstrip('/')}/chat/completions",
            data=payload,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {os.environ.get(self.api_key_env, 'none')}"},
        )
        try:
            body = json.loads(_open_with_deadline(req, self.timeout))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            err = LLMError(f"HTTP {exc.code}: {detail}")
            err.retryable = exc.code in self.RETRY_STATUS
            raise err from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            err = LLMError(str(exc))
            err.retryable = True          # transport-level: worth another try
            raise err from exc
        try:
            usage = body.get("usage") or {}
            details = usage.get("prompt_tokens_details")
            observed_usage = {
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
                "cache_read": details.get("cached_tokens", 0)
                              if isinstance(details, dict) else 0,
            }
            with self._usage_lock:
                self.last_usage = observed_usage
                self.last_usage_error = ""
        except Exception as exc:  # usage is advisory; never fail a call on it
            with self._usage_lock:
                self.last_usage_error = f"{type(exc).__name__}: {exc}"
        try:
            choice = body["choices"][0]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"unexpected response shape: {str(body)[:200]}") from exc
        # A truncated response is not a malformed one. Without this check the
        # caller parses a half-written JSON object and reports "Unterminated
        # string starting at column 30744", which reads like a parser bug and
        # says nothing about the real cause. Observed 2026-08-11: 14 of 465
        # session bursts failed exactly this way. NOT retryable -- an identical
        # request truncates at an identical place, so retrying only burns calls.
        if choice.get("finish_reason") == "length":
            with self._usage_lock:
                completion_tokens = self.last_usage.get("completion_tokens", 0)
            err = LLMError(
                "response truncated at the output limit (finish_reason=length, "
                f"{completion_tokens} completion tokens) -- send less input or raise the limit")
            err.retryable = False
            raise err
        try:
            return choice["message"]["content"]
        except (KeyError, TypeError) as exc:
            raise LLMError(f"unexpected response shape: {str(body)[:200]}") from exc


@dataclass
class ScriptedClient:
    """Replays canned responses in order. Offline, deterministic, no API key."""

    responses: list[str] = field(default_factory=list)
    calls: list[tuple[str, str]] = field(default_factory=list)

    def complete(self, system: str, user: str, temperature: float = 0.0) -> str:
        self.calls.append((system, user))
        if not self.responses:
            raise LLMError("ScriptedClient exhausted")
        return self.responses.pop(0)
