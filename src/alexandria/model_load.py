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

SHARED KEYED COOLDOWN (Red review, 2026-08-20): a bounded per-call timeout is
not automatically a bounded TOTAL cost when it can be paid repeatedly by
independent callers -- CrossEncoderReranker originally hit this: a
persistently slow network made EVERY caller re-pay the full timeout, and
36+ test-suite call sites compounded a 30s bound into an 11-minute-plus CI
hang. The FIRST fix gave the reranker a module-level cache and the embedders
a PER-INSTANCE memoization keyed on the assumption "a fresh instance is built
per top-level operation." That assumption does not hold for serve
(build_serve_context builds one embedder ONCE and reuses it for the process's
life, warming it proactively at startup) -- so a boot-time network blip would
have made the per-instance failure PERMANENT, the opposite of the intended
asymmetry. This module now owns the cooldown as a shared, keyed facility, so
every caller gets correct behavior by construction rather than by an
unenforced assumption about how long an instance lives.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Callable, TypeVar

__all__ = ["ModelLoadTimeout", "clear_failure_cache", "load_with_timeout"]

T = TypeVar("T")

# Generous (a real model load is a few seconds even over a normal network) but
# finite: past this, "slow" and "hung" are indistinguishable to the caller, and
# a hang must never masquerade as normal startup.
#
# The 30s default fits the Mac (Apple Silicon) deploy. A CPU-only NAS/cloud
# host can legitimately exceed 30s constructing the same model (measured
# during the 2026-08-23 NAS migration: ~23s of shard progress plus
# torch/pooling init crossed the bound). Override with
# ALEXANDRIA_MODEL_LOAD_TIMEOUT (seconds) in the supervisor EnvironmentFile.
DEFAULT_LOAD_TIMEOUT = 30.0


def _effective_load_timeout(timeout: float | None) -> float:
    """Env override (ALEXANDRIA_MODEL_LOAD_TIMEOUT, seconds) wins over the
    caller's default, so a deployment can bound the bound without code."""
    raw = os.environ.get("ALEXANDRIA_MODEL_LOAD_TIMEOUT", "").strip()
    if raw:
        try:
            return max(float(raw), 1.0)
        except ValueError:
            pass
    return timeout if timeout is not None else DEFAULT_LOAD_TIMEOUT

# A network blip must not become a process-lifetime outage (the exact bug
# fixed here), but must also not force a real per-caller cost floor when the
# network is reliably down -- 60s balances "retry reasonably soon" against
# "don't hammer a genuinely dead endpoint."
DEFAULT_COOLDOWN = 60.0

_lock = threading.Lock()
# Keyed identically by the CALLER (model name, precision, whatever makes two
# attempts "the same load"). Value: (monotonic failure time, exception) for a
# remembered failure, or (monotonic time, result) is NOT stored here --
# success caching stays the caller's own concern (e.g. CrossEncoderReranker's
# _MODEL_CACHE) EXCEPT when a key is used with cooldown-based success reuse,
# which this module also supports so a caller does not need two separate
# caches for "remember failure" and "remember success."
_state: dict[str, tuple[float, bool, object]] = {}  # key -> (at, is_success, value_or_exc)

# Single-flight: key -> an in-flight Condition, so N concurrent callers for
# the SAME key share ONE load attempt instead of each spawning a loader
# thread (Red review, 2026-08-20). Without this, concurrent callers arriving
# during the first attempt's window each ran their own load -- a resource
# waste AND the exact multi-live-model condition this repo's own
# _MODEL_CACHE docstring warns destabilizes MPS to the point of segfault.
_in_flight: dict[str, threading.Condition] = {}


class ModelLoadTimeout(Exception):
    """A model load did not complete within its bound. Names what was loading
    and the likely cause, matching ingest.py's refusal style
    ("pdftotext is not installed...", "no vision credential available...")."""


