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

__all__ = ["LLMClient", "ScriptedClient", "LLMError"]


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


@dataclass
class LLMClient:
    base_url: str = "http://127.0.0.1:20128/v1"
    model: str = "omni-claude-sonnet"
    api_key_env: str = "ALEXANDRIA_LLM_KEY"
    timeout: int = 120
    max_retries: int = 4
    base_delay: float = 2.0
    min_interval: float = 0.0     # floor between calls from one client; be a good citizen
    _last_call: float = field(default=0.0, repr=False)
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
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            if self.min_interval:
                gap = time.monotonic() - self._last_call
                if gap < self.min_interval:
                    time.sleep(self.min_interval - gap)
            try:
                out = self._once(system, user, temperature)
                self._last_call = time.monotonic()
                return out
            except LLMError as exc:
                last = exc
                self._last_call = time.monotonic()
                if not getattr(exc, "retryable", False) or attempt == self.max_retries:
                    raise
                # Exponential backoff with full jitter: synchronized retries from a
                # worker pool are how one rate limit becomes a thundering herd.
                delay = self.base_delay * (2 ** attempt)
                time.sleep(random.uniform(0, delay))
        raise last if last else LLMError("unreachable")

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
            self.last_usage = {
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
                "cache_read": details.get("cached_tokens", 0)
                              if isinstance(details, dict) else 0,
            }
            self.last_usage_error = ""
        except Exception as exc:  # usage is advisory; never fail a call on it
            self.last_usage_error = f"{type(exc).__name__}: {exc}"
        try:
            return body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
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
