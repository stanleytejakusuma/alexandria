# SPEC — Phase 3 harness extension + blinded side-by-side gate

Status: revised (2026-08-07, post-Red review gpt-5.6-sol — see
`docs/pi-red-verdict-2026-08-07.md`). Not dispatched — phase 3 starts only
when Stanley green-lights the first contest run after this revision.

## Why this gate exists

Phase 2's recall gate exists precisely so we don't take an unproven
synthesizer into a contest and lose on evidence. Phase 3 makes that
contest real: Alexandria, installed as an agent-harness memory backend,
measured head-to-head against the incumbent memory tool in daily use on
this machine, on the same queries, blinded, with adjudicated verdicts.

The gate is binary and pre-registered: **Alexandria must win recall@5
against the incumbent on the contest query set, or nothing switches.**

Phase 2's disposition: DOCUMENTED GATE WAIVER (97.1% stable stratum, 85%
full set) — the compound-fact failure class is out of scope for phase 2
certification but IN SCOPE for phase 3: the contest query set deliberately
includes rapid-reversal/transitional topics (Red condition 1), and the
round-4 writer fixes (`docs/pi-round4-plan.md`) are expected to close that
class before or during the contest cycle.

## 0. Platform scoping (Stanley, 2026-08-07)

- **Primary incumbent (gate run 1): the Pi harness's incumbent memory
  extension** — Pi's primary read/write session/project memory (SQLite
  FTS5 store, `memory_search` tool). Stanley's daily driver: Pi with
  its incumbent memory extension as the primary backend.
- **Later incumbents (separate pre-registered contests, same query set
  where overlapping, same blinding): the Claude Code project-memory
  extension, and the OpenCode vector-store memory. Not in gate run 1.
- **Codex**: memory is file-convention based, no retrieval store — out of
  contest scope; its adoption story is phase-4 (documented, not measured).
- **Open-install framing**: every path and endpoint in the harness is
  env-configurable (`ALEXANDRIA_CORPUS`, `ALEXANDRIA_GATEWAY_URL`,
  `ALEXANDRIA_API_KEY_ENV`, `INCUMBENT_MEMORY_PKG`/`INCUMBENT_MEMORY_DIR`
  for the incumbent package + store) so other users can run the contest
  against their own corpus and incumbent. No hardcoded home paths in
  committed code. (Private identifiers, host addresses, and frozen query
  sets live in the private corpus repo, not this public one.)

## 1. Deliverables

### 1.1 Pi harness integration (the extension)

`extensions/pi/` in this repo — a Pi extension exposing two tools:

- `alexandria-search` — shell to `alexandria search "<query>" --k 5`
  against the configured corpus (env `ALEXANDRIA_CORPUS`).
- `alexandria-answer` — shell to `alexandria answer "<question>"` with
  the cost-cautious model config, page printed with citations.

Extension lifecycle follows the installed Pi extension docs; the extension
is inert until the phase-3 gate passes (a switch-over requires the gate,
per this spec).

### 1.2 The contest harness (the gate)

`scripts/contest-recall.py` — measures both systems on one query set:

- **Query set**: N ≥ 40 queries, three strata — stable-topic (≈70%),
  rapid-reversal/transitional (≈20%), operational/agent-run (≈10%) —
  drawn from the phase-0/1 golden set's zero-overlap band (hand-verified
  queries where the incumbent historically fails), real session queries,
  and reversal-class topics from the phase-2 diagnostics. Exclusions
  documented (e.g., queries answerable from the agent system prompt
  alone). Pre-registered, frozen, hashed into a manifest (same discipline
  as the fact-recall manifest).
- **Systems**: (A) Alexandria retrieval over the synthesized wiki+corpus
  (hybrid + rerank, k=5); (B) the incumbent's own retrieval over its own
  store (k=5), invoked exactly as a harness would invoke it (for gate run
  1: the Pi harness incumbent memory extension's `searchMemories` path).
- **Result unit**: a retrieved result = the parent document of a chunk
  (deduplicated). A result is relevant iff the document contains the
  answer/fact the query demands; partial support counts when it answers
  the query's operative verb; agnostic/opinion answers grade not-relevant.
- **Grading**: per query, the union of both systems' top-5 docs is
  presented to two independent graders, blinded (no system labels,
  shuffled order, doc ids redacted). Relevant iff both graders agree.
- **Score**: recall@5 per system = relevant docs in that system's top5 /
  total relevant in the union. Wilson 95% CI reported per system.
- **Gate**: Alexandria recall@5 > incumbent recall@5 (a tie = |Δ| ≤ 1
  relevant doc → one pre-registered re-run, then FAIL), with a minimum
  absolute floor (≥ 60%), and no adjudicated relevance disagreement above
  the pre-registered cap (20% of queries).
- **Cost guard**: bounded to ≤ 3 contest runs per evidence cycle; each
  run's LLM spend recorded next to its result. A full run is expected in
  the low tens of dollars on the remote measurement gateway (blinded
  grading dominates).

## 2. Blinding mechanics

1. Query → both systems run independently (order shuffled, seed fixed).
2. Union results shuffled; each result shown as "Result N" with its
   source text (doc id redacted to `[sources/…]`), never the system.
3. Graders (two independent LLM clients, sonnet-5 + deepseek-v4-pro per
   the measured cost config) answer "relevant to this query? yes/no".
   Neither grader sees the other's answers or any system label.
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
- **INVALID**: manifest mismatch, grading disagreement over the cap,
  spend over budget, or a rerun not pre-registered → run discarded, no
  verdict recorded.

## 4. Red conditions (gpt-5.6-sol, 2026-08-07) — mapping

1. Query sampling/exclusions/min N/reversal representation → §1.2 (N≥40,
   strata, documented exclusions).
2. Recall@5 unit + relevance/qualifier rules → §1.2 (doc-level,
   operative-verb rule).
3. Corpus snapshot, ingestion cutoff, tool versions, equal budgets →
   manifest records: corpus git sha, ingestion cutoff date, alexandria +
   incumbent + node versions, identical k/timeout/grading for both.
4. Blinded ordering + independent judging → §2.
5. Paired comparison, aggregation, ties, uncertainty, PASS threshold →
   §1.2/§3 (recall@5 diff, tie re-run rule, Wilson CI, floor, cap).
6. Fixed seeds, no optional stopping; reruns only for enumerated
   infrastructure failures, logged before unblinding → §3 + manifest
   (enumerated infra failures: gateway timeout > 120s sustained, API key
   expiry, process crash; each logged before unblinding).
7. Leakage controls: query set frozen and hashed BEFORE any system
   tuning; no pipeline changes between freeze and run end; gold answers
   never fed to either system → manifest + §1.2.
8. Per-query and per-stratum reporting → run report includes the full
   per-query table and stratum-level recall, not only aggregates.
9. Mechanical (non-operator-discretionary) INVALID criteria → §3.

## 5. Out of scope

- Switching any live harness default off Alexandria (Stanley's call, only
  after a PASS).
- Graph-structured retrieval — formally re-examined at the phase 3 → 4
  boundary per `docs/DECISIONS-graph-vs-vector.md`, not here.
- The enterprise/multi-tenant deployment layer (post-phase-4 discussion).
- The Claude Code and OpenCode incumbent contests before gate run 1
  passes (§0).
