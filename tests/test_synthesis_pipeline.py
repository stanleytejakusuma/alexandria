import json
from dataclasses import dataclass

import pytest

from alexandria.llm import ScriptedClient
from alexandria.synthesis.gather import GatherResult, SourceChunk
from alexandria.synthesis.judge import ChunkAccountingError, judge_page
from alexandria.synthesis.pipeline import run_pipeline
from alexandria.synthesis.write import Claim, Citation, SynthesisPage, write_page


def _audit_response(verdict):
    return json.dumps({"verdict": verdict, "reason": "scripted"})


def _coverage_response(label, code):
    return json.dumps({
        "label": label,
        "label_code": code,
        "claim": "the remaining claim",
        "fact": "the skipped qualification",
        "relation": "scripted",
    })


def _page(text, claims):
    return json.dumps({"page_text": text, "claims": claims})


@dataclass(frozen=True)
class Result:
    chunk_id: str
    doc_id: str
    text: str
    score: float = 0.0


class FakeEngine:
    def __init__(self, results_by_query):
        self.results_by_query = results_by_query

    def search(self, query, *, k=None):
        return self.results_by_query[query][:k]


def _gathered(*chunks):
    return GatherResult(
        topic_query="topic",
        chunks=tuple(chunks),
        round_one=tuple(chunks),
        round_two=(),
        follow_up_queries=(),
        gap_response='{"queries": []}',
    )


def test_write_keeps_structured_claims_separate_from_page_text_and_adds_author():
    gathered = _gathered(SourceChunk("sources/a#1", "sources/a", "Source text.", 0.9))
    llm = ScriptedClient([_page("A short cited page.", [{
        "text": "A supported claim.",
        "citations": [{"doc_id": "sources/a", "chunk_id": "sources/a#1"}],
    }])])

    page = write_page(gathered, "topic", llm=llm, model="writer-model", prompt_version="v7")

    assert page.text == "A short cited page."
    assert page.author == "synthesis-sweep@writer-model@v7"
    assert page.claims == (
        Claim("claim-1", "A supported claim.", (Citation("sources/a", "sources/a#1"),)),
    )


def test_judge_raises_for_a_single_unaccounted_gathered_chunk_before_grading():
    gathered = _gathered(
        SourceChunk("sources/a#1", "sources/a", "A", 0.9),
        SourceChunk("sources/b#1", "sources/b", "B", 0.8),
        SourceChunk("sources/c#1", "sources/c", "C", 0.7),
    )
    page = SynthesisPage(
        topic_query="topic",
        text="Page",
        claims=(Claim("claim-1", "Claim", (Citation("sources/a", "sources/a#1"),)),),
        author="synthesis-sweep@writer@v1",
        skip_log=(
            # Chunk b is accounted for, chunk c is not. This must not become a warning.
            {"chunk_id": "sources/b#1", "doc_id": "sources/b", "reason": "out_of_scope:not_cited"},
        ),
    )

    with pytest.raises(ChunkAccountingError, match="sources/c#1"):
        judge_page(
            gathered,
            page,
            audit_llm=ScriptedClient([]),
            coverage_llm_a=ScriptedClient([]),
            coverage_llm_b=ScriptedClient([]),
        )


def test_passing_pipeline_emits_page_and_skip_log_with_attribution_seams(tmp_path):
    engine = FakeEngine({"topic": [Result("sources/a#1", "sources/a", "Supported evidence.")]})
    result = run_pipeline(
        engine,
        "topic",
        gather_llm=ScriptedClient([json.dumps({"queries": []})]),
        writer_llm=ScriptedClient([_page("Published page.", [{
            "text": "Supported claim.",
            "citations": [{"doc_id": "sources/a", "chunk_id": "sources/a#1"}],
        }],)]),
        repair_llm=ScriptedClient([]),
        audit_llm=ScriptedClient([_audit_response("supported")]),
        coverage_llm_a=ScriptedClient([]),
        coverage_llm_b=ScriptedClient([]),
        corpus_root=tmp_path,
        writer_model="writer-model",
        prompt_version="v2",
    )

    assert result.emitted is True
    assert result.repair.iterations == 0
    assert result.page_path == tmp_path / "wiki" / "topic.md"
    assert result.skip_log_path == tmp_path / "wiki" / "topic.skip-log.json"
    assert "author: synthesis-sweep@writer-model@v2" in result.page_path.read_text()
    assert json.loads(result.skip_log_path.read_text()) == {
        "author": "synthesis-sweep@writer-model@v2",
        "skips": [],
        "topic_query": "topic",
    }


