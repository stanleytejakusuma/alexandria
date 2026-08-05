import json

import pytest

from alexandria.coverage import GRADER_SYSTEM, SkipVerdict, grade_skip
from alexandria.eval.calibration_cases import LABEL_CODES
from alexandria.llm import LLMError, ScriptedClient


def _resp(label, label_code, claim="the claim", fact="the fact", relation="direct negation"):
    return json.dumps({"label": label, "label_code": label_code,
                       "claim": claim, "fact": fact, "relation": relation})


def test_grade_skip_parses_a_load_bearing_verdict():
    llm = ScriptedClient([_resp("LB", "LB:contradiction:direct")])
    v = grade_skip(llm, "The fix was verified working.", "The fix failed verification.", "case-1")
    assert v == SkipVerdict("case-1", "LB", "LB:contradiction:direct",
                            "the claim", "the fact", "direct negation")


def test_grade_skip_parses_a_safe_skip_verdict():
    llm = ScriptedClient([_resp("SS", "SS:tangential")])
    v = grade_skip(llm, "claims", "chunk", "case-2")
    assert v.label == "SS"
    assert v.label_code == "SS:tangential"


def test_grade_skip_accepts_borderline():
    llm = ScriptedClient([_resp("borderline", "LB:qualification:temporal")])
    v = grade_skip(llm, "claims", "chunk", "case-3")
    assert v.label == "borderline"


def test_bad_label_value_is_rejected():
    llm = ScriptedClient([_resp("maybe", "LB:contradiction:direct")])
    with pytest.raises(LLMError, match="bad label"):
        grade_skip(llm, "claims", "chunk", "case-4")


def test_bad_label_code_is_rejected():
    llm = ScriptedClient([_resp("LB", "LB:made_up_code")])
    with pytest.raises(LLMError, match="bad label_code"):
        grade_skip(llm, "claims", "chunk", "case-5")


def test_label_code_parent_must_match_label():
    """An SS verdict paired with an LB-coded reason is exactly the ground-truth-corruption
    shape calibration_cases.py already guards against on the data side -- the grader
    output needs the same guard, not just the stored cases."""
    llm = ScriptedClient([_resp("SS", "LB:contradiction:direct")])
    with pytest.raises(LLMError, match="label_code.*label|label.*label_code"):
        grade_skip(llm, "claims", "chunk", "case-6")


def test_grader_failure_raises_and_is_never_a_silent_pass():
    llm = ScriptedClient(["not json"])
    with pytest.raises(LLMError, match="case-7"):
        grade_skip(llm, "claims", "chunk", "case-7")


def test_missing_field_is_rejected():
    llm = ScriptedClient([json.dumps({"label": "LB", "label_code": "LB:contradiction:direct"})])
    with pytest.raises(LLMError):
        grade_skip(llm, "claims", "chunk", "case-8")


def test_grader_prompt_requires_the_exhibited_triple():
    """Falsifiability mechanism from rubric section 0: an LB label without a specific
    (claim, fact, relation) triple is invalid on its face -- the prompt must ask for it,
    mirroring audit.py's own 'quoted relationship span' requirement."""
    assert "claim" in GRADER_SYSTEM.lower()
    assert "fact" in GRADER_SYSTEM.lower()
    assert "relation" in GRADER_SYSTEM.lower()


def test_grader_prompt_names_the_borderline_option():
    assert "borderline" in GRADER_SYSTEM.lower()


def test_all_appendix_a_codes_are_valid_grader_output():
    """Sanity anchor: the grader's accepted label codes must be exactly Appendix A,
    reusing calibration_cases.py's own LABEL_CODES rather than a second hardcoded list
    that could silently drift from it."""
    for code in LABEL_CODES:
        llm = ScriptedClient([_resp("LB" if code.startswith("LB") else "SS", code)])
        v = grade_skip(llm, "claims", "chunk", f"case-{code}")
        assert v.label_code == code
