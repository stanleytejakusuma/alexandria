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
