"""calibrate-coverage-grader.py's Wilson-interval math and correctness check are pure
functions with real judgment calls baked in (LB/SS matching ignores label_code,
borderline is reported separately from accuracy), so they get real tests. Nothing here
touches a network or a model -- that's the script's own job when actually run.
"""

import importlib.util
from dataclasses import dataclass
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "calibrate_coverage", Path(__file__).resolve().parent.parent / "scripts" / "calibrate-coverage-grader.py")
calibrate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(calibrate)


@dataclass
class _Case:
    true_label: str


@dataclass
class _Verdict:
    label: str


def test_outcome_correct_matches_on_label_only_not_label_code():
    case = _Case(true_label="LB")
    verdict = _Verdict(label="LB")
    assert calibrate.outcome_correct(case, verdict) is True


def test_outcome_correct_detects_a_real_mismatch():
    case = _Case(true_label="LB")
    verdict = _Verdict(label="SS")
    assert calibrate.outcome_correct(case, verdict) is False


def test_wilson_interval_is_wide_at_small_n():
    lo, hi = calibrate.wilson_interval(9, 10)
    assert lo < 0.65   # roughly the documented ~60% lower bound at n=10, 9/10 correct
    assert hi > 0.95


def test_wilson_interval_narrows_at_large_n():
    lo_small, hi_small = calibrate.wilson_interval(90, 100)
    lo_large, hi_large = calibrate.wilson_interval(900, 1000)
    assert (hi_large - lo_large) < (hi_small - lo_small)


def test_wilson_interval_handles_zero_n():
    assert calibrate.wilson_interval(0, 0) == (0.0, 0.0)


def test_lb_ss_borderline_strata_partition_matches_the_rubric():
    """Sanity anchor: strata 1,2,3,8 = LB; 5,6,7,9,10 = SS; 4 = borderline, and every
    stratum from calibration_cases.STRATA is accounted for exactly once."""
    from alexandria.eval.calibration_cases import STRATA
    all_covered = calibrate.LB_STRATA | calibrate.SS_STRATA | calibrate.BORDERLINE_STRATA
    assert all_covered == set(STRATA)
    assert not (calibrate.LB_STRATA & calibrate.SS_STRATA)
    assert not (calibrate.LB_STRATA & calibrate.BORDERLINE_STRATA)
    assert not (calibrate.SS_STRATA & calibrate.BORDERLINE_STRATA)
