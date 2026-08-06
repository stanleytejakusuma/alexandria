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


def _read_with_deadline(r, timeout: int) -> bytes:
    """Read the full response body under a hard deadline.

    urllib's socket timeout does not bound a stalled STREAM: a proxy that
    keeps the connection alive while sending nothing (observed live
    2026-08-07: an unattended synthesis run sat silent ~40min on an idle
    ESTABLISHED pair) lets r.read() block past any timeout. Run the read
    on a daemon thread and abandon it at the deadline."""
    result: list[bytes] = []

    def _read() -> None:
        try:
            result.append(r.read())
        except Exception:
            pass  # the deadline path abandons us anyway

    worker = threading.Thread(target=_read, daemon=True)
    worker.start()
    worker.join(timeout=timeout)
    if not result:
        raise TimeoutError(f"response body read exceeded {timeout}s deadline")
    return result[0]


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
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                body = json.loads(_read_with_deadline(r, self.timeout))
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
