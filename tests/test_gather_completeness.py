"""Judge 3 -- gather-completeness for CONTRA-SCAN, per SPEC-phase2-eval.md.

Structurally a retrieval-recall measurement, not an LLM judgment call: CONTRA-SCAN
cannot flag a contradiction its gather step never retrieved, so this measures whether
running the real search engine on a contradiction pair's query surfaces BOTH members
-- not ANY-OF (the retrieval golden set's semantics), because either member could end
up as "the one already cited" in a real synthesis pass, so the query must be able to
surface the other one regardless of which side that turns out to be.
"""

from types import SimpleNamespace

from alexandria.eval.contradiction_golden import ContradictionPairEntry
from alexandria.eval.gather_completeness import run_gather_completeness


class Result:
    def __init__(self, doc_id: str):
        self.doc_id = doc_id


class Store:
    def count(self) -> int:
        return 42


class FakeEngine:
    def __init__(self, results_by_query):
        self.results_by_query = results_by_query
        self.embedder = SimpleNamespace(name="hash-24")
        self.reranker = SimpleNamespace(model_name="fake-reranker", half_precision=True)
        self.config = SimpleNamespace(prefetch=20, top_k=5, rrf_k=60, wiki_boost=1.25)
        self.store = Store()

    def search(self, query, *, k=None):
        value = self.results_by_query[query]
        if isinstance(value, Exception):
            raise value
        return [Result(doc_id) for doc_id in value][: k if k else None]


def _pair(**overrides):
    base = dict(id="p1", query="q", claim_a="sources/a", claim_b="sources/b",
               relationship="contradicts", note=None, provenance="hand")
    base.update(overrides)
    return ContradictionPairEntry(**base)


def test_both_members_found_counts_as_covered():
    engine = FakeEngine({"q": ["sources/other", "sources/a", "sources/b"]})
    report = run_gather_completeness(engine, [_pair()], k_override=5)
    result = report.results[0]
    assert result.claim_a_found is True
    assert result.claim_b_found is True
    assert result.both_found is True
    assert report.summary.pair_recall == 1.0


def test_only_one_member_found_does_not_count_as_covered():
    """This is the whole point -- ANY-OF semantics (used by the retrieval golden set)
    would wrongly call this a hit. Gather-completeness needs BOTH sides findable by
    one query, since either member could be the one already cited in a real page."""
    engine = FakeEngine({"q": ["sources/a", "sources/other"]})
    report = run_gather_completeness(engine, [_pair()], k_override=5)
    result = report.results[0]
    assert result.claim_a_found is True
    assert result.claim_b_found is False
    assert result.both_found is False
    assert report.summary.pair_recall == 0.0


def test_neither_member_found():
    engine = FakeEngine({"q": ["sources/noise"]})
    report = run_gather_completeness(engine, [_pair()], k_override=5)
    result = report.results[0]
    assert result.both_found is False


def test_pair_recall_averages_across_multiple_pairs():
    engine = FakeEngine({
        "q1": ["sources/a1", "sources/b1"],
        "q2": ["sources/a2"],
    })
    entries = [_pair(id="p1", query="q1", claim_a="sources/a1", claim_b="sources/b1"),
              _pair(id="p2", query="q2", claim_a="sources/a2", claim_b="sources/b2")]
    report = run_gather_completeness(engine, entries, k_override=5)
    assert report.summary.pair_recall == 0.5
    assert report.summary.n == 2


def test_search_failure_degrades_loudly_never_a_silent_pass():
    """Same discipline as retrieval's run_eval: a query error must show up as a
    recorded, failed row -- never silently counted toward recall as if it passed."""
    engine = FakeEngine({"q": RuntimeError("index unavailable")})
    report = run_gather_completeness(engine, [_pair()], k_override=5)
    result = report.results[0]
    assert result.both_found is False
    assert result.error is not None
    assert "index unavailable" in result.error
    assert report.summary.pair_recall == 0.0
    assert report.summary.error_ids == ["p1"]


def test_respects_k_from_pair_or_override():
    engine = FakeEngine({"q": ["sources/noise1", "sources/noise2", "sources/a", "sources/b"]})
    # only top-2 requested -- both real members sit outside that window
    report = run_gather_completeness(engine, [_pair()], k_override=2)
    assert report.results[0].both_found is False


def test_retrieved_ids_are_recorded_for_auditability():
    engine = FakeEngine({"q": ["sources/a", "sources/b"]})
    report = run_gather_completeness(engine, [_pair()], k_override=5)
    assert report.results[0].retrieved_ids == ["sources/a", "sources/b"]


def test_gate_at_90_percent():
    from alexandria.eval.gather_completeness import passes_gate
    assert passes_gate(pair_recall=0.90) is True
    assert passes_gate(pair_recall=0.899) is False
    assert passes_gate(pair_recall=1.0) is True
