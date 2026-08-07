"""Offline tests for the phase-2 golden fact-recall evaluator (WORK-ORDER-phase2-fact-recall-eval.md).

Includes the Red-review hardening pass (2026-08-05): evidence spans must be
substrings of the rendered page body, duplicate JSON keys are rejected,
run-status separation (graded / pipeline_failure / measurement_invalid), and
the driver catches only expected pipeline exceptions.

All tests use ScriptedClient / FakeEngine — no network, no real models, no
reads from the private corpus. Synthetic ids only.
"""

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from alexandria.eval.synthesis_fact_recall import (
    GRADER_SYSTEM,
    VERDICT_FINAL_FAIL,
    VERDICT_INVALID,
    VERDICT_PASS,
    VERDICT_PROVISIONAL_FAIL,
    _verdict,
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

PAGE = "The fix shipped on Tuesday. The root cause was a null peak."
EVIDENCE = "The fix shipped on Tuesday."


def _facts(*ids):
    return tuple(
        LoadBearingFact(fid, f"golden fact {fid}", (f"sources/{fid}",))
        for fid in ids
    )


def _resp(*rows):
    return json.dumps({"facts": list(rows)})


def _covered(fid, evidence=EVIDENCE):
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
        ("f1", "f2", "f3"), page_body=PAGE,
    )
    assert [v.fact_id for v in verdicts] == ["f1", "f2", "f3"]
    assert verdicts[0].covered is True
    assert verdicts[0].evidence == EVIDENCE
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
        parse_fact_recall_response(_resp({"id": "f1", "covered": False, "evidence": EVIDENCE}), ("f1",))


def test_parse_rejects_invalid_json():
    with pytest.raises(LLMError):
        parse_fact_recall_response("not json", ("f1",))


def test_parse_rejects_facts_not_a_list():
    with pytest.raises(LLMError, match="facts"):
        parse_fact_recall_response(json.dumps({"facts": {"f1": True}}), ("f1",))


def test_parse_rejects_duplicate_json_keys():
    raw = '{"facts":[{"id":"f1","covered":true,"evidence":"' + EVIDENCE + '"}],' \
          '"facts":[{"id":"f1","covered":false,"evidence":""}]}'
    with pytest.raises(LLMError, match="duplicate JSON key"):
        parse_fact_recall_response(raw, ("f1",))


def test_parse_flags_evidence_not_in_page_instead_of_raising():
    """fact-recall-v2 semantics: a covered verdict whose evidence is not a
    verbatim page span is FLAGGED per-fact (joins adjudication), never a
    cluster-wide invalidation -- diffuse page statements make verbatim
    quoting impossible for some genuinely-covered facts."""
    verdicts = parse_fact_recall_response(_resp(_covered("f1", evidence="not in the page")),
                                          ("f1",), page_body=PAGE)
    assert verdicts[0].covered is True
    assert verdicts[0].error == "evidence_not_verbatim"


def test_parse_accepts_whitespace_normalized_evidence():
    raw = json.dumps({"facts": [{"id": "f1", "covered": True, "evidence": "the   fix   shipped on tuesday"}]})
    verdicts = parse_fact_recall_response(raw, ("f1",), page_body="the fix shipped on tuesday")
    assert verdicts[0].covered is True


# ---- single grader ----


def test_grade_fact_recall_computes_recall_and_records_model():
    llm = ScriptedClient([_resp(_covered("f1"), _uncovered("f2"))])
    result = grade_fact_recall(llm, PAGE, _facts("f1", "f2"), model="grader-x")
    assert result.recall == 0.5
    assert result.errors == ()
    assert result.passed is False


def test_grade_fact_recall_retains_raw_response_for_audit():
    raw = _resp(_covered("f1"))
    llm = ScriptedClient([raw])
    result = grade_fact_recall(llm, PAGE, _facts("f1"))
    assert result.raw == raw


def test_grade_fact_recall_defaults_model_from_client():
    class WithModel(ScriptedClient):
        model = "scripted-default"
    llm = WithModel([_resp(_covered("f1"))])
    result = grade_fact_recall(llm, PAGE, _facts("f1"))
    assert result.model == "scripted-default"


def test_grade_fact_recall_propagates_malformed_response():
    llm = ScriptedClient(["not json"])
    with pytest.raises(LLMError):
        grade_fact_recall(llm, PAGE, _facts("f1"))


# ---- disagreement semantics ----


def test_twice_agrees_covered():
    a = ScriptedClient([_resp(_covered("f1"))])
    b = ScriptedClient([_resp(_covered("f1", evidence="The root cause was a null peak."))])
    r = grade_fact_recall_twice(a, b, PAGE, _facts("f1"))
    assert r.consensus_covered == ("f1",)
    assert r.contested_ids == ()
    assert r.consensus_recall == 1.0
    assert r.union_recall == 1.0


def test_twice_agrees_not_covered():
    a = ScriptedClient([_resp(_uncovered("f1"))])
    b = ScriptedClient([_resp(_uncovered("f1"))])
    r = grade_fact_recall_twice(a, b, PAGE, _facts("f1"))
    assert r.consensus_covered == ()
    assert r.consensus_recall == 0.0
    assert r.union_recall == 0.0


def test_twice_disagreement_marks_contested_and_lowers_consensus_recall():
    a = ScriptedClient([_resp(_covered("f1"), _covered("f2"))])
    b = ScriptedClient([_resp(_covered("f1"), _uncovered("f2"))])
    r = grade_fact_recall_twice(a, b, PAGE, _facts("f1", "f2"))
    assert r.contested_ids == ("f2",)
    assert r.consensus_covered == ("f1",)
    assert r.consensus_recall == 0.5
    assert r.union_recall == 1.0
    assert r.result_a.recall == 1.0
    assert r.result_b.recall == 0.5
    assert r.passed is False


