# Loop Termination Contract (signed 2026-08-07)

Stanley's concern (2026-08-07): "there's no promise that v4 will be the last;
I'm genuinely concerned we'll be stuck in an endless reclustering → harness →
reclustering loop." This document makes the termination structural, not
conversational. It is pre-registered the same way the gates are: rules below
were written before v4 runs and cannot be amended after its number exists.

## Why the loop doesn't self-terminate (the diagnosis)

1. **Scalar-chasing**: the gate is one number; any fix plausibly moves it, so
   there's always "one more fix".
2. **Unbounded class discovery**: each leg revealed a NEW failure class (emit
   failures → qualifier omissions → compound claims → temporal layering →
   corpus coverage). Discovery is unbounded; a gate tied to discovery is
   unbounded.
3. **Expensive verification**: a full leg costs ~$25 and 2-3h; without it a fix
   is unverified, so every fix begs for one more run.
4. **No budget, no stop rule**: nothing forces a stop.

## The contract

### 1. Cycle budget: exactly ONE more cycle

- **v4** (8-cluster golden measurement, round-4 fixes live) is the LAST
  synthesis-leg cycle this round. There is no v5. If v4 fails on the
  compound/reversal class, that class is declared a documented product-scope
  limitation (per Red's verdict instruction, docs/pi-red-verdict-2026-08-07.md)
  — not a trigger for another fix leg.
- **Contest run 2** is the LAST contest run this cycle (spec already caps at
  3). Outcome is accepted either way: PASS → extension activates; FAIL →
  Alexandria stays opt-in read-only ("menu special"), contest becomes
  scheduled telemetry, not a fix loop.

### 2. Frozen failure-class taxonomy

The gate covers exactly these five classes, frozen now — each with its
OBSERVABLE trigger (Red's required change: labels alone are a semantic sink;
"corpus coverage" could absorb any retrieval failure, "compound claims" any
long fact. The triggers below are what freeze, not the words):

1. **emit/pipeline failures** — observable: a cluster sidecar with
   `emitted: false` or a `measurement_invalid` error in the fact-recall
   report.
2. **qualifier omissions** — observable: a fact-recall verdict whose stated
   reason is a dropped negative/contrast/completion/actor qualifier that is
   present in the cited source doc.
3. **compound multi-clause facts** — observable: a claim the clause-mode
   grader splits into ≥2 clauses with differing verdicts (at least one
   clause unsupported).
4. **temporal layering** — observable: a source doc stating a state change
   over time (ship state → defect → fix) where the page omits one layer.
5. **corpus coverage** — observable: a contest query whose union (both
   systems' results) contains zero relevant results — the knowledge is
   absent from BOTH stores, not just misplaced.

A failure that matches none of these triggers is, by definition, a new
class. A failure that matches more than one is assigned to exactly one by
the adjudicator with the trigger cited — no ambiguity laundering.

A NEW class discovered in v4 (or run 2) goes to the **backlog**, not to the
gate. The gate cannot be re-litigated against a class that wasn't frozen
here. Any future cycle must publish a NEW frozen taxonomy + budget and get
explicit sign-off from Stanley before a single leg runs.

The backlog is BINDING (Red's required change): every newly discovered class
gets a dated entry in the Backlog section below, signed "no leg" — no
synthesis leg and no contest run may be justified by it. Reopening a backlog
item requires a git diff against THIS FILE plus Stanley's explicit sign-off
on the diff. A backlog with no entries is a valid backlog; a backlog that
grows without entries here is a contract breach.

### 3. Cheap verification rule

No full-cluster leg runs unless (a) a code change is committed and (b) unit
tests proving the specific frozen class are green, where "proving" means the
test reproduces the EXACT failing artifact — the v3 sidecar row, the contest
graded row, the verdict line — not merely a similar shape (Red's required
change; a shape-similar test is gaming). Expensive legs are release
events, not debug tools. "Let's just run it again to see" is forbidden —
a leg without a committed fix is a wasted leg.

### 4. Convergence stop-rule

If v4's pooled number improves over v3 (85% full-set) by **fewer than 5
points**, the loop has asymptoted → declare and stop, no more legs regardless
of how close the threshold looks. Proximity to the gate is not a reason to
iterate.

### 5. Declared decision tree (no undefined after-states)

| Outcome | Then |
|---|---|
| v4 PASS (opencode emits, class closed) | synthesis done → contest run 2 |
| v4 FAIL | reversal/compound declared product-scope limitation → contest run 2 anyway |
| run 2 PASS | extension active; switch decision on evidence |
| run 2 FAIL | read-only opt-in; contest = monthly telemetry only |
| any infra failure | logged per enumerated list; doesn't count, doesn't extend the loop |

### 6. Enforcement

This file is committed. It is also recorded in Pi's durable memory store, so
any future session proposing "one more cluster run" must first reconcile with
this contract. Enforcement is the written rule, not self-restraint.

## Backlog (binding — see §2)

_Empty at signing. Any new failure class discovered after this contract is
signed gets a dated entry here, signed "no leg"._

## Signed

Stanley — SIGNED 2026-08-07 ("signed. agree at all of it.") · Pi (kimi k3), 2026-08-07
Red review (gpt-5.6-sol, hop 5): APPROVE-WITH-CHANGES — all 3 changes
implemented (operational triggers in §2, binding backlog in §2, exact-artifact
rule in §3).
