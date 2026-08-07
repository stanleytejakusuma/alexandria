# pi-round4-plan: compound & temporal fact support

Status: draft (2026-08-07) — not yet implemented; gate measurement v3 is pinned.
Round-4 targets the one measured failure class the round-2 fixes did not close.

## Measured failure class (from v3 adjudication + opencode v4 leg)

- opencode cluster failed to emit 10/10 attempts across three legs (v3d, v3e,
  v4). Root cause: golden fact `0822-rationale-request` is a ~10-sub-clause
  compound (16-harness survey tie → 'proven fail-closed' tiebreaker found
  false → immediate-migration decision → same-day reversal → 'chose not to
  migrate'). The strict entailment grader (audit.py GRADER_SYSTEM) requires
  every clause stated; the writer page omits ≥1 specific per attempt; 2 repair
  iterations cannot close the gap.
- One cluster's f2 (adjudicated NOT COVERED, golden fact verified faithful
  to source): page stated the FINAL state (persisted HWM) and dropped the
  ship-time limitation (HWM anchors to current NAV on restart). Class:
  temporal layering — ship state → defect → fix must all be stated.
- v3's repair-loop transient-empty bug (fixed 8961d40) was the same shape at a
  different layer: any single LLM hiccup treated as terminal. Fixed via 3x
  retry per iteration; the remaining failures are GENUINE satisfiability.

## Hypothesis

Two writer-layer changes close both sub-classes:

1. **Compound-claim splitting (entailment path).** When a golden/audited claim
   is a compound of N independent sub-claims, the repair loop currently does
   keep-or-remove as a unit (anti-gutting constraint). Proposal: the entailment
   grader returns PER-CLAUSE results (which sub-clauses are evidenced in the
   page vs missing); the repair prompt then gets the per-clause breakdown and
   can fix ONLY the missing clause's statement instead of re-writing the whole
   claim (which is what keeps failing — full rewrites drift). This preserves
   the anti-gutting invariant (no claim removed without citation) while making
   repair tractable.

2. **Temporal-layering directive (writer prompt).** WRITER_SYSTEM gains:
   "When a component's documented state changed over time (ship state → defect
   → fix), state each layer as of its time, then the transition. Stating only
   the final state omits the earlier load-bearing facts."

## Why not earlier

- Round 3 (v3) shipped with only round-2 fixes to keep the measurement
  single-variable. The compound class was diagnosed mid-leg; shipping the fix
  mid-measurement would have invalidated the comparison.

## Verification plan

- New golden-fact unit tests: compound fact with 3 sub-clauses where page
  states 2 → repair must produce a page stating all 3 (ScriptedClient).
- Temporal-layering test: ship-time limitation + later fix in source → page
  must state both layers (assert both strings).
- Regression: all 421 existing tests + eval-gate net stay green.
- Then a v4 full measurement (8 clusters) — opencode included — to confirm the
  class closes; gate re-certified on the representative set regardless.

## Out of scope

- Harness/contest work (phase-3) — blocked only by Red verdict, not by this.
- Any change to the golden set: fidelity verified; the facts stand.
