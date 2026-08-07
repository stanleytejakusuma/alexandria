# Alexandria mission report — 2026-08-08 (overnight autonomous run)

Status: IN PROGRESS — final numbers land when the v4 leg completes.

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
| TBD | TBD | leg completes → evaluator (sonnet-5 + terra) → pin → stop-rule → reindex → contest run 2 → docs → report |

## v4 leg state

- Runner job: `alx-v4-leg-1786125`; log /tmp/alx/synth-v4b-run.log
- Monitors: `alx-v4-opencode-*` (milestone) + `alx-v4-leg-*` (completion + evaluator)
- HEAD measured: d7e4cfa (round-4 clause repair + temporal directive + temp0.1 coverage)

## Contract checklist (loop-termination, signed 2a5eb09)

- [ ] v4 = last synthesis leg — NO v5 under any outcome
- [ ] contest run 2 = last contest run — no run 3
- [ ] <5pt improvement over v3 (85%) → declare and stop (convergence rule)
- [ ] New failure classes → binding backlog, never the gate
- [ ] Cheap-verification rule: no leg without committed fix + exact-artifact test

## Outcome slots (pre-declared)

- v4 opencode emits → compound class closed
- v4 pooled >= 90% → (waiver re-certification; still no v5)
- v4 pooled < 90% → stop-rule declares; phase-2 stays a documented waiver
- contest run 2 PASS → extension activates (docs + README)
- contest run 2 FAIL → Alexandria stays opt-in; contest becomes monthly telemetry

_Filled in at mission end with real numbers._