def test_repair_deletion_is_logged_then_coverage_rejects_gutting_and_pipeline_emits_nothing(
    tmp_path,
):
    """The anti-gutting proof: deleting a fabricated claim cannot turn a page green.

    The first repair removes the bad claim. The next judge pass re-runs coverage and
    finds the now-skipped source load-bearing, so the bounded loop exhausts without
    writing either output file.
    """
    engine = FakeEngine({"topic": [
        Result("sources/bad#1", "sources/bad", "Bad-claim evidence."),
        Result("sources/keep#1", "sources/keep", "Supported claim evidence."),
        Result("sources/qualifier#1", "sources/qualifier", "Load-bearing qualifier."),
    ]})
    initial = _page("Initial page.", [
        {"id": "bad", "text": "Fabricated claim.", "citations": [{"doc_id": "sources/bad"}]},
        {"id": "keep", "text": "Remaining claim.", "citations": [{"doc_id": "sources/keep"}]},
    ])
    repaired = _page("Gutted page.", [
        {"id": "keep", "text": "Remaining claim.", "citations": [{"doc_id": "sources/keep"}]},
    ])

    coverage_llm_a = ScriptedClient([
        _coverage_response("SS", "SS:tangential"),
        _coverage_response("LB", "LB:qualification:scope"),
        _coverage_response("LB", "LB:qualification:scope"),
    ])
    coverage_llm_b = ScriptedClient([
        _coverage_response("SS", "SS:tangential"),
        _coverage_response("LB", "LB:qualification:scope"),
        _coverage_response("LB", "LB:qualification:scope"),
    ])
    result = run_pipeline(
        engine,
        "topic",
        gather_llm=ScriptedClient([json.dumps({"queries": []})]),
        writer_llm=ScriptedClient([initial]),
        repair_llm=ScriptedClient([repaired, repaired]),
        audit_llm=ScriptedClient([
            _audit_response("fabricated"), _audit_response("supported"),
            _audit_response("supported"), _audit_response("supported"),
        ]),
        coverage_llm_a=coverage_llm_a,
        coverage_llm_b=coverage_llm_b,
        corpus_root=tmp_path,
    )

    assert result.emitted is False
    assert result.repair.iterations == 2
    assert result.repair.verdict.coverage_passed is False
    assert any(entry["chunk_id"] == "sources/bad#1" for entry in result.repair.page.skip_log)
    assert len(coverage_llm_a.calls) == 3
    assert len(coverage_llm_b.calls) == 3
    assert not list((tmp_path / "wiki").glob("*.md"))
    assert not list((tmp_path / "wiki").glob("*.skip-log.json"))


def test_repair_survives_transient_empty_writer_json(tmp_path):
    """Regression for the 2026-08-07 measurement kill: opencode went 3/3
    attempts dead because repair iteration 2 got an EMPTY writer response
    (JSONDecodeError char 0) and the loop `break`ed -- silent emit failure.
    A transient empty completion must be retried, not treated as terminal."""
    engine = FakeEngine({"topic": [
        Result("sources/bad#1", "sources/bad", "Bad-claim evidence."),
        Result("sources/keep#1", "sources/keep", "Supported claim evidence."),
    ]})
    initial = _page("Initial page.", [
        {"id": "bad", "text": "Fabricated claim.", "citations": [{"doc_id": "sources/bad"}]},
        {"id": "keep", "text": "Remaining claim.", "citations": [{"doc_id": "sources/keep"}]},
    ])
    repaired = _page("Repaired page.", [
        {"id": "keep", "text": "Remaining claim.", "citations": [{"doc_id": "sources/keep"}]},
    ])

    # repair_llm: first completion is EMPTY (the transient), the retry succeeds.
    # audit: fail on the fabricated claim, then pass the repaired page (2 passes:
    # iteration 1 repair + final judge after the repaired page is accepted).
    result = run_pipeline(
        engine,
        "topic",
        gather_llm=ScriptedClient([json.dumps({"queries": []})]),
        writer_llm=ScriptedClient([initial]),
        repair_llm=ScriptedClient(["", repaired]),
        audit_llm=ScriptedClient([
            _audit_response("fabricated"), _audit_response("supported"),
            _audit_response("supported"),
        ]),
        coverage_llm_a=ScriptedClient([_coverage_response("SS", "SS:tangential"),
                                       _coverage_response("SS", "SS:tangential")]),
        coverage_llm_b=ScriptedClient([_coverage_response("SS", "SS:tangential"),
                                       _coverage_response("SS", "SS:tangential")]),
        corpus_root=tmp_path,
    )

    assert result.emitted is True
    assert result.repair.iterations == 1
    assert result.repair.errors == ()
    assert result.repair.transient_errors == (
        "repair iteration 1 attempt 1 failed: synthesis writer returned "
        "invalid page JSON: JSONDecodeError: Expecting value: "
        "line 1 column 1 (char 0)",
    )
    assert (tmp_path / "wiki" / "topic.md").exists()


def test_writer_prompt_demands_load_bearing_coverage():
    """Regression for the measured writer layer (~72% consensus on emitted
    pages): WRITER_SYSTEM must direct the writer to state load-bearing
    propositions from the sources it cites, not merely avoid fabrication."""
    from alexandria.synthesis.write import WRITER_SYSTEM
    text = WRITER_SYSTEM.lower()
    assert "load-bearing" in text
    assert "omit" in text


def test_repair_prompt_demands_support_or_removal_not_regeneration():
    """Regression for the magpie stuck-claim failure: REPAIR_SYSTEM must force a
    per-failed-claim decision (cite gathered support or remove the claim) and
    forbid regenerating failed claims with new wording."""
    from alexandria.synthesis.repair import REPAIR_SYSTEM
    text = REPAIR_SYSTEM.lower()
    assert "remove" in text
    assert "support" in text
    assert "regenerat" in text
