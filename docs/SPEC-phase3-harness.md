# SPEC — Phase 3 harness extension + blinded side-by-side gate

Status: draft (2026-08-07, built overnight while the phase-2 gate measured).
Not dispatched — phase 3 starts only when phase 2 certifies (README phase
table, the standing sequencing decision).

## Why this gate exists

Phase 2's recall gate exists precisely so we don't take an unproven
synthesizer into a contest and lose on evidence. Phase 3 makes that
contest real: Alexandria, installed as an agent-harness memory backend,
measured head-to-head against the incumbent memory tools already in
use on this machine on the same queries, blinded, with adjudicated verdicts.

The gate is binary and pre-registered: **Alexandria must win recall@5
against the incumbent on the contest query set, or nothing switches.**

## 1. Deliverables

### 1.1 Pi harness integration (the extension)

`extensions/pi/` in this repo — a Pi extension exposing two tools:

- `alexandria-search` — shell to `alexandria search "<query>" --k 5`
  against a configured corpus (default `~/alexandria-corpus`).
- `alexandria-answer` — shell to `alexandria answer "<question>"` with
  the cost-cautious model config, page printed with citations.

Extension lifecycle follows the installed Pi extension docs; the extension
is inert until the phase-3 gate passes (a switch-over requires the gate,
per this spec).

### 1.2 The contest harness (the gate)

`scripts/contest-recall.py` — measures both systems on one query set:

- **Query set**: drawn from the phase-0/1 golden set's zero-overlap band
  (hand-verified queries where the incumbent historically fails) plus a
  sample of real session queries. Pre-registered, frozen per contest run,
  hashed into a manifest (same discipline as the fact-recall manifest).
- **Systems**: (A) Alexandria retrieval over the synthesized wiki+corpus
  (hybrid + rerank, k=5); (B) the incumbent tool's own retrieval over its
  own store (k=5), invoked exactly as a harness would invoke it.
- **Grading**: for each query, the union of both systems' 5 results is
  presented to two independent graders in a blinded form (no system
  labels, shuffled order). A result is relevant iff both graders agree.
- **Score**: recall@5 per system = relevant results in that system's top5
  / total relevant in the union (union-based recall is the honest form —
  absolute relevance would need a fresh hand-built golden set).
- **Gate**: Alexandria recall@5 > incumbent recall@5, with a minimum
  absolute floor (≥ 60% — no "wins 12% vs 11%" theater), and no
  adjudicated relevance disagreement above a pre-registered cap.
- **Cost guard**: bounded to ≤ 3 contest runs per evidence cycle; each
  run's LLM spend recorded next to its result. A full run is expected in
  the low tens of dollars on the remote measurement gateway (blinded grading dominates).

## 2. Blinding mechanics

1. Query → both systems run independently (order shuffled, seed fixed).
2. Union results shuffled; each result shown as "Result N" with its
   source text (doc id redacted to `[sources/…]`), never the system.
3. Graders (two independent LLM clients, sonnet-5 + deepseek-v4-pro per
   the measured cost config) answer "relevant to this query? yes/no".
4. System labels attached only at scoring time, from the pre-shuffled
   mapping stored in the run manifest.

## 3. Pre-registered outcomes

- **PASS**: Alexandria wins recall@5 with the floor met and disagreement
  under the cap → the switch-over decision goes to Stanley with the full
  manifest (this spec does not auto-switch anything; harnesses on this
  machine are his call).
- **FAIL**: incumbent wins or floor unmet → Alexandria stays read-only
  on this harness; the failure is recorded with the same honesty rules as
  the phase-2 gate (no threshold re-tuning to pass, no re-litigating
  without the evidence).
- **INVALID**: manifest mismatch, grading disagreement over the cap, or
  spend over budget → run discarded, no verdict recorded.

## 4. Out of scope

- Switching any live harness default off Alexandria (Stanley's call, only
  after a PASS).
- Graph-structured retrieval — formally re-examined at the phase 3 → 4
  boundary per `docs/DECISIONS-graph-vs-vector.md`, not here.
- The enterprise/multi-tenant deployment layer (post-phase-4 discussion).
