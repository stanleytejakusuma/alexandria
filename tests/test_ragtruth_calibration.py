"""Calibrating audit.py against RAGTruth: the ground-truth mapping and stratified
sampler are pure functions with real judgment calls baked in, so they get real tests.
Nothing here touches a network or a model -- that's scripts/calibrate-audit.py's job.
"""

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "calibrate_audit", Path(__file__).resolve().parent.parent / "scripts" / "calibrate-audit.py")
calibrate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(calibrate)


def resp(labels):
    return {"labels": [{"label_type": t} for t in labels]}


def test_clean_response_expects_supported():
    assert calibrate.ground_truth(resp([])) == "supported"


def test_any_conflict_expects_fabricated():
    """Conflict means the response contradicts the source -- an exact match to
    audit.py's own definition of fabricated. Strict, not lenient."""
    assert calibrate.ground_truth(resp(["Evident Conflict"])) == "fabricated"
    assert calibrate.ground_truth(resp(["Subtle Conflict"])) == "fabricated"
    # mixed: a response with a Conflict AND a Baseless span is still fabricated --
    # the presence of one confirmed contradiction settles it
    assert calibrate.ground_truth(resp(["Evident Baseless Info", "Subtle Conflict"])) == "fabricated"


def test_baseless_only_expects_not_supported():
    """RAGTruth's 'Baseless Info' (no basis in source) does not cleanly map to
    audit.py's unsupported-vs-fabricated split -- documented as an intentional
    lenient category, not a strict one."""
    assert calibrate.ground_truth(resp(["Evident Baseless Info"])) == "not_supported"
    assert calibrate.ground_truth(resp(["Subtle Baseless Info"])) == "not_supported"


def test_scoring_is_strict_for_fabricated_and_lenient_for_not_supported():
    assert calibrate.is_correct("fabricated", "fabricated") is True
    assert calibrate.is_correct("fabricated", "unsupported") is False
    assert calibrate.is_correct("fabricated", "supported") is False

    assert calibrate.is_correct("not_supported", "unsupported") is True
    assert calibrate.is_correct("not_supported", "fabricated") is True   # lenient
    assert calibrate.is_correct("not_supported", "supported") is False

    assert calibrate.is_correct("supported", "supported") is True
    assert calibrate.is_correct("supported", "fabricated") is False


def test_stratified_sample_guarantees_rare_category_coverage():
    """Subtle Conflict is 16 of 2,675 test-split items (0.6%) -- a uniform random
    sample of a few hundred would likely draw zero. Stratification must not."""
    items = (
        [{"id": f"clean{i}", "labels": []} for i in range(1000)]
        + [{"id": f"sc{i}", "labels": [{"label_type": "Subtle Conflict"}]} for i in range(16)]
        + [{"id": f"ec{i}", "labels": [{"label_type": "Evident Conflict"}]} for i in range(500)]
    )
    plan = {"clean": 50, "Subtle Conflict": 16, "Evident Conflict": 30}
    sampled = calibrate.stratified_sample(items, plan, seed=0)

    got_ids = {item["id"] for item in sampled}
    assert sum(1 for i in got_ids if i.startswith("sc")) == 16
    assert sum(1 for i in got_ids if i.startswith("ec")) == 30
    assert sum(1 for i in got_ids if i.startswith("clean")) == 50


def test_stratified_sample_is_deterministic():
    items = [{"id": str(i), "labels": []} for i in range(200)]
    a = calibrate.stratified_sample(items, {"clean": 30}, seed=7)
    b = calibrate.stratified_sample(items, {"clean": 30}, seed=7)
    assert [i["id"] for i in a] == [i["id"] for i in b]


def test_category_of_labeled_response_picks_dominant_type():
    """For stratification bucketing: a response is filed under the single most
    specific label type it carries, conflict outranking baseless (rarer, more
    diagnostic), subtle outranking evident within each (the harder case)."""
    assert calibrate.category(resp([])) == "clean"
    assert calibrate.category(resp(["Evident Baseless Info"])) == "Evident Baseless Info"
    assert calibrate.category(resp(["Evident Conflict", "Evident Baseless Info"])) == "Evident Conflict"
    assert calibrate.category(resp(["Subtle Conflict", "Evident Conflict"])) == "Subtle Conflict"


def test_source_text_passes_through_plain_strings():
    assert calibrate.source_text("already text") == "already text"


def test_source_text_renders_qa_dict_readably():
    out = calibrate.source_text({"question": "phone number", "passages": "passage 1: ..."})
    assert "phone number" in out and "passage 1" in out


def test_source_text_renders_arbitrary_structured_data():
    """Data2txt's source_info shape wasn't fully known ahead of time -- must not
    crash on an unexpected dict shape, just render it."""
    out = calibrate.source_text({"name": "value", "count": 3})
    assert "name: value" in out and "count: 3" in out
