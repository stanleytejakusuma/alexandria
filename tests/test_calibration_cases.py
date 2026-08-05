import json

import pytest

from alexandria.eval.calibration_cases import (
    STRATA,
    CalibrationCase,
    load_calibration_cases,
    stratum_counts,
)


def _write(path, *rows) -> None:
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _base_row(**overrides) -> dict:
    row = {
        "id": "case-1",
        "stratum": 1,
        "true_label": "LB",
        "label_code": "LB:contradiction:direct",
        "page_claims": "The fix was verified working after deployment.",
        "skipped_chunk": "The fix failed verification in the same deploy window.",
        "claim": "The fix was verified working.",
        "fact": "The fix failed verification.",
        "relation": "direct negation of the verified-working claim",
        "borderline": False,
        "provenance": "hand",
        "source_doc": None,
    }
    row.update(overrides)
    return row


def test_load_calibration_cases_parses_a_full_case(tmp_path):
    path = tmp_path / "cal.jsonl"
    _write(path, _base_row())

    cases = load_calibration_cases(path)

    assert cases == [CalibrationCase(
        id="case-1", stratum=1, true_label="LB", label_code="LB:contradiction:direct",
        page_claims="The fix was verified working after deployment.",
        skipped_chunk="The fix failed verification in the same deploy window.",
        claim="The fix was verified working.", fact="The fix failed verification.",
        relation="direct negation of the verified-working claim",
        borderline=False, provenance="hand", source_doc=None,
    )]


def test_source_doc_is_optional_for_synthetic_cases(tmp_path):
    path = tmp_path / "cal.jsonl"
    _write(path, _base_row(source_doc="sources/example-source/some-doc"))
    cases = load_calibration_cases(path)
    assert cases[0].source_doc == "sources/example-source/some-doc"

    path2 = tmp_path / "cal2.jsonl"
    _write(path2, _base_row(id="case-2"))
    cases2 = load_calibration_cases(path2)
    assert cases2[0].source_doc is None


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("stratum", 11),
        ("stratum", 0),
        ("true_label", "MAYBE"),
        ("label_code", "LB:made_up_code"),
        ("provenance", "robot"),
    ],
)
def test_rejects_invalid_field_values(tmp_path, field, bad_value):
    path = tmp_path / "cal.jsonl"
    _write(path, _base_row(**{field: bad_value}))
    with pytest.raises(ValueError, match=fr"line 1.*{field}" ):
        load_calibration_cases(path)


def test_label_code_must_be_consistent_with_true_label(tmp_path):
    """An SS-coded case claiming true_label LB (or vice versa) is a real
    ground-truth-corruption risk -- catch it structurally, not by review alone."""
    path = tmp_path / "cal.jsonl"
    _write(path, _base_row(true_label="SS", label_code="LB:contradiction:direct"))
    with pytest.raises(ValueError, match=r"line 1.*label_code.*true_label|line 1.*true_label.*label_code"):
        load_calibration_cases(path)


def test_borderline_cases_must_be_stratum_4(tmp_path):
    path = tmp_path / "cal.jsonl"
    _write(path, _base_row(borderline=True, stratum=1))
    with pytest.raises(ValueError, match=r"line 1.*borderline"):
        load_calibration_cases(path)


def test_missing_required_field(tmp_path):
    path = tmp_path / "cal.jsonl"
    row = _base_row()
    del row["relation"]
    _write(path, row)
    with pytest.raises(ValueError, match=r"line 1.*relation"):
        load_calibration_cases(path)


def test_duplicate_id_rejected(tmp_path):
    path = tmp_path / "cal.jsonl"
    _write(path, _base_row(), _base_row())
    with pytest.raises(ValueError, match=r"line 2.*duplicate"):
        load_calibration_cases(path)


def test_stratum_counts_matches_rubric_section_5_targets():
    """Sanity anchor: STRATA must match the 10-row table in
    docs/RUBRIC-skip-log-audit.md section 5, so a future edit to one without
    the other is caught by CI rather than silently drifting apart."""
    assert len(STRATA) == 10
    assert STRATA[1]["target_n"] == 10
    assert STRATA[1]["true_label"] == "LB"
    assert STRATA[4]["true_label"] == "borderline"
    assert STRATA[4]["target_n"] == 10
    assert sum(s["target_n"] for s in STRATA.values()) == 104


def test_stratum_counts_reports_actual_vs_target(tmp_path):
    path = tmp_path / "cal.jsonl"
    _write(path, _base_row(id="a", stratum=1), _base_row(id="b", stratum=1))
    cases = load_calibration_cases(path)

    counts = stratum_counts(cases)

    assert counts[1]["actual"] == 2
    assert counts[1]["target"] == 10
    assert counts[2]["actual"] == 0
    assert counts[2]["target"] == 15