def clear_failure_cache() -> None:
    """Reset every remembered failure/success entry. Used by TESTS to prevent
    cross-test interference through the shared keyed cache (Red review,
    2026-08-20): timeout-behavior tests with overlapping keys would otherwise
    leak one test's cached failure into the next. Not for production use --
    clearing the cache mid-process would re-expose every caller to the
    compounding-timeout bug it exists to prevent."""
    with _lock:
        _state.clear()


def load_with_timeout(load: Callable[[], T], *, timeout: float | None = DEFAULT_LOAD_TIMEOUT,
                      description: str, key: str | None = None,
                      cooldown: float = DEFAULT_COOLDOWN) -> T:
    timeout = _effective_load_timeout(timeout)
    """Run ``load`` to completion, or raise ModelLoadTimeout after ``timeout``
    seconds.

    A real exception raised BY ``load`` (missing package, malformed model id,
    huggingface_hub's own fast OSError under HF_HUB_OFFLINE=1) propagates as
    itself -- this function only ever substitutes a TIMEOUT for a HANG, never
    for a genuine error, so a caller's existing `except SomeSpecificError`
    handling is undisturbed.

    ``key`` (optional, no caching at all when omitted -- every pre-existing
    caller is unaffected): when given, a failure OR a success within
    ``cooldown`` seconds is remembered and replayed without touching the
    network again. This is deliberately keyed by identity, not by instance --
    it survives across as many caller instances as share the same key, which
    is what makes it correct for both "one instance per operation" (CLI) and
    "one instance forever" (serve) callers without either needing to know
    which shape it is.

    Single-flight (Red review, 2026-08-20): concurrent callers for one key
    share ONE in-flight attempt. The first caller (the leader) runs the load;
    followers wait on a per-key Condition and then read the leader's cached
    result -- so N simultaneous requests for a cold model cannot each spawn
    their own loader thread (resource waste, and the exact multi-live-model
    condition this repo's _MODEL_CACHE docstring warns destabilizes MPS).
    """
    if key is not None:
        with _lock:
            cached = _state.get(key)
        if cached is not None:
            at, is_success, value = cached
            if time.monotonic() - at < cooldown:
                if is_success:
                    return value  # type: ignore[return-value]
                raise value  # type: ignore[misc]
        # stale entry: drop it and become (or join) a fresh attempt
        with _lock:
            if key in _state:
                del _state[key]

        # Single-flight: claim leadership or wait for the leader.
        with _lock:
            cond = _in_flight.get(key)
        if cond is None:
            cond = threading.Condition(_lock)
            with _lock:
                _in_flight[key] = cond
            is_leader = True
        else:
            with cond:
                cond.wait()
            # leader finished (or timed out): the cached entry is now set
            # (failure) or the success was cached -- re-read and return.
            with _lock:
                cached = _state.get(key)
            if cached is not None:
                at, is_success, value = cached
                if is_success:
                    return value  # type: ignore[return-value]
                raise value  # type: ignore[misc]
            # leader timed out but did not cache (shouldn't happen -- timeout
            # caches a failure) -- fall through to a fresh attempt below.
            is_leader = False

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
        exc = ModelLoadTimeout(
            f"{description} did not load within {timeout:g}s. This looks like "
            f"a slow or unreachable network rather than a missing model -- "
            f"check connectivity. If this model has loaded successfully "
            f"before, HF_HUB_OFFLINE=1 will use the cached copy instantly "
            f"instead of re-checking the network for updates.")
        if key is not None:
            with _lock:
                _state[key] = (time.monotonic(), False, exc)
        if key is not None and is_leader:
            with _in_flight[key]:
                _in_flight[key].notify_all()
                del _in_flight[key]
        raise exc
    if error:
        exc = error[0]
        if key is not None:
            with _lock:
                _state[key] = (time.monotonic(), False, exc)
        if key is not None and is_leader:
            with _in_flight[key]:
                _in_flight[key].notify_all()
                del _in_flight[key]
        raise exc
    value = result[0]
    if key is not None:
        with _lock:
            _state[key] = (time.monotonic(), True, value)
    if key is not None and is_leader:
        with _in_flight[key]:
            _in_flight[key].notify_all()
            del _in_flight[key]
    return value
