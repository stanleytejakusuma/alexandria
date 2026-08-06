"""Offline tests for the phase-2 golden fact-recall evaluator (WORK-ORDER-phase2-fact-recall-eval.md).

All tests use ScriptedClient / FakeEngine — no network, no real models, no
reads from the private corpus. Synthetic ids only.
"""

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from alexandria.eval.synthesis_fact_recall import (
    GRADER_SYSTEM,
    FactRecallAgreement,
    FactRecallResult,
    FactVerdict,
    build_fact_recall_prompt,
    classify_miss,
    grade_fact_recall,
    grade_fact_recall_twice,
    parse_fact_recall_response,
    passes_gate,
    run_fact_recall_eval,
)
from alexandria.eval.synthesis_golden import LoadBearingFact, SynthesisClusterEntry
from alexandria.llm import LLMError, ScriptedClient


def _facts(*ids):
    return tuple(
        LoadBearingFact(fid, f"golden fact {fid}", (f"sources/{fid}",))
        for fid in ids
    )


def _resp(*rows):
    return json.dumps({"facts": list(rows)})


def _covered(fid, evidence="page states it."):
    return {"id": fid, "covered": True, "evidence": evidence}


def _uncovered(fid):
    return {"id": fid, "covered": False, "evidence": ""}


def _entry(cid="cluster-a", topic="topic", fact_ids=("f1",)):
    return SynthesisClusterEntry(
        cid, topic, (f"sources/{f}" for f in fact_ids),
        _facts(*fact_ids), "hand",
    )


# ---- strict parsing ----


def test_parse_valid_response_with_exact_ids_and_evidence():
    verdicts = parse_fact_recall_response(
        _resp(_covered("f1"), _uncovered("f2"), _covered("f3")),
        ("f1", "f2", "f3"),
    )
    assert [v.fact_id for v in verdicts] == ["f1", "f2", "f3"]
    assert verdicts[0].covered is True
    assert verdicts[0].evidence == "page states it."
    assert verdicts[1].covered is False
    assert verdicts[2].covered is True


def test_parse_rejects_missing_fact_id():
    with pytest.raises(LLMError, match="f2"):
        parse_fact_recall_response(_resp(_covered("f1"), _covered("f3")), ("f1", "f2", "f3"))


def test_parse_rejects_duplicate_fact_id():
    with pytest.raises(LLMError, match="duplicate"):
        parse_fact_recall_response(_resp(_covered("f1"), _covered("f1")), ("f1",))


def test_parse_rejects_unknown_fact_id():
    with pytest.raises(LLMError, match="f9"):
        parse_fact_recall_response(_resp(_covered("f9")), ("f1",))


def test_parse_rejects_non_bool_covered():
    with pytest.raises(LLMError):
        parse_fact_recall_response(json.dumps({"facts": [{"id": "f1", "covered": "yes", "evidence": ""}]}), ("f1",))


def test_parse_rejects_covered_without_evidence():
    with pytest.raises(LLMError, match="evidence"):
        parse_fact_recall_response(json.dumps({"facts": [{"id": "f1", "covered": True, "evidence": ""}]}), ("f1",))


def test_parse_rejects_not_covered_with_evidence():
    with pytest.raises(LLMError):
        parse_fact_recall_response(_resp({"id": "f1", "covered": False, "evidence": "span"}), ("f1",))


def test_parse_rejects_invalid_json():
    with pytest.raises(LLMError):
        parse_fact_recall_response("not json", ("f1",))


def test_parse_rejects_facts_not_a_list():
    with pytest.raises(LLMError, match="facts"):
        parse_fact_recall_response(json.dumps({"facts": {"f1": True}}), ("f1",))


# ---- single grader ----


def test_grade_fact_recall_computes_recall_and_records_model():
    llm = ScriptedClient([_resp(_covered("f1"), _uncovered("f2"))])
    result = grade_fact_recall(llm, "page text.", _facts("f1", "f2"), model="grader-x")
    assert isinstance(result, FactRecallResult)
    assert result.model == "grader-x"
    assert result.recall == 0.5
    assert result.errors == ()
    assert result.passed is False


def test_grade_fact_recall_defaults_model_from_client():
    class WithModel(ScriptedClient):
        model = "scripted-default"
    llm = WithModel([_resp(_covered("f1"))])
    result = grade_fact_recall(llm, "page text.", _facts("f1"))
    assert result.model == "scripted-default"


def test_grade_fact_recall_propagates_malformed_response():
    llm = ScriptedClient(["not json"])
    with pytest.raises(LLMError):
        grade_fact_recall(llm, "page text.", _facts("f1"))


# ---- disagreement semantics ----


