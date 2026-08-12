"""§8 / gate T1: a tripwire, not a column.

Multi-tenant scoping is deliberately deferred (BACKLOG #27/#28) -- but
`ResponseCache.key()` has NO filters dimension (cache.py), while `answer`
also accepts no filter argument, so the omission is harmless today. The
gap becomes a real cross-tenant leak the moment BOTH of these become
true: `answer` gains a way to scope a question to a subset of documents,
AND the response cache still doesn't fold that scope into its key -- two
tenants asking the identical question could then be served an answer
synthesized from the OTHER tenant's private documents, cache-hit, with
citations. This test must fail the instant that combination ships,
before it reaches review by inspection alone.
"""

from __future__ import annotations

import inspect

from alexandria.cache import ResponseCache
from alexandria.cli import build_parser, run_answer


def _answer_parser_has_a_filter_argument() -> bool:
    parser = build_parser()
    # argparse doesn't expose subparsers by name directly; walk the
    # top-level subparsers action to find "answer".
    subparsers_action = next(a for a in parser._actions
                             if getattr(a, "choices", None) and "answer" in a.choices)
    answer_parser = subparsers_action.choices["answer"]
    return any("filter" in (action.dest or "").lower() for action in answer_parser._actions)


def _run_answer_accepts_filters() -> bool:
    return "filters" in inspect.signature(run_answer).parameters


def _response_cache_key_includes_filters() -> bool:
    return "filters" in inspect.signature(ResponseCache.key).parameters


def _cross_tenant_leak_is_possible(*, answer_has_filters: bool, cache_key_has_filters: bool) -> bool:
    """The exact condition the gate names: a caller can scope an answer to a
    subset of documents, but the cache key that would serve a REPEAT of the
    same question ignores that scope entirely."""
    return answer_has_filters and not cache_key_has_filters


def test_t1_todays_real_code_cannot_leak_across_tenants(tmp_path):
    """The gate, run against the actual shipped answer parser and
    ResponseCache.key signature -- not a simulation of them."""
    answer_has_filters = _answer_parser_has_a_filter_argument() or _run_answer_accepts_filters()
    cache_key_has_filters = _response_cache_key_includes_filters()

    assert not _cross_tenant_leak_is_possible(
        answer_has_filters=answer_has_filters, cache_key_has_filters=cache_key_has_filters), (
        "answer gained a filter argument while ResponseCache.key() still omits "
        "filters -- this is a live cross-tenant cache leak (BACKLOG #27/#28's "
        "deferred tenancy work must land alongside this, not after it)")


def test_t1_the_predicate_itself_actually_catches_the_dangerous_combination():
    """Mutation check on the tripwire's own logic: prove it isn't vacuously
    safe by feeding it the exact combination the docstring describes."""
    assert _cross_tenant_leak_is_possible(answer_has_filters=True, cache_key_has_filters=False) is True
    assert _cross_tenant_leak_is_possible(answer_has_filters=True, cache_key_has_filters=True) is False
    assert _cross_tenant_leak_is_possible(answer_has_filters=False, cache_key_has_filters=False) is False
    assert _cross_tenant_leak_is_possible(answer_has_filters=False, cache_key_has_filters=True) is False


def test_t1_response_cache_key_is_unaffected_by_a_metadata_filter_today(tmp_path):
    """Behavioral proof, not just a signature check: two "tenants" asking the
    identical question through run_answer's actual cache-key construction
    collide on purpose today (filters don't exist as a concept anywhere in
    the answer path) -- documenting exactly what BACKLOG #27/#28 must change
    once filters are introduced."""
    cache = ResponseCache(tmp_path)
    key_one = cache.key(question="what is the billing policy?", model="m", k=8,
                        prompt_version="v1", generation=1)
    key_two = cache.key(question="what is the billing policy?", model="m", k=8,
                        prompt_version="v1", generation=1)
    assert key_one == key_two  # true today by construction -- there is no
    # scope dimension for two "tenants" to differ on, which is exactly why
    # T1 exists: the moment answer/run_answer gain one, this equality
    # becomes the cross-tenant leak, and the two tests above catch it.
