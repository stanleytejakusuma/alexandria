# audit.py calibration history

Raw logs from each RAGTruth calibration run, kept because the headline numbers
alone (recorded in commit messages) don't preserve the full per-category
breakdown -- an independent audit (Fable, 2026-08-04) flagged that v2's full
breakdown existed only in commit-message summary form, not as a durable
artifact. This directory closes that gap.

All three runs used the identical 295-item stratified sample (seed=0) from
`scripts/calibrate-audit.py`, so the three logs are directly comparable.

- `audit-v1-baseline.log` -- original prompt (if recovered; v1's numbers are
  fully recorded in commit `3afd1ce` regardless)
- `audit-v2-full-breakdown.log` -- the shipped prompt (commit `3d45c8c`).
  Only Subtle Conflict (73.3%) and clean false-positive (32.5%) were recorded
  durably before this; the full breakdown (Evident Conflict 98.3%, Evident
  Baseless 86.7%, Subtle Baseless 92.5%) lived only in terminal output.
- v3 (compositional support, tried and reverted) is fully recorded in commit
  `262ded6` and in `tests/test_audit.py`; no separate log needed here.

## coverage.py (Judge 2, skip-log audit) calibration history

- `coverage-grader-v1-run1.log` -- first real run of `scripts/calibrate-
  coverage-grader.py` against `coverage-calibration-v1.jsonl` (71 real
  cases). Headline numbers: LB recall 100% (15/15, small-n -- Wilson 95% CI
  [79.6%, 100%]), SS false-positive rate 14.3% (7/49). **Do not read this as
  "grader validated, ship it"** -- a manual spot-check of 4 verdicts (not
  just the tally) found two real, unresolved issues before this log was
  saved: (1) one SS case the grader labeled LB with defensible-sounding
  reasoning about undisclosed conditionality, suggesting an earlier same-
  night adjudication may have resolved that case too confidently rather
  than the grader being simply wrong; (2) on all 7 stratum-4 (borderline)
  cases -- built specifically to be genuinely contested -- the grader never
  once output "borderline" despite the prompt explicitly licensing it,
  always resolving to a confident label instead. Preliminary conclusion,
  not yet acted on: single-shot self-reported uncertainty may be the wrong
  mechanism for borderline detection; disagreement across two independent
  grader runs (the same method already proven on the rubric itself, Fable
  vs Sol) is the more consistent path, still to be built.
