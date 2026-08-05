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

## Borderline-via-disagreement, clean final run (2026-08-05)

`coverage-borderline-disagreement-fable-vs-sol-clean.log` -- claude-fable-5
vs gpt-5.6-sol (temperature=0.1 on the sol side; LLMClient refuses it
outright at temperature=0, permanently, even though the underlying gateway
dedup bug is now fixed -- see llm.py comment and the gateway work order
(kept outside this repo, private infrastructure) for why that guard stays
regardless of upstream state). This run is the one to trust:
0 errors, 71/71 graded, disagreement concentrated 28.6% (n=7) on stratum 4
vs 3.1% (n=64) on the other nine strata -- a ~9x ratio, the sharpest signal
of every attempt at this experiment. Three earlier attempts the same day
(sol at temperature=0, then terra at temperature=0, then this same
fable-vs-sol pairing before the gateway fix) were all silently corrupted by
a gateway-side request-deduplication bug unrelated to Alexandria's own code
or model reasoning quality -- see the work order for the full trace. A
fourth, valid run against claude-fable-5 vs deepseek-v4-pro (a model outside
the affected class, used before the gateway fix was confirmed) also showed
the correct directional signal (28.6% vs 9.4%), corroborating this result
independently before the gateway-side root cause was even found.

## Judge 3: gather-completeness for CONTRA-SCAN, first real measurement (2026-08-05)

`gather-completeness-judge3-v1.log` -- the real result against
`contradiction-pairs-v1.jsonl` (6 pairs, post ANY-OF fix) and the real
corpus, k=8 (matching `rerank_prefetch`, the actual gather-stage depth, not
the final top-5 a user sees). **16.7% pair recall (1/6), far below the 90%
gate.** Do not read the exact percentage as precise -- n=6 gives a Wilson
95% CI of roughly [3%, 56%] -- but the direction is real: single-shot
retrieval at k=8 cannot reliably surface both sides of a contradiction in
one query. This is not a labeling artifact: every miss's actual retrieved
candidates were checked against real content before this number was
trusted (see the contradiction-pairs-v1 commit in the corpus repo), and 4
of 6 pairs' near-misses turned out to be genuine, different documents, not
duplicates that should have counted.

This result is direct empirical support for a design decision already made
independently (`DECISIONS-graph-vs-vector.md`): the bounded phase-2 gather
loop (seed retrieve -> detect referenced-but-missing -> one follow-up
retrieve) exists specifically to close gaps like this one, where a single
retrieval pass finds one side of a contradiction but not the other. Next
step per SPEC-phase2-eval.md's own order of work: this seeded-contradiction
set is far too small (n=6) to trust as a real gate; needs the same
expansion treatment the retrieval golden set and coverage-calibration sets
already got.
