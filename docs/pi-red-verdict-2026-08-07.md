# pi-red-verdict-2026-08-07: phase-2 gate disposition (gpt-5.6-sol)

Source: Red chain hop 5, `openrouter/openai/gpt-5.6-sol` via the local
openrouter-payg gateway (relay hops 1-4 broken; `RED_CHAIN_OVERRIDE=5` with
brief via stdin worked). Verdict verbatim preserved in this record.

## Verdict: APPROVE-WITH-CHANGES

Proceed to Phase 3, but do NOT represent Phase 2 as an unqualified PASS.
Record it as a **qualified waiver**: 97.1% on the post-hoc representative
subset, 85% on the pre-registered full set, with a known failure on
short-lived decision/reversal episodes.

## Load-bearing risks (ranked, from the chain)

1. Exclusion is outcome-driven despite outcome-independent wording;
   "<=3 calendar days" is a weak proxy for rapid-reversal difficulty.
   Valid as a PROSPECTIVE reporting stratum, not a retroactive rescue.
2. Evaluation unit malformed: a ~10-sub-clause "fact" is not atomic; one
   clause failing the page measures grader brittleness, not recall.
   Atomization + per-assertion scoring is the principled repair.
3. Golden set became a development set (fixes toward measured misses =
   calibration); 34/35 on 7 correlated clusters = weak generalization.
4. Adjudicator non-independence: audit trail makes decisions inspectable,
   not unbiased; tolerable internally, inadequate for public benchmarks.
5. v2 semantics likely raised scores but may be more valid; the problem is
   the mid-loop rule change + non-independent adjudication. Rescore all
   systems under ONE frozen rule.
6. Excluded class is operationally relevant; phase-3 queries will include
   it; cannot be dismissed as out-of-scope without changing the product
   claim.
7. Contest spec adequacy requires reviewing the actual file; pre-registered
   != unbiased.

## Adopted decisions

- [x] Publish three numbers together (frozen full set 34/40 85% FAIL;
      prospective stable stratum 34/35 97.1%; reversal stratum separate).
- [x] Label advancement a documented gate waiver, not a clean PASS
      (README + calibration docs updated 2026-08-07).
- [x] Atomize compound facts + per-assertion scoring = round-4 plan item 1
      (docs/pi-round4-plan.md), preserving original scores.
- [x] Freeze phase-2: no further changes without a new version and a
      held-out cluster set.
- [ ] Blinded independent review of contested facts (sample + all
      score-changing adjudications) before any public benchmark claim.
- [ ] Phase-3 spec revision per the 9 conditions (next build step).

## Phase-3 minimum conditions (from the chain, verbatim intent)

1. Query sampling, exclusions, minimum sample size, representation of
   rapid reversals. 2. Exact recall@5 unit (chunks/docs/facts/answers) +
   relevance and qualifier rules. 3. Corpus snapshot, ingestion cutoff,
   tool versions, equal indexing/resource budgets. 4. Blinded/randomized
   output ordering + independent judging. 5. Paired comparison,
   aggregation, tie handling, uncertainty intervals, PASS threshold.
   6. Fixed seeds/runs, no optional stopping; reruns only for enumerated
   infrastructure failures, logged before unblinding. 7. Leakage controls
   separating query/gold construction from tuning. 8. Per-query and
   per-stratum reporting. 9. Mechanical (non-operator-discretionary)
   INVALID criteria.

## Consequence for the harness build

Build phase-3 once these conditions are frozen. Do not spend another loop
optimizing the current eight-cluster set.
