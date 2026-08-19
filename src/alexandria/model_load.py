"""Bounded, timed loading for opaque third-party model constructors.

CrossEncoder(model_name) / SentenceTransformer(model_name) / mlx_embeddings
.load(model_name) all make their own network calls with no native timeout
parameter, and each underlying HTTP request is bounded individually
(huggingface_hub defaults to ~10s per request) but the TOTAL across the
several sequential files a model load fetches (config, tokenizer, weights)
is not bounded at all. A network that is slow rather than absent -- not the
case HF_HUB_OFFLINE=1 already catches in ~3s -- can therefore hang a caller's
very first `alexandria search` or `alexandria index` for minutes with no
indication anything is wrong. This is backlog #44.

The mechanism: run the load in a daemon thread and join with a deadline. A
synchronous, GIL-holding-while-blocked-on-network call cannot be interrupted
from outside, so a timeout here means "the caller gets control back," not
"the background attempt is cancelled" -- the thread is daemonic so it never
blocks process exit, and it is simply abandoned (its eventual result, if any,
is never observed by the timed-out caller).
"""

from __future__ import annotations

import threading
from typing import Callable, TypeVar

__all__ = ["ModelLoadTimeout", "load_with_timeout"]

T = TypeVar("T")

# Generous (a real model load is a few seconds even over a normal network) but
# finite: past this, "slow" and "hung" are indistinguishable to the caller, and
# a hang must never masquerade as normal startup.
DEFAULT_LOAD_TIMEOUT = 30.0


class ModelLoadTimeout(Exception):
    """A model load did not complete within its bound. Names what was loading
    and the likely cause, matching ingest.py's refusal style
    ("pdftotext is not installed...", "no vision credential available...")."""


def load_with_timeout(load: Callable[[], T], *, timeout: float = DEFAULT_LOAD_TIMEOUT,
                      description: str) -> T:
    """Run ``load`` to completion, or raise ModelLoadTimeout after ``timeout``
    seconds.

    A real exception raised BY ``load`` (missing package, malformed model id,
    huggingface_hub's own fast OSError under HF_HUB_OFFLINE=1) propagates as
    itself -- this function only ever substitutes a TIMEOUT for a HANG, never
    for a genuine error, so a caller's existing `except SomeSpecificError`
    handling is undisturbed.
    """
    result: list[T] = []
    error: list[BaseException] = []

    def run() -> None:
        try:
            result.append(load())
        except BaseException as exc:  # noqa: BLE001 -- must reach the caller's thread
            error.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout)

    if thread.is_alive():
        raise ModelLoadTimeout(
            f"{description} did not load within {timeout:g}s. This looks like "
            f"a slow or unreachable network rather than a missing model -- "
            f"check connectivity, or set HF_HUB_OFFLINE=1 to fail fast when "
            f"nothing is cached locally.")
    if error:
        raise error[0]
    return result[0]