def test_twice_preserves_both_individual_verdicts_and_evidence():
    a = ScriptedClient([_resp(_covered("f1", evidence="The root cause was a null peak."))])
    b = ScriptedClient([_resp(_uncovered("f1"))])
    r = grade_fact_recall_twice(a, b, PAGE, _facts("f1"))
    assert r.result_a.verdicts[0].evidence == "The root cause was a null peak."
    assert r.result_b.verdicts[0].covered is False


def test_twice_propagates_either_grader_error():
    a = ScriptedClient([_resp(_covered("f1"))])
    b = ScriptedClient(["not json"])
    with pytest.raises(LLMError):
        grade_fact_recall_twice(a, b, PAGE, _facts("f1"))


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


def _write_fixture(tmp_path, cluster_id, page_text, facts, emitted=True):
    pages = tmp_path / "pages"
    gather = tmp_path / "gather"
    pages.mkdir(exist_ok=True)
    gather.mkdir(exist_ok=True)
    (pages / f"{cluster_id}.md").write_text(f"---\ntitle: {cluster_id}\n---\n{page_text}\n", encoding="utf-8")
    (gather / f"{cluster_id}.gather.json").write_text(json.dumps({
        "emitted": emitted,
        "gathered_doc_ids": [f"sources/{f}" for f in facts],
    }))
    return pages, gather


def test_run_fact_recall_eval_reports_consensus_and_contested_per_cluster(tmp_path):
    a_llm = ScriptedClient([
        _resp(_covered("f1"), _covered("f2")),
        _resp(_covered("f3"), _uncovered("f4"), _covered("f5")),
    ])
    b_llm = ScriptedClient([
        _resp(_covered("f1"), _covered("f2")),
        _resp(_covered("f3"), _uncovered("f4"), _uncovered("f5")),
    ])
    pages, gather = _write_fixture(tmp_path, "cluster-a", PAGE, ("f1", "f2"))
    _write_fixture(tmp_path, "cluster-b", PAGE, ("f3", "f4", "f5"))
    entries = [_entry("cluster-a", "topic a", ("f1", "f2")), _entry("cluster-b", "topic b", ("f3", "f4", "f5"))]

    report = run_fact_recall_eval(entries, pages, gather, a_llm, b_llm,
                                  model_a="m-a", model_b="m-b")

    assert report.total_facts == 5
    assert report.scored_fact_count == 5
    # cluster-a: 2 consensus-covered. cluster-b: f3 consensus-covered, f4 consensus-miss,
    # f5 contested (a says covered, b says not).
    assert report.consensus_count == 3
    assert report.pooled_consensus_recall == pytest.approx(0.6)
    assert report.pooled_union_recall == pytest.approx(0.8)
    assert report.macro_consensus_recall == pytest.approx((1.0 + 1 / 3) / 2)
    assert report.contested_count == 1
    assert report.invalid_cluster_ids == ()
    assert report.verdict == VERDICT_PROVISIONAL_FAIL   # unresolved disagreement
    assert report.config["model_a"] == "m-a" and report.config["model_b"] == "m-b"
    assert report.git_sha
    cluster_b = report.clusters[1]
    assert cluster_b.status == "graded"
    assert cluster_b.contested_ids == ("f5",)
    assert cluster_b.miss_taxonomy == (
        {"fact_id": "f4", "fact_text": "golden fact f4",
         "classification": "evidence_gathered_but_omitted", "provisional": True},
    )


def test_run_fact_recall_eval_missing_page_is_measurement_invalid(tmp_path):
    pages = tmp_path / "pages"
    gather = tmp_path / "gather"
    pages.mkdir(); gather.mkdir()
    report = run_fact_recall_eval(
        [_entry("cluster-a", "topic", ("f1",))], pages, gather,
        ScriptedClient([]), ScriptedClient([]),
    )
    cluster = report.clusters[0]
    assert cluster.status == "measurement_invalid"
    assert cluster.errors == ("gather sidecar missing",)
    assert report.invalid_cluster_ids == ("cluster-a",)
    assert report.scored_fact_count == 0
    assert report.verdict == VERDICT_INVALID


def test_run_fact_recall_eval_missing_gather_sidecar_is_measurement_invalid(tmp_path):
    pages = tmp_path / "pages"
    gather = tmp_path / "gather"
    pages.mkdir(); gather.mkdir()
    (pages / "cluster-a.md").write_text(PAGE, encoding="utf-8")
    a = ScriptedClient([_resp(_uncovered("f1"))])
    b = ScriptedClient([_resp(_uncovered("f1"))])
    report = run_fact_recall_eval(
        [_entry("cluster-a", "topic", ("f1",))], pages, gather, a, b,
    )
    cluster = report.clusters[0]
    assert cluster.status == "measurement_invalid"
    assert "gather sidecar missing" in cluster.errors
    assert cluster.miss_taxonomy == ()
    assert report.verdict == VERDICT_INVALID


def test_run_fact_recall_eval_pipeline_failure_facts_count_as_misses(tmp_path):
    # Sidecar records emitted=false and no page exists: an attributable pipeline
    # failure whose facts stay in the denominator as misses (fail closed).
    pages = tmp_path / "pages"
    gather = tmp_path / "gather"
    pages.mkdir(); gather.mkdir()
    (gather / "cluster-a.gather.json").write_text(json.dumps({
        "emitted": False, "gathered_doc_ids": [],
        "error": "ChunkAccountingError: unaccounted chunk",
    }))
    report = run_fact_recall_eval(
        [_entry("cluster-a", "topic", ("f1", "f2"))], pages, gather,
        ScriptedClient([]), ScriptedClient([]),
    )
    cluster = report.clusters[0]
    assert cluster.status == "pipeline_failure"
    assert report.pipeline_failure_cluster_ids == ("cluster-a",)
    assert report.scored_fact_count == 2
    assert report.pooled_consensus_recall == 0.0
    assert report.verdict == VERDICT_FINAL_FAIL


