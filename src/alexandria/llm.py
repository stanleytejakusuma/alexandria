"""One LLM dialect: OpenAI-compatible chat completions against any base_url.

`ScriptedClient` replays canned responses so prompt-parsing and failure posture are
testable offline with no API key -- the deterministic half of the eval split.
"""

from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

__all__ = ["LLMClient", "ScriptedClient", "LLMError"]


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

    def complete(self, system: str, user: str, temperature: float = 0.0) -> str:
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
                body = json.loads(r.read())
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