def test_twice_agrees_covered():
    a = ScriptedClient([_resp(_covered("f1"))])
    b = ScriptedClient([_resp(_covered("f1", evidence="other span"))])
    r = grade_fact_recall_twice(a, b, "page text.", _facts("f1"))
    assert r.consensus_covered == ("f1",)
    assert r.contested_ids == ()
    assert r.consensus_recall == 1.0
    assert r.union_recall == 1.0


def test_twice_agrees_not_covered():
    a = ScriptedClient([_resp(_uncovered("f1"))])
    b = ScriptedClient([_resp(_uncovered("f1"))])
    r = grade_fact_recall_twice(a, b, "page text.", _facts("f1"))
    assert r.consensus_covered == ()
    assert r.consensus_recall == 0.0
    assert r.union_recall == 0.0


def test_twice_disagreement_marks_contested_and_lowers_consensus_recall():
    a = ScriptedClient([_resp(_covered("f1"), _covered("f2"))])
    b = ScriptedClient([_resp(_covered("f1"), _uncovered("f2"))])
    r = grade_fact_recall_twice(a, b, "page text.", _facts("f1", "f2"))
    assert r.contested_ids == ("f2",)
    assert r.consensus_covered == ("f1",)
    assert r.consensus_recall == 0.5
    assert r.union_recall == 1.0
    assert r.result_a.recall == 1.0
    assert r.result_b.recall == 0.5
    assert r.passed is False


def test_twice_preserves_both_individual_verdicts_and_evidence():
    a = ScriptedClient([_resp(_covered("f1", evidence="span from a"))])
    b = ScriptedClient([_resp(_uncovered("f1"))])
    r = grade_fact_recall_twice(a, b, "page text.", _facts("f1"))
    assert r.result_a.verdicts[0].evidence == "span from a"
    assert r.result_b.verdicts[0].covered is False


def test_twice_propagates_either_grader_error():
    a = ScriptedClient([_resp(_covered("f1"))])
    b = ScriptedClient(["not json"])
    with pytest.raises(LLMError):
        grade_fact_recall_twice(a, b, "page text.", _facts("f1"))


# ---- gate + miss taxonomy ----


def test_passes_gate_at_boundary():
    assert passes_gate(0.90) is True
    assert passes_gate(0.899) is False
    assert passes_gate(1.0) is True


def test_classify_miss_when_evidence_gathered_but_omitted():
    fact = LoadBearingFact("f1", "t", ("sources/f1",))
    assert classify_miss(fact, {"sources/f1"}) == "evidence_gathered_but_omitted"


def test_classify_miss_when_evidence_not_gathered():
    fact = LoadBearingFact("f1", "t", ("sources/f1",))
    assert classify_miss(fact, {"sources/other"}) == "evidence_not_gathered"


def test_build_prompt_embeds_every_fact_id_and_the_page():
    system, user = build_fact_recall_prompt("THE PAGE BODY", _facts("f1", "f2"))
    assert "THE PAGE BODY" in user
    assert "f1" in user and "f2" in user
    assert "evidence" in system
    assert "exactly once" in system.lower()


# ---- whole-eval orchestration ----


def test_run_fact_recall_eval_reports_consensus_and_contested_per_cluster(tmp_path):
    a_llm = ScriptedClient([
        _resp(_covered("f1"), _covered("f2")),
        _resp(_covered("f3"), _uncovered("f4"), _covered("f5")),
    ])
    b_llm = ScriptedClient([
        _resp(_covered("f1"), _covered("f2")),
        _resp(_covered("f3"), _uncovered("f4"), _uncovered("f5")),
    ])
    pages = tmp_path / "pages"
    gather = tmp_path / "gather"
    pages.mkdir(); gather.mkdir()
    (pages / "cluster-a.md").write_text("---\ntitle: a\n---\nbody a\n", encoding="utf-8")
    (pages / "cluster-b.md").write_text("body b", encoding="utf-8")
    (gather / "cluster-a.gather.json").write_text(json.dumps({"gathered_doc_ids": ["sources/f1", "sources/f2"]}))
    (gather / "cluster-b.gather.json").write_text(json.dumps({"gathered_doc_ids": ["sources/f3", "sources/f4", "sources/f5"]}))
    entries = [_entry("cluster-a", "topic a", ("f1", "f2")), _entry("cluster-b", "topic b", ("f3", "f4", "f5"))]

    report = run_fact_recall_eval(entries, pages, gather, a_llm, b_llm,
                                  model_a="m-a", model_b="m-b")

    assert report.total_facts == 5
    # cluster-a: 2 consensus-covered. cluster-b: f3 consensus-covered, f4 consensus-miss,
    # f5 contested (a says covered, b says not).
    assert report.pooled_consensus_recall == 0.6
    assert report.pooled_union_recall == 0.8
    assert report.contested_count == 1
    assert report.gate is False
    assert report.config["model_a"] == "m-a" and report.config["model_b"] == "m-b"
    assert report.git_sha
    cluster_b = report.clusters[1]
    assert cluster_b.contested_ids == ("f5",)
    assert cluster_b.miss_taxonomy == (
        {"fact_id": "f4", "fact_text": "golden fact f4", "classification": "evidence_gathered_but_omitted"},
    )


