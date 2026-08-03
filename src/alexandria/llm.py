"""One LLM dialect: OpenAI-compatible chat completions against any base_url.

`ScriptedClient` replays canned responses so prompt-parsing and failure posture are
testable offline with no API key -- the deterministic half of the eval split.
"""

from __future__ import annotations

import json
import os
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

    def complete(self, system: str, user: str, temperature: float = 0.0) -> str:
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
            raise LLMError(f"HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise LLMError(str(exc)) from exc
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
