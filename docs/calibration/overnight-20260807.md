# Overnight build log — 2026-08-07 (Stanley asleep, autonomous session)

Scope: keep building while the phase-2 gate measured; cost-cautious; every
commit tested and leak-scanned. All timestamps local.

## The night's numbers (the gate story)

| Event | Result |
|---|---|
| v2-leg1 (sonnet-5, 3 clusters, from the 23:35 run) | cluster-1 ✅ · cluster-2 ✅ · **cluster-3 ✅** (the v1 deterministic-failure cluster — fix round 1's repair directive worked) |
| opus-4.5 leg (Stanley's model swap, 5 clusters) | **opencode failed 3/3 native checks** (~22 min each) → opus is not a drop-in for sonnet-5; measurement must stay single-model |
| v2-leg2 (sonnet-5, 5 clusters, launched ~01:45) | running (opencode attempt 1 longest-ever: 85+ min at last check) |

**Decision recorded**: opus-4.5 via Bedrock is not the synthesis model; the
proven cost-cautious config is sonnet-5 (gather/write/repair/audit/coverage-a)
+ deepseek-v4-pro (coverage-b). One variable at a time.

## Shipped (all committed + pushed)

| Commit | What |
|---|---|
| `0c6c860` | immutable run manifest (build_manifest / verify_manifest, sha-256 bindings) |
| `5518127` | compare-fact-recall.py — report delta tool |
| `dd3833c` | `--replay` mode (adjudicate without re-grading) + emit-fact-recall-summary.py |
| `6e5fa3c` | GitHub Actions CI (pytest + leak scan) · eval-gate synthesis wiring · measure-synthesis.sh · README phase-2 status |
| `355bd6a` | replay aggregate fallback (pre-agreement v1 reports) |
| `822ed26` (branch `phase2-clustering`) | **clustering: dedup + topic passes** + calibration script (10 tests) |
| `65ae1a8` | phase-4: `answer` endpoint + wiki-site renderer (7 tests) |
| `f09b47e` | demo corpus (Northwind Dynamics / Project Proxima, 10 leak-clean docs) |
| `370c722` | answer fix (failed-claim path + sonnet-5 default) — caught live on the demo run |
| `ea7b45a` (branch `phase2-full-sweep`) | **full-sweep orchestrator** (serial fold, exhaustive accounting, checkpoint resume) + driver (6 tests) |
| `ff2ad5e` | SPEC-phase3-harness (blinded contest, pre-registered PASS/FAIL/INVALID) |
| `dcb6606` | Pi extension skeleton (inert until the gate passes) |
| `5024f26` | fresh-clone test as one command |

**412 tests green** (was 381 at night start). Every commit leak-scanned.

## Verified live (demo corpus, end-to-end)

`answer "What is the Proxima deal state and what happens next?"` — a
cited, cross-team handoff page covering all 4 teams and 10 sources, every
claim with a chunk-level citation; native checks passed. Search ranks the
verbal-close doc #1. The phase-4 story is real.

## Bugs found and fixed overnight

1. **argparse prefix-abbreviation trap** (`allow_abbrev=False`): `--gather`
   was silently parsed as `--gather-model`, sidecars landed in nested dirs.
2. **cmd_answer attribute bug**: `repair.failed_claim_ids` →
   `repair.verdict.failed_claim_ids` (caught by the live demo run).
3. **sweep accounting bug**: excluded docs were unioned into accounted
   before the overlap check (the accounting check now separates them).

## Morning checklist (in order)

1. v2c run: `grep "=====" /tmp/alx/synth-v2c-run.log` — expect 5 clusters
   (sonnet-5); sidecars at `/tmp/alx/synth-v2/gather/`.
2. Evaluate: `scripts/eval-synthesis-fact-recall.py --pages
   /tmp/alx/synth-v2/pages --gather /tmp/alx/synth-v2/gather
   --base-url $ALEX_BASE_URL --api-key-env $ALEX_API_KEY_ENV
   --model-a openrouter/anthropic/claude-sonnet-5 --model-b deepseek-v4-pro
   --output <private-golden-dir>/report.json`
3. Compare vs pinned v1 (`compare-fact-recall.py`), replay adjudications,
   emit sanitized summary (`emit-fact-recall-summary.py`). (Gateway: set
   ALEX_BASE_URL + ALEX_API_KEY_ENV per the private-run conventions.)
4. Gate verdict → PASS: merge clustering (calibration numbers below),
   run the sweep §6 bounded run (8 golden clusters), then the full sweep.
   FAIL: fix round 2 scoped to the measured failure taxonomy.
5. Open items: remove the local `alexandria` gateway key once hosted
   production is proven; phase-3 contest run (cost-guarded, 3 runs max).

## Calibration results (clustering thresholds, real ground truth)

- Dedup: 20 hand-verified positive pairs, chunk-level max-cosine.
  **t=0.75 chosen** (precision 1.00 / recall 0.70, precision-first: a
  false merge is data loss; missed dups are caught by the sweep's
  cross-page layer). Max-F1 point is 0.60/0.91 but at precision 0.83.
- Topic: 8 hand-built clusters, first-chunk probes. **t=0.75 chosen**
  (mean best-match Jaccard 0.53). Full-corpus sweep re-measurement is
  the sweep run's own output.
- Small-n caveats are printed by the script itself (Wilson intervals).