def test_run_fact_recall_eval_gate_forced_fail_when_any_cluster_invalid(tmp_path):
    a_llm = ScriptedClient([
        _resp(_covered("f1")),
    ])
    b_llm = ScriptedClient([
        _resp(_covered("f1")),
    ])
    pages, gather = _write_fixture(tmp_path, "cluster-a", PAGE, ("f1",))
    # cluster-b: page exists, sidecar missing -> measurement_invalid
    (pages / "cluster-b.md").write_text(PAGE, encoding="utf-8")
    report = run_fact_recall_eval(
        [_entry("cluster-a", "topic a", ("f1",)), _entry("cluster-b", "topic b", ("f2",))],
        pages, gather, a_llm, b_llm,
    )
    assert report.clusters[0].status == "graded"
    assert report.pooled_consensus_recall == 1.0
    assert report.verdict == VERDICT_INVALID   # fail closed: one invalid cluster
    assert report.invalid_cluster_ids == ("cluster-b",)


def test_verdict_boundaries():
    assert _verdict(0.95, 0, False) == VERDICT_PASS
    assert _verdict(0.90, 0, False) == VERDICT_PASS
    assert _verdict(0.89, 0, False) == VERDICT_PROVISIONAL_FAIL   # in band
    assert _verdict(0.85, 0, False) == VERDICT_PROVISIONAL_FAIL   # band edge
    assert _verdict(0.84, 0, False) == VERDICT_FINAL_FAIL
    assert _verdict(0.90, 1, False) == VERDICT_PASS   # contested already counts as a miss; gate met
    assert _verdict(0.0, 0, True) == VERDICT_INVALID
    assert _verdict(0.99, 0, True) == VERDICT_INVALID             # invalid is never FAIL


def test_adjudication_resolves_contested_and_recomputes_verdict(tmp_path):
    a_llm = ScriptedClient([
        _resp(_covered("f1"), _covered("f2")),
        _resp(_covered("f3"), _uncovered("f4"), _uncovered("f5")),
    ])
    b_llm = ScriptedClient([
        _resp(_covered("f1"), _covered("f2")),
        _resp(_covered("f3"), _uncovered("f4"), _covered("f5")),
    ])
    pages, gather = _write_fixture(tmp_path, "cluster-a", PAGE, ("f1", "f2"))
    _write_fixture(tmp_path, "cluster-b", PAGE, ("f3", "f4", "f5"))
    entries = [_entry("cluster-a", "topic a", ("f1", "f2")), _entry("cluster-b", "topic b", ("f3", "f4", "f5"))]
    # f4: consensus miss -> adjudicated covered. f5: contested -> adjudicated covered.
    adjudications = {"cluster-b::f4": True, "cluster-b::f5": True}

    report = run_fact_recall_eval(entries, pages, gather, a_llm, b_llm,
                                  adjudications=adjudications)

    assert report.consensus_count == 5
    assert report.pooled_consensus_recall == 1.0
    assert report.contested_count == 0
    assert report.adjudicated_count == 2
    assert report.verdict == VERDICT_PASS
    cluster_b = report.clusters[1]
    assert cluster_b.miss_taxonomy == ()
    # Raw grader verdicts are retained for audit even where adjudication overrode them.
    assert cluster_b.agreement.result_a.verdicts[2].covered is False  # f5, grader a
    assert cluster_b.agreement.result_b.verdicts[2].covered is True   # f5, grader b


def test_adjudication_false_records_miss_and_affects_verdict(tmp_path):
    a_llm = ScriptedClient([_resp(_covered("f1"))])
    b_llm = ScriptedClient([_resp(_covered("f1"))])
    pages, gather = _write_fixture(tmp_path, "cluster-a", PAGE, ("f1",))
    adjudications = {"cluster-a::f1": False}

    report = run_fact_recall_eval([_entry("cluster-a", "topic", ("f1",))], pages, gather,
                                  a_llm, b_llm, adjudications=adjudications)

    assert report.consensus_count == 0
    assert report.verdict == VERDICT_FINAL_FAIL
    assert report.clusters[0].miss_taxonomy == (
        {"fact_id": "f1", "fact_text": "golden fact f1",
         "classification": "adjudicated_not_covered", "provisional": False},
    )


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


def _audit_response(verdict):
    return json.dumps({"verdict": verdict, "reason": "scripted"})


