import json
from dataclasses import dataclass

from alexandria.llm import ScriptedClient
from alexandria.synthesis.gather import gather


@dataclass(frozen=True)
class Result:
    chunk_id: str
    doc_id: str
    text: str
    score: float = 0.0


class FakeEngine:
    def __init__(self, results_by_query):
        self.results_by_query = results_by_query
        self.calls = []

    def search(self, query, *, k=None):
        self.calls.append((query, k))
        return self.results_by_query[query][:k]


def test_gather_runs_exactly_two_retrieval_rounds_and_keeps_raw_follow_up_queries():
    engine = FakeEngine({
        "topic": [
            Result("sources/current#1", "sources/current", "The current decision."),
            Result("sources/shared#1", "sources/shared", "Shared evidence."),
        ],
        "earlier assertion": [
            Result("sources/earlier#1", "sources/earlier", "The superseded decision."),
            Result("sources/shared#2", "sources/shared", "Duplicate document."),
        ],
    })
    llm = ScriptedClient([json.dumps({"queries": ["earlier assertion"]})])

    result = gather(engine, "topic", llm=llm, seed_k=2)

    assert engine.calls == [("topic", 2), ("earlier assertion", 2)]
    assert [chunk.doc_id for chunk in result.round_one] == ["sources/current", "sources/shared"]
    assert [chunk.doc_id for chunk in result.round_two] == ["sources/earlier", "sources/shared"]
    assert [chunk.doc_id for chunk in result.chunks] == [
        "sources/current", "sources/shared", "sources/earlier",
    ]
    assert result.follow_up_queries == ("earlier assertion",)
    assert "earlier" in llm.calls[0][0].lower()
    assert "superseded" in llm.calls[0][0].lower()


def test_gather_does_not_retrieve_a_third_round_when_no_follow_up_is_needed():
    engine = FakeEngine({"topic": [Result("sources/a#1", "sources/a", "Evidence.")]})
    llm = ScriptedClient([json.dumps({"queries": []})])

    result = gather(engine, "topic", llm=llm)

    assert engine.calls == [("topic", 8)]
    assert result.round_two == ()
    assert [chunk.doc_id for chunk in result.chunks] == ["sources/a"]


def test_gather_includes_seed_chunks_in_pool():
    """Round-2 fix: cluster member docs seed the gather pool (real corpus
    structure, not golden leakage). Seed chunks must be in the merged pool,
    deduped by doc_id against retrieval results."""
    from alexandria.synthesis.gather import SourceChunk, gather

    class Engine:
        def search(self, query, k=8):
            return [type("R", (), {"doc_id": "sources/retrieved", "chunk_id": "sources/retrieved#1",
                                   "text": "retrieved text", "score": 1.0})()]

    class GapLLM:
        def complete(self, system, user):
            return '{"queries": []}'

    seeds = [SourceChunk("sources/seed#0", "sources/seed", "seed text"),
             SourceChunk("sources/seed#1", "sources/seed", "seed text 2")]
    result = gather(Engine(), "topic", llm=GapLLM(), seed_k=8, seed_chunks=seeds)
    doc_ids = [c.doc_id for c in result.chunks]
    assert "sources/seed" in doc_ids
    assert "sources/retrieved" in doc_ids
    assert doc_ids.count("sources/seed") == 1  # deduped against retrieval
    assert len(result.chunks) == 2


def test_gather_caps_follow_up_queries_at_the_limit():
    engine = FakeEngine({
        "topic": [Result("sources/a#1", "sources/a", "A.")],
        "q1": [Result("sources/b#1", "sources/b", "B.")],
        "q2": [Result("sources/c#1", "sources/c", "C.")],
        "q3": [Result("sources/d#1", "sources/d", "D.")],
    })
    llm = ScriptedClient([json.dumps({"queries": ["q1", "q2", "q3"]})])

    result = gather(engine, "topic", llm=llm, max_follow_up_queries=2)

    # topic + exactly q1 + q2 searched; q3 is capped even though the gap
    # detector returned it (each follow-up is another full search).
    assert [q for q, _ in engine.calls] == ["topic", "q1", "q2"]
    assert result.follow_up_queries == ("q1", "q2")
    assert [c.doc_id for c in result.round_two] == ["sources/b", "sources/c"]


def test_gather_default_caps_follow_ups_at_two():
    engine = FakeEngine({
        "topic": [Result("sources/a#1", "sources/a", "A.")],
        "q1": [Result("sources/b#1", "sources/b", "B.")],
        "q2": [Result("sources/c#1", "sources/c", "C.")],
        "q3": [Result("sources/d#1", "sources/d", "D.")],
    })
    llm = ScriptedClient([json.dumps({"queries": ["q1", "q2", "q3"]})])

    result = gather(engine, "topic", llm=llm)  # default max_follow_up_queries=2

    assert [q for q, _ in engine.calls] == ["topic", "q1", "q2"]
    assert result.follow_up_queries == ("q1", "q2")


def test_gather_unbounded_when_max_set_high():
    engine = FakeEngine({
        "topic": [Result("sources/a#1", "sources/a", "A.")],
        "q1": [Result("sources/b#1", "sources/b", "B.")],
        "q2": [Result("sources/c#1", "sources/c", "C.")],
        "q3": [Result("sources/d#1", "sources/d", "D.")],
    })
    llm = ScriptedClient([json.dumps({"queries": ["q1", "q2", "q3"]})])

    result = gather(engine, "topic", llm=llm, max_follow_up_queries=10)

    assert [q for q, _ in engine.calls] == ["topic", "q1", "q2", "q3"]
    assert result.follow_up_queries == ("q1", "q2", "q3")
