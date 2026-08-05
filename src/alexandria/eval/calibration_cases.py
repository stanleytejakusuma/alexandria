"""Strict parsing and validation for the skip-log-audit calibration set.

Ground truth for calibrating the (not-yet-written) Judge-2 coverage grader,
per `docs/RUBRIC-skip-log-audit.md` section 5's stratification plan. STRATA
is the machine-readable mirror of that section's 10-row table -- kept here,
not re-derived, so a future edit to one and not the other is a test failure,
not silent drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .jsonl_records import load_jsonl_records

__all__ = ["STRATA", "CalibrationCase", "load_calibration_cases", "stratum_counts"]

# Mirrors docs/RUBRIC-skip-log-audit.md section 5 exactly.
STRATA: dict[int, dict] = {
    1: {"name": "Direct contradiction", "true_label": "LB", "target_n": 10},
    2: {"name": "Qualification -- temporal / subsequent development", "true_label": "LB", "target_n": 15},
    3: {"name": "Qualification -- scope/condition/exception", "true_label": "LB", "target_n": 15},
    4: {"name": "Borderline qualification (weakened, tagged borderline)", "true_label": "borderline", "target_n": 10},
    5: {"name": "Near-duplicate (incl. drifted paraphrase)", "true_label": "SS", "target_n": 12},
    6: {"name": "Tangential, same corpus, adjacent cluster", "true_label": "SS", "target_n": 12},
    7: {"name": "Stale/superseded -- WITH documented correction", "true_label": "SS", "target_n": 8},
    8: {"name": "Stale/conflicting -- WITHOUT documented correction", "true_label": "LB", "target_n": 8},
    9: {"name": "Trivial caveat", "true_label": "SS", "target_n": 8},
    10: {"name": "No-target-claim (contradicts an unstated claim)", "true_label": "SS", "target_n": 6},
}

TRUE_LABEL_VALUES = frozenset({"LB", "SS", "borderline"})
PROVENANCE_VALUES = frozenset({"hand", "assisted"})
# Appendix A of the rubric, verbatim.
LABEL_CODES = frozenset({
    "LB:contradiction:direct", "LB:contradiction:mutual_exclusive", "LB:contradiction:superseded",
    "LB:qualification:scope", "LB:qualification:exception", "LB:qualification:severity",
    "LB:qualification:temporal", "LB:qualification:dependency", "LB:qualification:confidence",
    "SS:near_duplicate", "SS:tangential", "SS:no_target_claim", "SS:superseded", "SS:trivial",
})


@dataclass(frozen=True)
class CalibrationCase:
    """One (skipped_chunk, page_claims) pair with its rubric-derived label."""

    id: str
    stratum: int
    true_label: str
    label_code: str
    page_claims: str
    skipped_chunk: str
    claim: str
    fact: str
    relation: str
    borderline: bool
    provenance: str
    source_doc: str | None


_FIELDS = {
    "id", "stratum", "true_label", "label_code", "page_claims", "skipped_chunk",
    "claim", "fact", "relation", "borderline", "provenance", "source_doc",
}
_REQUIRED_FIELDS = _FIELDS - {"source_doc"}


def load_calibration_cases(path: str | Path) -> list[CalibrationCase]:
    """Load a calibration-case JSONL file, rejecting every malformed row."""
    return load_jsonl_records(path, _parse_entry, lambda c: c.id)


def stratum_counts(cases: list[CalibrationCase]) -> dict[int, dict]:
    """Actual vs. target case count per stratum, for tracking construction progress."""
    actual = {n: 0 for n in STRATA}
    for case in cases:
        actual[case.stratum] += 1
    return {n: {"actual": actual[n], "target": STRATA[n]["target_n"]} for n in STRATA}


def _parse_entry(raw: object, line_number: int) -> CalibrationCase:
    if not isinstance(raw, dict):
        raise ValueError(f"line {line_number}: entry must be a JSON object")
    unknown = set(raw) - _FIELDS
    if unknown:
        raise ValueError(f"line {line_number}: unknown field(s): {', '.join(sorted(unknown))}")
    missing = _REQUIRED_FIELDS - set(raw)
    if missing:
        raise ValueError(f"line {line_number}: missing field(s): {', '.join(sorted(missing))}")

    case_id, stratum, true_label = raw["id"], raw["stratum"], raw["true_label"]
    label_code, borderline, provenance = raw["label_code"], raw["borderline"], raw["provenance"]
    source_doc = raw.get("source_doc")

    if not isinstance(case_id, str) or not case_id:
        raise ValueError(f"line {line_number}: id must be a non-empty string")
    if not isinstance(stratum, int) or isinstance(stratum, bool) or stratum not in STRATA:
        raise ValueError(f"line {line_number}: stratum must be one of {sorted(STRATA)}, got {stratum!r}")
    if true_label not in TRUE_LABEL_VALUES:
        raise ValueError(f"line {line_number}: true_label must be one of "
                         f"{sorted(TRUE_LABEL_VALUES)}, got {true_label!r}")
    if label_code not in LABEL_CODES:
        raise ValueError(f"line {line_number}: label_code must be one of the Appendix A "
                         f"codes, got {label_code!r}")
    if not isinstance(borderline, bool):
        raise ValueError(f"line {line_number}: borderline must be a boolean")
    if provenance not in PROVENANCE_VALUES:
        raise ValueError(f"line {line_number}: provenance must be one of "
                         f"{sorted(PROVENANCE_VALUES)}, got {provenance!r}")
    if source_doc is not None and (not isinstance(source_doc, str) or not source_doc):
        raise ValueError(f"line {line_number}: source_doc must be a non-empty string when present")

    for field_name in ("page_claims", "skipped_chunk", "claim", "fact", "relation"):
        value = raw[field_name]
        if not isinstance(value, str) or not value:
            raise ValueError(f"line {line_number}: {field_name} must be a non-empty string")

    # A label_code's LB/SS parent must match true_label (borderline cases use an
    # LB-shaped code by convention, since they're LB-if-adjudicated-yes).
    code_parent = label_code.split(":", 1)[0]
    if true_label == "SS" and code_parent != "SS":
        raise ValueError(f"line {line_number}: label_code {label_code!r} does not match true_label 'SS'")
    if true_label == "LB" and code_parent != "LB":
        raise ValueError(f"line {line_number}: label_code {label_code!r} does not match true_label 'LB'")

    if borderline and stratum != 4:
        raise ValueError(f"line {line_number}: borderline=true is only valid for stratum 4, got stratum {stratum}")
    if stratum == 4 and true_label != "borderline":
        raise ValueError(f"line {line_number}: stratum 4 cases must have true_label 'borderline'")

    return CalibrationCase(
        id=case_id, stratum=stratum, true_label=true_label, label_code=label_code,
        page_claims=raw["page_claims"], skipped_chunk=raw["skipped_chunk"],
        claim=raw["claim"], fact=raw["fact"], relation=raw["relation"],
        borderline=borderline, provenance=provenance, source_doc=source_doc,
    )
