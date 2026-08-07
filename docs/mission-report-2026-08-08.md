# Alexandria mission report — 2026-08-08 (overnight autonomous run)

Status: COMPLETE — v4 measured and declared (FINAL_FAIL 28/40, stop-rule fired, no v5); contest cycle closed with no verdict; all docs committed.

## Mission (from Stanley, 2026-08-07 ~23:50 WIB)

Ship Alexandria end-to-end through the final phase: complete the v4 golden
leg, run the phase-3 contest run 2 (last of the cycle), close all
documentation, and deliver this report. Models: deepseek-v4-flash for all
agent reasoning, gpt-5.6-sol via Red for roadblocks. Loop-termination
contract (signed) governs: no v5, no retry-until-success, frozen taxonomy,
declared decision tree.

## Timeline

| UTC | WIB | Event |
|---|---|---|
| 15:24 | 22:24 | Mission directive received |
| 15:25 | 22:25 | create_goal; root cause of the v4 leg killer found: coverage grader at temperature=0 refused by llm.py for gpt-5.6-terra |
| 16:01 | 23:01 | fix committed d7e4cfa (temperature=0.1 through judge coverage + evaluator); 429 passed |
| 16:0x | 23:0x | poisoned leg killed (job alx-v4-leg-1786115); dirs cleaned |
| 16:2x | 23:2x | leg relaunched — job alx-v4-leg-1786125, HEAD d7e4cfa |
| 16:3x | 23:3x | timezone-marker fix committed 5ec1408 (print-only) |
| — | — | monitors fixed (sidecar schema: gather/*.gather.json, cluster_id) and re-armed |
| 17:24 | 00:24 | v4 attempt 1/3 (cluster 1) starts |
| 17:44 | 00:44 | cluster 1 EMITTED attempt 1 (entailment/coverage/accounting all clean) — temp fix vindicated; cluster 2 starts |
| 18:01 | 01:01 | cluster 2 EMITTED attempt 1 (the cluster the poisoned leg killed) |
| 19:01 | 02:01 | opencode attempt 1 (compound-class test) |
| 19:37 | 02:37 | opencode attempt 3/3 (failed — new single-claim blocker) |
| 20:36 | 03:36 | cluster 7 attempt 1 |
| 21:02 | 04:02 | **ALL DONE — 7/8 emitted** (clusters 1,2,5,6,7,8 clean; clusters 3+4 emit-failed) |
| 04:06 | 07:06 | evaluator report: PROVISIONAL_FAIL, pooled 0.625, 4 contested |
| 04:2x | 07:2x | adjudication: 3 covered (cluster 2 f2, cluster 7 f4, cluster 8 f3) → v4-pinned **28/40 = 0.70 FINAL_FAIL**; compare vs v3-pinned (−0.15) |
| 04:3x | 07:3x | **convergence stop-rule declared: no v5**; docs + commits (422e6cd); backlog entry; cluster 3+4 sidecar evidence preserved |
| 04:37 | 07:37 | v4 wiki pages synced into corpus wiki (they were never part of the corpus before); reindex |
| 05:1x | 08:1x | **contest run 2 launched** (job alx-contest-run2, pid 20616) — LAST run of the cycle |
| TBD | TBD | contest verdict → docs → final report |

## v4 leg state

COMPLETE. 7/8 clusters emitted; clusters 3+4 failed all 3 attempts
(single-claim entailment each; sidecars + last-page text preserved). Runner job
`alx-v4-leg-1786125`; evaluator report
`~/alexandria-corpus/.alexandria/golden/synthesis-fact-recall-v4-20260808-040024.json`;
pinned `synthesis-fact-recall-v4-pinned.json` (28/40 = 0.70 FINAL_FAIL);
compare vs v3-pinned −0.150.

## Contest run 2 state

COMPLETE — **INVALID** (disagreement 0.25 > 0.20 pre-registered cap; SPEC
§3: run discarded, no verdict recorded). recall@5: Alexandria 0.532 vs
incumbent 0.468 (+0.065 — margin widened from run 1's +0.018; disagreement
halved 0.50 → 0.25; floor still unmet 0.53 < 0.60). Cycle closed with no
contest verdict; Alexandria stays read-only; extension inert; contest
becomes standing monthly telemetry. Records:
/tmp/alx/contest-run2/ + private corpus
`contest-run2-DISPOSITION.md`.

## Mission outcome

DONE within the signed contract's bounds: v4 measured and declared
(FINAL_FAIL 28/40, convergence stop-rule fired, no v5, backlog entry),
contest cycle closed (no verdict, stays read-only), all documentation
committed (public: 422e6cd, 33c5496 + earlier; private corpus: pinned
reports, adjudications, dispositions). The loop-termination contract was
honored at every fork — including the two places it cost us progress
(killed healthy leg, no retry-until-success).

## Contract checklist (loop-termination, signed 2a5eb09)

- [x] v4 = last synthesis leg — NO v5 under any outcome (declared 2026-08-08, backlog entry)
- [x] contest run 2 = last contest run — no run 3
- [x] <5pt improvement over v3 (85%) → declare and stop (convergence rule — FIRED: v4 0.70 < 0.85)
- [x] New failure classes → binding backlog, never the gate
- [x] Cheap-verification rule: no leg without committed fix + exact-artifact test

## Outcome slots (pre-declared)

- v4 opencode emits → compound class closed → NOT ACHIEVED (blocker moved 0822-rationale-request → initial-stay-leaning; class declared limitation)
- v4 pooled >= 90% → NOT ACHIEVED (0.70; stop-rule fired instead)
- v4 pooled < 90% → stop-rule declares; phase-2 stays a documented waiver → FIRED
- contest run 2 PASS → extension activates (docs + README) → PENDING
- contest run 2 FAIL → Alexandria stays opt-in; contest becomes monthly telemetry → PENDING

_Filled in at mission end with real numbers._