def _load_script(name):
    import importlib.util
    filename = name.replace("_", "-")
    path = Path(__file__).resolve().parents[1] / "scripts" / f"{filename}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _clients():
    return {
        "gather": ScriptedClient([json.dumps({"queries": []}), json.dumps({"queries": []})]),
        "writer": ScriptedClient([
            _page("Page text.", [{
                "id": "c1", "text": "The fix shipped on Tuesday.",
                "citations": [{"doc_id": "sources/f1", "chunk_id": "sources/f1#1"}],
            }]),
            _page("Gutted page.", [{
                "id": "c1", "text": "The fix shipped on Tuesday.",
                "citations": [{"doc_id": "sources/ghost", "chunk_id": "sources/ghost#1"}],
            }]),
        ]),
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
    assert sidecar["gathered_chunk_ids"] == ["sources/f1#1"]
    assert sidecar["native_passed"] is True
    assert len(sidecar["page_sha256"]) == 64


def test_driver_records_failing_pipeline_as_data_without_aborting_batch(tmp_path):
    """A pipeline that runs but fails its judges (no emission) is data: recorded
    in the sidecar, batch continues. An unexpected exception is the abort case
    (covered by the next test)."""
    driver = _load_script("synthesize_golden_pages")
    engine = FakeEngine({
        "topic a": [Result("sources/f1#1", "sources/f1", "The fix shipped on Tuesday.")],
        "topic b": [],   # empty gather; the writer then cites an unknown chunk
    })
    entries = [_entry("cluster-a", "topic a", ("f1",)), _entry("cluster-b", "topic b", ("f2",))]
    out = tmp_path / "out"

    results = driver.synthesize_golden_pages(entries, out, engine, _clients(), seed_k=8)

    assert results[0]["emitted"] is True
    assert (out / "pages" / "cluster-a.md").exists()
    assert results[1]["emitted"] is False
    assert results[1]["native_passed"] is False
    sidecar_b = json.loads((out / "gather" / "cluster-b.gather.json").read_text())
    assert sidecar_b["emitted"] is False
    assert sidecar_b["page_sha256"] is None
    # diagnostic verdict details persisted even for failed pages
    # (1 iteration: the scripted repair client is exhausted and raises)
    assert sidecar_b["repair_iterations"] == 1
    assert sidecar_b["entailment_passed"] is False
    assert sidecar_b["repair_errors"]
    assert "final_claim_count" in sidecar_b and "failed_claim_ids" in sidecar_b


def test_driver_records_unexpected_exception_as_crash_sidecar(tmp_path):
    """A driver crash mid-cluster is recorded as a crash sidecar (emitted=false,
    error=driver_crash with traceback) and the batch CONTINUES -- the old
    propagate-and-die contract silently lost clusters (measured 2026-08-07:
    a cluster in v3 vanished with no sidecar while the bash loop carried on). The
    evaluator maps driver_crash sidecars to measurement_invalid, so Red's
    conflation concern (programmer errors as page misses) still holds."""
    driver = _load_script("synthesize_golden_pages")
    engine = FakeEngine({
        "topic a": [Result("sources/f1#1", "sources/f1", "The fix shipped on Tuesday.")],
        "topic b": RuntimeError("gather exploded"),
    })
    entries = [_entry("cluster-a", "topic a", ("f1",)), _entry("cluster-b", "topic b", ("f2",))]
    out = tmp_path / "out"

    results = driver.synthesize_golden_pages(entries, out, engine, _clients(), seed_k=8)
    assert len(results) == 2
    assert results[0]["emitted"] is True
    crash = results[1]
    assert crash["emitted"] is False
    assert crash["error"].startswith("driver_crash: RuntimeError: gather exploded")
    assert "traceback" in crash
    crash_sidecar = json.loads((out / "gather" / "cluster-b.gather.json").read_text())
    assert crash_sidecar["error"].startswith("driver_crash")


def test_report_serialization_retains_agreement_and_raw_responses(tmp_path):
    """Regression: the CLI serializer once dropped the entire agreement (per-fact
    verdicts, evidence spans, raw responses) before persisting -- destroying the
    audit trail. The persisted payload must keep it."""
    cli = _load_script("eval_synthesis_fact_recall")
    raw_a = _resp(_covered("f1"))
    raw_b = _resp(_uncovered("f1"))
    a_llm = ScriptedClient([raw_a])
    b_llm = ScriptedClient([raw_b])
    pages, gather = _write_fixture(tmp_path, "cluster-a", PAGE, ("f1",))
    report = run_fact_recall_eval([_entry("cluster-a", "topic", ("f1",))], pages, gather,
                                  a_llm, b_llm)

    payload = cli._as_dict(report)

    cluster = payload["clusters"][0]
    agreement = cluster["agreement"]
    assert agreement is not None
    assert agreement["result_a"]["raw"] == raw_a
    assert agreement["result_b"]["raw"] == raw_b
    assert agreement["result_a"]["verdicts"][0]["evidence"] == EVIDENCE
    assert cluster["contested_ids"] == ["f1"]
    assert cluster["consensus_covered"] == []


def _retry_clients():
    # attempt 1: writer cites an unknown chunk -> expected failure.
    # attempt 2: writer cites the gathered chunk -> emitted.
    return {
        "gather": ScriptedClient([json.dumps({"queries": []}), json.dumps({"queries": []})]),
        "writer": ScriptedClient([
            _page("Bad page.", [{"id": "c1", "text": "x",
                                 "citations": [{"doc_id": "sources/ghost", "chunk_id": "sources/ghost#1"}]}]),
            _page("Good page.", [{"id": "c1", "text": "The fix shipped on Tuesday.",
                                  "citations": [{"doc_id": "sources/f1", "chunk_id": "sources/f1#1"}]}]),
        ]),
        "repair": ScriptedClient([]),
        "audit": ScriptedClient([_audit_response("supported")]),
        "coverage_a": ScriptedClient([]),
        "coverage_b": ScriptedClient([]),
    }


def test_driver_retries_failed_attempt_and_records_attempts(tmp_path):
    driver = _load_script("synthesize_golden_pages")
    engine = FakeEngine({"topic": [Result("sources/f1#1", "sources/f1", "The fix shipped on Tuesday.")]})
    out = tmp_path / "out"

    results = driver.synthesize_golden_pages(
        [_entry("cluster-a", "topic", ("f1",))], out, engine, _retry_clients(), seed_k=8, retries=1)

    assert results[0]["emitted"] is True
    sidecar = json.loads((out / "gather" / "cluster-a.gather.json").read_text())
    assert sidecar["attempt_count"] == 2
    assert [a["emitted"] for a in sidecar["attempts"]] == [False, True]
    assert sidecar["error"] is None
    assert (out / "pages" / "cluster-a.md").exists()


def test_driver_burns_attempt_on_signal_and_retries(tmp_path):
    """A watchdog/operator SIGTERM lands inside an attempt (the handler raises
    SystemExit('terminated by ...')). It must burn only THAT attempt and let
    the retry loop continue -- the old behavior let one signal kill the whole
    cluster with a crash sidecar (measured 2026-08-07: cluster-1 attempt 2
    SIGTERM'd at the 30-min watchdog, cluster ended 0/1 emitted, attempt 3
    never ran)."""
    driver = _load_script("synthesize_golden_pages")
    engine = FakeEngine({"topic": [Result("sources/f1#1", "sources/f1", "The fix shipped on Tuesday.")]})
    out = tmp_path / "out"
    clients = _retry_clients()

    class SignalWriter:
        """Raises the signal-handler exception on first use, then succeeds
        (ScriptedClient would RETURN the exception object -- canned responses
        replay, they don't raise)."""
        def __init__(self, good):
            self.good = good
            self.calls = 0
        def complete(self, system, user, temperature=0.0):
            self.calls += 1
            if self.calls == 1:
                raise SystemExit("terminated by SIGTERM")
            return self.good

    clients["writer"] = SignalWriter(_page("Good page.", [{"id": "c1",
        "text": "The fix shipped on Tuesday.",
        "citations": [{"doc_id": "sources/f1", "chunk_id": "sources/f1#1"}]}]))
    results = driver.synthesize_golden_pages(
        [_entry("cluster-a", "topic", ("f1",))], out, engine, clients, seed_k=8, retries=1)
    assert results[0]["emitted"] is True
    assert results[0]["attempt_count"] == 2
    assert "terminated by SIGTERM" in results[0]["attempts"][0]["error"]
    assert results[0]["attempts"][1]["emitted"] is True
    sidecar = json.loads((out / "gather" / "cluster-a.gather.json").read_text())
    assert sidecar["error"] is None  # no crash sidecar: the signal was an attempt, not a crash
    assert (out / "pages" / "cluster-a.md").exists()


def test_driver_exhausts_retries_and_records_failure(tmp_path):
    driver = _load_script("synthesize_golden_pages")
    engine = FakeEngine({"topic": [Result("sources/f1#1", "sources/f1", "The fix shipped on Tuesday.")]})
    bad = _page("Bad page.", [{"id": "c1", "text": "x",
                               "citations": [{"doc_id": "sources/ghost", "chunk_id": "sources/ghost#1"}]}])
    clients = {
        "gather": ScriptedClient([json.dumps({"queries": []}), json.dumps({"queries": []})]),
        "writer": ScriptedClient([bad, bad]),
        "repair": ScriptedClient([]),
        "audit": ScriptedClient([]),
        "coverage_a": ScriptedClient([]),
        "coverage_b": ScriptedClient([]),
    }
    out = tmp_path / "out"

    results = driver.synthesize_golden_pages(
        [_entry("cluster-a", "topic", ("f1",))], out, engine, clients, seed_k=8, retries=1)

    assert results[0]["emitted"] is False
    sidecar = json.loads((out / "gather" / "cluster-a.gather.json").read_text())
    assert sidecar["attempt_count"] == 2
    assert all(not a["emitted"] for a in sidecar["attempts"])


def test_driver_persists_failed_claim_details(tmp_path):
    """The magpie-type case: a returned (not raised) failing result must persist
    the failed claims' texts so a stuck claim is diagnosable after the run."""
    driver = _load_script("synthesize_golden_pages")
    engine = FakeEngine({"topic": [Result("sources/f1#1", "sources/f1", "The fix shipped on Tuesday.")]})
    clients = {
        "gather": ScriptedClient([json.dumps({"queries": []})]),
        "writer": ScriptedClient([_page("Page text.", [{
            "id": "c1", "text": "The fix shipped on Tuesday.",
            "citations": [{"doc_id": "sources/f1", "chunk_id": "sources/f1#1"}],
        }])]),
        "repair": ScriptedClient([]),          # exhausted -> repair error, loop breaks
        "audit": ScriptedClient([_audit_response("fabricated")]),
        "coverage_a": ScriptedClient([]),
        "coverage_b": ScriptedClient([]),
    }
    out = tmp_path / "out"

    results = driver.synthesize_golden_pages(
        [_entry("cluster-a", "topic", ("f1",))], out, engine, clients, seed_k=8)

    assert results[0]["emitted"] is False
    sidecar = json.loads((out / "gather" / "cluster-a.gather.json").read_text())
    details = sidecar["failed_claim_details"]
    assert details and details[0]["id"] == "c1"
    assert details[0]["text"] == "The fix shipped on Tuesday."
    assert details[0]["citations"][0]["doc_id"] == "sources/f1"


# ---- immutable run manifest (Red round-2 deferral, now implemented) ----


def _manifest_fixture(tmp_path):
    from alexandria.eval.synthesis_fact_recall import run_fact_recall_eval
    golden = tmp_path / "golden.jsonl"
    golden.write_text(json.dumps({"id": "cluster-a", "topic": "topic",
                                  "source_docs": ["sources/f1"],
                                  "load_bearing_facts": [
                                      {"id": "f1", "text": "The fix shipped on Tuesday.",
                                       "supported_by": ["sources/f1"]}],
                                  "provenance": "hand"}) + "\n", encoding="utf-8")
    pages, gather = _write_fixture(tmp_path, "cluster-a", PAGE, ("f1",))
    a = ScriptedClient([_resp(_covered("f1"))])
    b = ScriptedClient([_resp(_covered("f1"))])
    report = run_fact_recall_eval([_entry("cluster-a", "topic", ("f1",))], pages, gather,
                                  a, b, model_a="m-a", model_b="m-b", golden_path=golden)
    return report, golden, pages, gather


def test_manifest_hashes_artifacts_and_prompts(tmp_path):
    report, golden, pages, gather = _manifest_fixture(tmp_path)
    manifest = report.manifest
    assert manifest["aggregation_version"]
    assert manifest["git_sha"]
    assert manifest["golden_sha256"]
    assert manifest["prompt_sha256"]["writer"]
    assert manifest["prompt_sha256"]["repair"]
    assert manifest["prompt_sha256"]["grader"]
    assert manifest["models"] == {"model_a": "m-a", "model_b": "m-b"}
    assert manifest["pages"]["cluster-a"]
    assert manifest["gather_sidecars"]["cluster-a"]


def test_verify_manifest_clean_when_unchanged(tmp_path):
    from alexandria.eval.synthesis_fact_recall import verify_manifest
    report, golden, pages, gather = _manifest_fixture(tmp_path)
    assert verify_manifest(report.manifest, golden_path=golden, page_dir=pages,
                           gather_dir=gather) == []


def test_verify_manifest_detects_page_edit(tmp_path):
    from alexandria.eval.synthesis_fact_recall import verify_manifest
    report, golden, pages, gather = _manifest_fixture(tmp_path)
    (pages / "cluster-a.md").write_text(PAGE + "\nedited later\n", encoding="utf-8")
    problems = verify_manifest(report.manifest, golden_path=golden, page_dir=pages,
                               gather_dir=gather)
    assert any("cluster-a.md" in p for p in problems)


def test_verify_manifest_detects_golden_edit(tmp_path):
    from alexandria.eval.synthesis_fact_recall import verify_manifest
    report, golden, pages, gather = _manifest_fixture(tmp_path)
    golden.write_text(golden.read_text() + "extra\n", encoding="utf-8")
    problems = verify_manifest(report.manifest, golden_path=golden, page_dir=pages,
                               gather_dir=gather)
    assert any("golden" in p for p in problems)


def test_manifest_empty_when_no_golden_path_given(tmp_path):
    pages, gather = _write_fixture(tmp_path, "cluster-a", PAGE, ("f1",))
    a = ScriptedClient([_resp(_covered("f1"))])
    b = ScriptedClient([_resp(_covered("f1"))])
    report = run_fact_recall_eval([_entry("cluster-a", "topic", ("f1",))], pages, gather,
                                  a, b)
    assert report.manifest == {}


def test_compare_reports_deltas_and_version_mismatch(tmp_path):
    """Offline smoke for scripts/compare-fact-recall.py: pooled deltas and
    aggregation-version mismatch detection (never silently compare reports
    scored under different rules)."""
    cli = _load_script("compare_fact_recall")
    base = {
        "manifest": {"aggregation_version": "fact-recall-v1"},
        "pooled_consensus_recall": 0.45, "pooled_union_recall": 0.525,
        "pooled_recall_a": 0.45, "pooled_recall_b": 0.525,
        "macro_consensus_recall": 0.452, "contested_count": 3,
        "scored_fact_count": 40, "verdict": "PROVISIONAL_FAIL",
        "clusters": [{"cluster_id": "cluster-a", "status": "graded",
                      "consensus_recall": 0.5, "contested_ids": ["f1"]}],
    }
    curr = json.loads(json.dumps(base))
    curr["pooled_consensus_recall"] = 0.6
    curr["verdict"] = "FINAL_FAIL"
    curr["clusters"][0]["consensus_recall"] = 0.66
    curr["clusters"][0]["contested_ids"] = []

    d = cli.compare_reports(base, curr)

    assert d["aggregation_version_match"] is True
    assert d["base"]["pooled_consensus_recall"] == 0.45
    assert d["current"]["pooled_consensus_recall"] == 0.6
    assert d["rows"][0]["base_contested"] == 1
    assert d["rows"][0]["curr_contested"] == 0

    curr["manifest"] = {"aggregation_version": "fact-recall-v2"}
    assert cli.compare_reports(base, curr)["aggregation_version_match"] is False


def test_replay_report_applies_adjudications_without_regrading(tmp_path):
    """The v1 adjudication scenario: 3 contested facts adjudicated false pins the
    verdict from PROVISIONAL to FINAL without re-running the 80-LLM-call grading."""
    cli = _load_script("eval_synthesis_fact_recall")
    base = {
        "scored_fact_count": 40, "consensus_count": 18, "contested_count": 3,
        "adjudicated_count": 0, "invalid_cluster_ids": [],
        "pooled_consensus_recall": 0.45, "pooled_union_recall": 0.525,
        "pooled_recall_a": 0.45, "pooled_recall_b": 0.525,
        "macro_consensus_recall": 0.452, "verdict": "PROVISIONAL_FAIL",
        "clusters": [
            {"cluster_id": "c1", "status": "graded", "consensus_recall": 0.5,
             "consensus_covered": ["f1"], "contested_ids": ["f2"],
             "adjudicated_fact_count": 0,
             "agreement": {"result_a": {"verdicts": [
                 {"fact_id": "f1", "covered": True}, {"fact_id": "f2", "covered": True}]},
                 "result_b": {"verdicts": [
                 {"fact_id": "f1", "covered": True}, {"fact_id": "f2", "covered": False}]}}},
            {"cluster_id": "c2", "status": "pipeline_failure",
             "consensus_recall": 0.0, "contested_ids": [], "agreement": None},
        ],
    }
    # c1::f2 contested -> adjudicated covered: consensus 18->19, contested 3->2.
    replayed = cli.replay_report(base, {"c1::f2": True})
    assert replayed["consensus_count"] == 19
    assert replayed["contested_count"] == 2
    assert replayed["pooled_consensus_recall"] == pytest.approx(19 / 40)
    assert replayed["clusters"][0]["consensus_covered"] == ["f1", "f2"]
    assert replayed["clusters"][0]["contested_ids"] == []
    # raw agreement preserved for audit
    assert replayed["clusters"][0]["agreement"]["result_a"]["verdicts"][1]["covered"] is True


def test_replay_report_rejects_unknown_adjudication_key(tmp_path):
    cli = _load_script("eval_synthesis_fact_recall")
    base = {"scored_fact_count": 1, "consensus_count": 1, "contested_count": 0,
            "adjudicated_count": 0, "invalid_cluster_ids": [],
            "pooled_consensus_recall": 1.0, "pooled_union_recall": 1.0,
            "pooled_recall_a": 1.0, "pooled_recall_b": 1.0, "macro_consensus_recall": 1.0,
            "verdict": "PASS",
            "clusters": [{"cluster_id": "c1", "status": "graded",
                          "consensus_recall": 1.0, "consensus_covered": ["f1"],
                          "contested_ids": [], "adjudicated_fact_count": 0,
                          "agreement": {"result_a": {"verdicts": [
                              {"fact_id": "f1", "covered": True}]},
                              "result_b": {"verdicts": [
                              {"fact_id": "f1", "covered": True}]}}}]}
    with pytest.raises(ValueError, match="unknown"):
        cli.replay_report(base, {"c9::f9": True})


def test_emit_summary_anonymizes_cluster_ids(tmp_path):
    gen = _load_script("emit_fact_recall_summary")
    report = {
        "git_sha": "abc123", "timestamp": "2026-08-06T00:00:00+00:00",
        "manifest": {"aggregation_version": "fact-recall-v1"},
        "scored_fact_count": 3, "consensus_count": 2, "contested_count": 0,
        "adjudicated_count": 0, "invalid_cluster_ids": [],
        "pipeline_failure_cluster_ids": ["zebra-cluster"],
        "pooled_consensus_recall": 2 / 3, "pooled_union_recall": 2 / 3,
        "pooled_recall_a": 2 / 3, "pooled_recall_b": 2 / 3,
        "macro_consensus_recall": 2 / 3, "verdict": "FINAL_FAIL",
        "total_facts": 3,
        "clusters": [
            {"cluster_id": "alpha-cluster", "status": "graded",
             "consensus_recall": 1.0, "recall_a": 1.0, "recall_b": 1.0,
             "contested_ids": []},
            {"cluster_id": "zebra-cluster", "status": "pipeline_failure",
             "consensus_recall": 0.0, "recall_a": None, "recall_b": None,
             "contested_ids": []},
        ],
    }
    golden = [{"id": "alpha-cluster", "load_bearing_facts": [{"id": "a"}, {"id": "b"}]},
              {"id": "zebra-cluster", "load_bearing_facts": [{"id": "c"}]}]
    summary = gen.build_summary(report, golden)
    markdown = gen.render_markdown(summary, title="t", source_report="/tmp/report.json")
    assert summary["mapping"] == {"alpha-cluster": "cluster-1", "zebra-cluster": "cluster-2"}
    assert "alpha-cluster" not in markdown
    assert "zebra-cluster" not in markdown
    assert "cluster-1" in markdown and "cluster-2" in markdown
    assert "FINAL_FAIL" in markdown
    assert summary["pipeline_failures"] == ["cluster-2"]


def test_replay_aggregate_fallback_without_agreement(tmp_path):
    """Pre-agreement-persistence reports (v1 artifact) lack raw verdicts; replay
    must recover prior status from consensus_covered/contested_ids and still
    recompute the pinned verdict exactly (v1: 3 contested adjudicated false ->
    FINAL_FAIL, consensus unchanged)."""
    cli = _load_script("eval_synthesis_fact_recall")
    base = {
        "scored_fact_count": 40, "consensus_count": 18, "contested_count": 3,
        "adjudicated_count": 0, "invalid_cluster_ids": [],
        "pooled_consensus_recall": 0.45, "pooled_union_recall": 0.525,
        "pooled_recall_a": 0.45, "pooled_recall_b": 0.525,
        "macro_consensus_recall": 0.452, "verdict": "PROVISIONAL_FAIL",
        "clusters": [
            {"cluster_id": "alpha-cluster", "status": "graded",
             "consensus_fact_count": 3, "consensus_recall": 0.75,
             "union_recall": 1.0, "recall_a": 0.75, "recall_b": 1.0,
             "consensus_covered": ["f1", "f3", "f4"], "contested_ids": ["f2"],
             "miss_taxonomy": [], "agreement": None,
             "adjudicated_fact_count": 0},
            {"cluster_id": "z-cluster", "status": "pipeline_failure",
             "consensus_recall": 0.0, "contested_ids": [], "agreement": None},
        ],
    }
    adj = {"alpha-cluster::f2": False}
    replayed = cli.replay_report(base, adj)
    assert replayed["consensus_count"] == 18
    assert replayed["contested_count"] == 2
    assert replayed["pooled_consensus_recall"] == pytest.approx(0.45)
    assert replayed["clusters"][0]["consensus_covered"] == ["f1", "f3", "f4"]
    assert replayed["clusters"][0]["contested_ids"] == []
    assert replayed["verdict"] == "PROVISIONAL_FAIL"  # still contested elsewhere


def test_driver_rejects_ambiguous_flag_abbreviations():
    """allow_abbrev=False (found 2026-08-07): argparse silently accepted
    `--gather <path>` as an abbreviation of `--gather-model`, swallowing a
    flag the user thought existed. Flags that look right but mean something
    else must fail loudly."""
    cli = _load_script("synthesize_golden_pages")
    import pytest as _pytest
    with _pytest.raises(SystemExit):
        cli.build_parser().parse_args([
            "--golden", "g.jsonl", "--out", "/tmp/o", "--gather", "/tmp/gather",
        ])


def test_grade_fact_recall_retries_paraphrased_evidence_with_hint():
    """Stochastic evidence-substring failures (measured 2026-08-07: the same
    page/model/prompt passed on re-run while three clusters were invalidated
    in one batch) must retry with a verbatim hint instead of nuking the
    cluster -- strictness preserved: a response only counts when its evidence
    is verbatim in the page."""
    bad = _resp(_covered("f1", evidence="a paraphrase that is not in the page"))
    good = _resp(_covered("f1", evidence=EVIDENCE))
    llm = ScriptedClient([bad, good])
    result = grade_fact_recall(llm, PAGE, _facts("f1"))
    assert result.recall == 1.0
    assert result.errors == ("evidence retried 1x",)
    assert result.raw == good


def test_grade_fact_recall_accepts_flag_after_bounded_retries():
    """After bounded retries the flagged verdict stands (contested, not
    invalid): the measurement records it for adjudication instead of
    excluding the cluster's facts."""
    bad = _resp(_covered("f1", evidence="never verbatim"))
    llm = ScriptedClient([bad, bad, bad])
    result = grade_fact_recall(llm, PAGE, _facts("f1"), evidence_retries=2)
    assert result.verdicts[0].covered is True
    assert result.verdicts[0].error == "evidence_not_verbatim"


def test_cluster_outcome_flags_evidence_failure_as_contested():
    """A fact with an evidence-not-verbatim flag from either grader joins the
    contested list (adjudication required) -- consensus needs BOTH graders'
    covered verdicts AND verified evidence."""
    from alexandria.eval.synthesis_fact_recall import FactVerdict, FactRecallAgreement, _cluster_outcome
    from alexandria.eval.synthesis_golden import LoadBearingFact, SynthesisClusterEntry

    entry = SynthesisClusterEntry("c1", "topic", ("d1",),
                                  (LoadBearingFact("f1", "the fact", ("d1",)),),
                                  "hand")
    flagged = FactVerdict("f1", True, "not in page", error="evidence_not_verbatim")
    clean = FactVerdict("f1", True, EVIDENCE)
    agreement = FactRecallAgreement(
        result_a=type("A", (), {"verdicts": (flagged,), "recall": 1.0,
                                "model": "x", "errors": (), "raw": ""})(),
        result_b=type("B", (), {"verdicts": (clean,), "recall": 1.0,
                                "model": "y", "errors": (), "raw": ""})(),
        consensus_covered=(), contested_ids=("f1",),
        consensus_recall=0.0, union_recall=1.0,
    )
    consensus, contested, misses = _cluster_outcome(entry, agreement, None, {"d1"})
    assert consensus == ()
    assert contested == ("f1",)
    assert misses == ()


def test_replay_adjudicated_false_resolves_contested():
    """Replay bug found 2026-08-07: adjudicated-FALSE facts that were
    contested (A-false/B-true) stayed in the pooled contested count -- the
    verdict-level path never decremented contested_delta on adj False. A
    resolved miss must stop being contested."""
    cli = _load_script("eval_synthesis_fact_recall")
    base = {
        "scored_fact_count": 4, "consensus_count": 2, "contested_count": 2,
        "adjudicated_count": 0, "invalid_cluster_ids": [],
        "pooled_consensus_recall": 0.5, "pooled_union_recall": 1.0,
        "pooled_recall_a": 0.5, "pooled_recall_b": 1.0,
        "macro_consensus_recall": 0.5, "verdict": "PROVISIONAL_FAIL",
        "clusters": [
            {"cluster_id": "c1", "status": "graded", "consensus_recall": 0.5,
             "consensus_covered": ["f1"], "contested_ids": ["f2", "f3"],
             "adjudicated_fact_count": 0,
             "agreement": {"result_a": {"verdicts": [
                 {"fact_id": "f1", "covered": True}, {"fact_id": "f2", "covered": False},
                 {"fact_id": "f3", "covered": False}]},
                 "result_b": {"verdicts": [
                 {"fact_id": "f1", "covered": True}, {"fact_id": "f2", "covered": True},
                 {"fact_id": "f3", "covered": True}]}}},
            {"cluster_id": "c2", "status": "graded", "consensus_recall": 0.0,
             "consensus_covered": [], "contested_ids": [],
             "adjudicated_fact_count": 0,
             "agreement": {"result_a": {"verdicts": [
                 {"fact_id": "f4", "covered": False}]},
                 "result_b": {"verdicts": [
                 {"fact_id": "f4", "covered": False}]}}},
        ],
    }
    # f2 adjudicated false (was contested A-false/B-true): resolved miss.
    replayed = cli.replay_report(base, {"c1::f2": False})
    assert replayed["contested_count"] == 1  # only f3 remains contested
    assert replayed["consensus_count"] == 2
    assert replayed["clusters"][0]["contested_ids"] == ["f3"]


def test_driver_crash_sidecar_is_measurement_invalid_not_pipeline_failure(tmp_path):
    """A driver_crash sidecar must exclude the cluster's facts from the
    denominator (INVALID), never count them as pipeline misses -- the crash is
    a measurement problem, not a synthesis failure."""
    from alexandria.eval.synthesis_fact_recall import run_fact_recall_eval

    class NoopLLM:
        def complete(self, system, user):
            raise AssertionError("no grading should happen for an invalid cluster")

    page_dir = tmp_path / "pages"
    gather_dir = tmp_path / "gather"
    page_dir.mkdir()
    gather_dir.mkdir()
    (gather_dir / "c1.gather.json").write_text(
        json.dumps({"emitted": False, "error": "driver_crash: RuntimeError: boom"})
        + "\n", encoding="utf-8")
    from alexandria.eval.synthesis_golden import LoadBearingFact, SynthesisClusterEntry
    entry = SynthesisClusterEntry("c1", "topic", ("d1",),
                                  (LoadBearingFact("f1", "fact", ("d1",)),), "hand")
    report = run_fact_recall_eval([entry], page_dir, gather_dir,
                                  NoopLLM(), NoopLLM(), golden_path=None)
    assert report.clusters[0].status == "measurement_invalid"
    assert "driver_crash" in report.clusters[0].errors[0]
    assert report.scored_fact_count == 0
    assert report.verdict == "INVALID"