def test_run_fact_recall_eval_records_missing_page_as_error(tmp_path):
    pages = tmp_path / "pages"
    gather = tmp_path / "gather"
    pages.mkdir(); gather.mkdir()
    report = run_fact_recall_eval(
        [_entry("cluster-a", "topic", ("f1",))], pages, gather,
        ScriptedClient([]), ScriptedClient([]),
    )
    assert report.clusters[0].errors == ("page missing",)
    assert report.pooled_consensus_recall == 0.0
    assert report.gate is False


def test_run_fact_recall_eval_missing_gather_sidecar_falls_back_and_records(tmp_path):
    pages = tmp_path / "pages"
    gather = tmp_path / "gather"
    pages.mkdir(); gather.mkdir()
    (pages / "cluster-a.md").write_text("body a", encoding="utf-8")
    a = ScriptedClient([_resp(_uncovered("f1"))])
    b = ScriptedClient([_resp(_uncovered("f1"))])
    report = run_fact_recall_eval(
        [_entry("cluster-a", "topic", ("f1",))], pages, gather, a, b,
    )
    cluster = report.clusters[0]
    assert "gather sidecar missing" in cluster.errors
    assert cluster.miss_taxonomy[0]["classification"] == "evidence_not_gathered"


# ---- measurement driver (scripts/synthesize-golden-pages.py) ----


@dataclass(frozen=True)
class Result:
    chunk_id: str
    doc_id: str
    text: str


class FakeEngine:
    def __init__(self, results_by_query):
        self.results_by_query = results_by_query

    def search(self, query, *, k=None):
        value = self.results_by_query[query]
        if isinstance(value, Exception):
            raise value
        return value[:k] if k is not None else value


def _page(text, claims):
    return json.dumps({"page_text": text, "claims": claims})


def _load_script(name):
    import importlib.util
    filename = name.replace("_", "-")
    path = Path(__file__).resolve().parents[1] / "scripts" / f"{filename}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _audit_response(verdict):
    return json.dumps({"verdict": verdict, "reason": "scripted"})


def _coverage_response(label, code):
    return json.dumps({"label": label, "label_code": code,
                       "claim": "the remaining claim", "fact": "the skipped qualification",
                       "relation": "scripted"})


def _clients():
    return {
        "gather": ScriptedClient([json.dumps({"queries": []})]),
        "writer": ScriptedClient([_page("Page text.", [{
            "id": "c1", "text": "The fix shipped on Tuesday.",
            "citations": [{"doc_id": "sources/f1", "chunk_id": "sources/f1#1"}],
        }])]),
        "repair": ScriptedClient([]),
        "audit": ScriptedClient([_audit_response("supported")]),
        "coverage_a": ScriptedClient([]),
        "coverage_b": ScriptedClient([]),
    }


def test_driver_writes_page_and_gather_sidecar(tmp_path):
    driver = _load_script("synthesize_golden_pages")
    engine = FakeEngine({"topic": [Result("sources/f1#1", "sources/f1", "The fix shipped on Tuesday.")]})
    entries = [_entry("cluster-a", "topic", ("f1",))]
    out = tmp_path / "out"

    results = driver.synthesize_golden_pages(entries, out, engine, _clients(), seed_k=8)

    assert results[0]["emitted"] is True
    assert (out / "pages" / "cluster-a.md").exists()
    assert (out / "pages" / "cluster-a.skip-log.json").exists()
    sidecar = json.loads((out / "gather" / "cluster-a.gather.json").read_text())
    assert sidecar["gathered_doc_ids"] == ["sources/f1"]
    assert sidecar["native_passed"] is True


def test_driver_records_failed_pipeline_without_aborting_batch(tmp_path):
    driver = _load_script("synthesize_golden_pages")
    engine = FakeEngine({
        "topic a": [Result("sources/f1#1", "sources/f1", "The fix shipped on Tuesday.")],
        "topic b": RuntimeError("gather exploded"),
    })
    entries = [_entry("cluster-a", "topic a", ("f1",)), _entry("cluster-b", "topic b", ("f2",))]
    out = tmp_path / "out"

    results = driver.synthesize_golden_pages(entries, out, engine, _clients(), seed_k=8)

    assert results[0]["emitted"] is True
    assert (out / "pages" / "cluster-a.md").exists()
    assert results[1]["emitted"] is False
    assert "gather exploded" in results[1]["error"]
    assert (out / "gather" / "cluster-b.gather.json").exists()
