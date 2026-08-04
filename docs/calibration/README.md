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
