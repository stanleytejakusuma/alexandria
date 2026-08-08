# pi-contest-cycle2-amendment.md

# Phase-3 contest — cycle 2 amendment (draft for principal signature)

**Status: DRAFT 2026-08-08 — not in force until Stanley signs.**
Amends `docs/SPEC-phase3-harness.md` (frozen cycle-1 mechanics) and is the
contract diff required by `docs/pi-loop-termination.md` before a new cycle.

## Why a new cycle

Cycle 1 closed with NO verdict: both runs INVALID on grader disagreement
(0.50, then 0.25, vs the 0.20 cap). The disagreement is symmetric (both
graders say yes/no in both directions) — the query set sits in genuinely
borderline relevance territory. The flaw is the *discard rule*: a 0.25
disagreement rate should trigger adjudication, not throw away a $8
measurement. Alexandria additionally showed a widening margin (+0.018 →
+0.065) and a halved disagreement rate across the two runs — signal worth
measuring properly.

## Amendments (cycle 2 only; cycle-1 results remain as recorded)

1. **Adjudication replaces discard.** When graders A and B disagree on a
   query's relevance set, the query goes to a pre-registered adjudicator:
   `gpt-5.6-sol` (temperature 0.1 — the llm.py fast-tier guard forbids 0).
   The adjudicator sees the same blinded union and the two graders'
   relevance sets, and decides relevance. Disagreement cap raised from
   0.20 to **0.40 of queries**: above that the run is still INVALID
   (measurement too unstable to trust even adjudicated).
2. **Query set: unchanged, still frozen at the cycle-1 set of 40.**
   Keeping the same queries preserves the cross-cycle trend line (the
   telemetry value). The borderline queries stay — they are the honest
   measurement, not a bug to engineer around.
3. **Corpus state: snapshot at run start** (git sha + chunk count in the
   manifest). Cycle 2 runs on the current corpus — which now includes the
   wiki pages and the ingested session notes — because a new cycle measures
   the current system, not a historical one. Cross-cycle recall numbers are
   NOT directly comparable; per-query agreement with cycle 1 is reported as
   drift telemetry.
4. **Per-query verdicts published** in the report (was: aggregate only).
5. **Everything else frozen:** seed, k=5, union-blinding, recall@5, Wilson
   CIs, tie rule, floor 0.60, <=3 runs/cycle, enumerated infra failures,
   PASS/FAIL/INVALID precedence, leak controls.

## What a PASS would mean

Same as SPEC §3: Alexandria outperforms the incumbent with the floor met —
followed by the write-surface scope review (not automatic; a new recorded
decision). What a FAIL means: nothing changes; Alexandria stays opt-in
read-only + ingestion; telemetry continues. No third option invented here.

## Signature

- [ ] Stanley signs → manifest `contest-cycle2-20260808` is created, run 1
      launches with the frozen mechanics above.
- [ ] Not signed → cycle 2 does not exist; cycle 1 records stand as final.
