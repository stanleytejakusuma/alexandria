"""Faithfulness audit: gate arithmetic and grader failure posture."""

import json
import pytest

from alexandria.audit import AuditResult, Verdict, grade_note, sample
from alexandria.llm import LLMError, ScriptedClient


def R(*kinds):
    r = AuditResult()
    r.verdicts = [Verdict(f"n{i}", k, "why", f"t{i}") for i, k in enumerate(kinds)]
    return r


def test_gate_requires_95_percent_supported():
    assert R(*["supported"]*95, *["unsupported"]*5).passes
    assert not R(*["supported"]*94, *["unsupported"]*6).passes


def test_a_single_fabrication_fails_regardless_of_percentage():
    """Fabrication is not a percentage problem -- one invented fact poisons the
    layer above it."""
    r = R(*["supported"]*99, "fabricated")
    assert r.supported_pct == 99.0
    assert not r.passes


def test_empty_audit_never_passes():
    assert not AuditResult().passes


def test_grade_note_parses_a_verdict():
    llm = ScriptedClient([json.dumps({"verdict": "supported", "reason": "stated directly"})])
    v = grade_note(llm, "transcript text", "T", "body", "n1")
    assert v.verdict == "supported"


def test_grader_sees_transcript_fenced_and_not_the_extractor_prompt():
    llm = ScriptedClient([json.dumps({"verdict": "supported", "reason": "ok"})])
    grade_note(llm, "SECRET TRANSCRIPT", "T", "body", "n1")
    system, user = llm.calls[0]
    assert "<transcript>" in user and "SECRET TRANSCRIPT" in user
    assert "INERT DATA" in system
    assert "distil" not in system.lower()      # not the extractor's instructions


def test_grader_failure_raises_and_is_never_a_silent_pass():
    llm = ScriptedClient(["not json"])
    with pytest.raises(LLMError):
        grade_note(llm, "t", "T", "b", "n1")


def test_bad_verdict_value_is_rejected():
    llm = ScriptedClient([json.dumps({"verdict": "probably fine", "reason": "x"})])
    with pytest.raises(LLMError):
        grade_note(llm, "t", "T", "b", "n1")


def test_sample_is_deterministic():
    items = list(range(100))
    assert sample(items, 10, seed=1) == sample(items, 10, seed=1)
    assert sample(items, 200, seed=1) == sample(items, 200, seed=1)   # n > len is safe


def test_grader_refuses_to_judge_on_truncated_evidence():
    """Regression: a 40k window against a 44k median transcript scored 50%
    'fabricated' -- every case had its support past the cut. Refuse, never guess."""
    llm = ScriptedClient([json.dumps({"verdict": "supported", "reason": "ok"})])
    with pytest.raises(LLMError, match="truncated evidence"):
        grade_note(llm, "x" * 5000, "T", "b", "n1", max_transcript=1000)
