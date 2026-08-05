# WORK ORDER — Phase 2 synthesis core (single-page pipeline)

**Repo:** `~/codebase/alexandria` · **Branch:** `phase2-synthesis-core`, not `main`
**Venv:** `.venv` (`.venv/bin/python`, never system python) · Python 3.12
**Baseline:** `main` at 328 passing tests. Do not regress it.

---

## 0. Why this exists, and why it's scoped the way it is (read first)

Phase 1 (retrieval) and the phase-1 eval harness are built and gate-passing.
Phase 2 is the synthesis sweep: an LLM writes cited wiki pages from
retrieved source chunks. This project's founding failure mode — the reason
it exists at all — is a 21k-note vault that died from **deferred synthesis
judgment**: content got compressed by an LLM with nothing checking whether
the compression preserved truth. Phase 2 does not repeat that. The judges
below are built and calibrated **before** this work order was written, on
purpose — judge before player.

**This work order is scoped to the core single-page pipeline only: gather
one topic's sources, synthesize one page, judge it, repair it if needed.**
It deliberately does **not** cover *exhaustive full-corpus sweep
orchestration* (how every topic across the whole corpus gets enumerated and
scheduled). That orchestration's spec ("§6.1a", referenced from
`SPEC-phase2-eval.md`) was genuinely missing when this work order was first
written — not locatable in this repo, session history, or memory — and has
since been reconstructed and fully resolved via direct confirmation, not
guessed: see `docs/DECISIONS-phase2-execution-model.md`. That resolution
does not change this work order's scope, though. Full-sweep orchestration
is still explicitly **out of scope** here (§8) and is still its own,
separate work order (not yet written) — the split was always a deliberate
mirror of how phase 1 itself was split, independent of whether the spec was
available.
how phase 1 itself was split into a retrieval-library order and a separate
eval-harness order — prove the core mechanism on one page before scaling to
"sweep everything."

---

## 1. Where things live

| Thing | Location | Why |
|---|---|---|
| This code | this repo, `src/alexandria/synthesis/` (new) | public, generic |
| Private corpus | `~/alexandria-corpus` | never commit corpus content into this repo |
| Ground truth (read-only inputs) | `~/alexandria-corpus/.alexandria/golden/{golden-synthesis-v1,contradiction-pairs-v1,coverage-calibration-v1}.jsonl` | private |
| Synthesized pages (this work order's output) | `~/alexandria-corpus/wiki/` (new dir, private, gitignored — same as `sources/`) | generated content, not engine code |
| Skip logs | alongside each page, same dir | per SPEC-phase2-eval.md §"Data model" |

**Never commit corpus, wiki, or ground-truth content into this repo.** Tests use
synthetic fixtures only. `scripts/precommit-scan.py` is a pre-commit hook and
will block you — fix the content, do not weaken the scanner.

---

## 2. What already exists — call these, do not rebuild them

Three judges are built and independently calibrated. This work order wires
them into a pipeline; it does not re-derive their logic.

- **`audit.py`** (`grade_note`) — entailment judge. v2 prompt (strict
  relationship-span quoting), calibrated on RAGTruth n=295: Evident Conflict
  98.3%, Subtle Conflict 73.3%, clean false-positive 32.5%. Verdicts:
  `supported` / `not_supported` / `fabricated`.
- **`coverage.py`** (`grade_skip`, `grade_skip_twice`) — coverage judge
  (Judge 2). Calibrated on `coverage-calibration-v1.jsonl` n=71: LB recall
  100% (small-n), SS false-positive 14.3%. `grade_skip_twice` is the
  borderline-detection mechanism — validated result 28.6% disagreement on
  genuinely-contested cases vs 3.1% on confident ones (see
  `docs/calibration/coverage-borderline-disagreement-fable-vs-sol-clean.log`).
  Verdicts: `LB` (load-bearing omission) / `SS` (safe skip, coded per
  `RUBRIC-skip-log-audit.md` Appendix A) / `borderline` (from disagreement,
  not single-shot self-report — see §6 below, this is a real trap).
- **`gather_completeness.py`** (`run_gather_completeness`) — gather-stage
  measurement (Judge 3). Real measured baseline against
  `contradiction-pairs-v1.jsonl` n=13: 30.8% pair recall at k=8, Wilson 95%
  CI [12.7%, 57.6%] — **this is the current gather mechanism's ceiling
  before this work order's gather loop exists.** A real, actionable
  secondary finding: of 9 misses, 6 found the *later/corrected* claim but
  not the *original superseded* one — your gap-detection step (§4.2) should
  specifically account for this (searching for earlier assertions, not just
  more content on a topic).

Read `docs/SPEC-phase2-eval.md` in full before writing any code — it is the
authoritative spec for all three judges' gates. Read
`docs/RUBRIC-skip-log-audit.md` in full — it is the authoritative decision
procedure the coverage judge encodes; you do not need to re-derive it, but
you need to understand what it protects against to wire the repair loop
correctly (§4.4).

**Read `docs/DECISIONS-graph-vs-vector.md` before designing gather.** It
already settled the architecture question this work order would otherwise
have to re-litigate: no persistent graph, a bounded 2-round disposable
gather loop instead. Building anything graph-shaped here is a spec
deviation — stop and report if you believe otherwise, do not just build it.

---

## 3. End-to-end pipeline this work order builds

```
topic/target (a query or seed doc_id — see §8, selection is out of scope,
              assume this work order is invoked with one already chosen)
    │
    ▼
[4.1] GATHER — bounded 2-round loop, no persistent state
    │
    ▼
[4.2] SYNTHESIZE — one LLM call, produces page text + per-claim citations
    │
    ▼
[4.3] JUDGE — entailment (audit.py) + coverage (coverage.py) against the
    │         gathered pool; chunk accounting is deterministic, not LLM
    ▼
[4.4] REPAIR — bounded loop, anti-gutting guard, re-judges every iteration
    │
    ▼
[4.5] EMIT — page + skip log, both carrying author/visibility per the
             attribution seams already in the data model
```

---

## 4. Deliverables

### 4.1 `src/alexandria/synthesis/gather.py`

Per `DECISIONS-graph-vs-vector.md`: seed retrieve → one LLM pass asking
"what's referenced in this candidate pool but not present" → one follow-up
retrieve → merge. **Hard bound: 2 rounds, no more, ever** — this is not a
tunable default, it's the anti-compounding-error guard (round 1's own
analysis: 85% per-hop accuracy compounds to 44% at 5 hops; capping at 2
keeps this nowhere near that regime).

- `gather(engine, topic_query: str, *, seed_k: int = 8) -> GatherResult`
  (name the dataclass fields yourself; must include at minimum: the merged
  candidate chunk pool, which chunks came from round 1 vs round 2, and the
  gap-detection LLM's raw follow-up queries for auditability).
- Round 1: `engine.search(topic_query, k=seed_k)` — reuse the existing
  `SearchEngine`, do not build a second retrieval path.
- Gap-detection LLM call: given the round-1 pool's content, ask what's
  referenced-but-missing. **Per Judge 3's own measured finding (§2 above),
  the prompt must explicitly ask about earlier/superseded assertions, not
  just adjacent topics** — this is empirically where round-1 gather already
  fails most often; a generic "what's missing" prompt without this framing
  is closer to a decorative feature than a fix.
- Round 2: retrieve on the gap-detection queries, merge into the pool
  (dedupe by doc_id, do not double-count a chunk found both rounds).
- **No LLM call may block indefinitely or retry unboundedly.** Reuse
  `LLMClient`'s existing retry/backoff — do not reimplement it.
- **Nothing here is persisted.** The whole point of the disposable-loop
  design is no standing index; `gather()` returns a value, keeps no state
  between calls, and the caller does not need to clean anything up.

### 4.2 `src/alexandria/synthesis/write.py`

One LLM call: given the gathered pool, write a short cited page.

- Every factual claim must carry a citation to a specific `doc_id` (and
  ideally chunk-level reference) from the gathered pool. **A claim with no
  citation is a spec violation the entailment judge will correctly catch —
  do not try to prevent this by construction with clever prompting instead
  of relying on the judge; the judge existing is the point.**
- The prompt must ask the model to state claims it can support and is
  explicitly permitted to leave out chunks it judges immaterial — that
  omission is exactly what §4.3's coverage judge checks, not something to
  avoid by forcing every chunk into the page.
- Output must be parseable into: page text, `author` field
  (`synthesis-sweep@<model>@<prompt-version>` per
  `DECISIONS-multi-actor-posture.md`), `visibility` field (optional,
  default absent, seam only — do not build enforcement, see §8), and a
  structured claim list (claim text + cited doc_id(s)) separate from the
  prose, since §4.3 needs to iterate claims individually.

### 4.3 `src/alexandria/synthesis/judge.py`

Wires the three already-built judges together. **This module contains no
new grading logic** — it is orchestration only.

- **Chunk accounting (deterministic, not LLM):** every chunk in the
  gathered pool (§4.1's output) must end up either (a) cited by at least
  one claim, or (b) present in the skip log with a deterministic predicate:
  `duplicate_of:<chunk_id>`, `below_salience:<score>`, or
  `out_of_scope:<rule>`. An unaccounted chunk is a build **error**, raised,
  not logged and continued past — per SPEC-phase2-eval.md, this is lint,
  not judgment.
- **Entailment pass:** for every claim, call `audit.py`'s `grade_note`
  (different model grading than wrote the page — reuse the existing
  two-model-family discipline, do not grade with the same model that
  synthesized). Gate: ≥95% supported, zero fabricated.
- **Coverage pass:** stratified sample of the skip log's entries, call
  `coverage.py`'s `grade_skip_twice` (not single-shot `grade_skip` — per
  §2 above, single-shot self-reported "borderline" doesn't work; the
  consensus/disagreement mechanism is what's validated). Gate: zero
  load-bearing skips in the audited sample; a `borderline` consensus is
  **not** a pass, route it to human review, do not silently treat it as
  either LB or SS.
- Return a structured verdict: pass/fail per gate, the specific failing
  claims/skips (not just an aggregate pass/fail — §4.4 needs to act on
  specifics), and every grader error recorded (never silently dropped, same
  discipline as `run_eval`/`run_gather_completeness`).

### 4.4 `src/alexandria/synthesis/repair.py`

**Bounded repair loop with the anti-gutting guard** — this piece was
already independently, fully specified in `SPEC-phase2-eval.md`'s own
text (quoted), before the rest of the execution model was reconstructed in
`DECISIONS-phase2-execution-model.md`: *"The repair loop may not fix a coverage failure by
fabricating support (Judge 1 re-runs on every repair iteration — the two
judges are each other's anti-gaming guard)"* and *"the anti-gutting guard
prevents the repair loop from satisfying the entailment gate by deleting
claims — deletions are counted by Judge 2."*

- Concretely: if entailment fails on a claim, the repair step may either
  (a) find a real supporting citation for it from the gathered pool, or
  (b) remove the claim. If it removes a claim, that removal must be logged
  as a skip (§4.3's chunk accounting), and the **next** repair iteration
  re-runs *both* judges, not just entailment — a repair that improves
  entailment by gutting content must not silently pass coverage as a side
  effect of having fewer claims to check.
- **Hard bound: a fixed small number of iterations (your call, document
  the number and why), not unbounded.** Never retry indefinitely.
- If bounded iterations exhaust without both gates passing, the page is
  **not emitted** — return the failure with full diagnostic detail (which
  gate, which claims/skips, how many iterations). A silently-emitted
  failing page is worse than no page.

### 4.5 `src/alexandria/synthesis/pipeline.py`

Thin composition of 4.1 → 4.2 → 4.3 → 4.4, plus emission (write page +
skip log to `~/alexandria-corpus/wiki/`, both files, both carrying
`author`). This is the one function a future full-sweep orchestrator (a
later work order) will call once per topic — keep its signature simple and
its behavior fully deterministic given the same gathered pool and model
responses, so it's easy to drive from a batch driver later without this
module needing to change.

---

## 5. THE TEST THAT MATTERS MOST

**Prove the repair loop's anti-gutting guard actually blocks gutting, not
just that it exists in a comment.** Construct a scripted scenario (via
`ScriptedClient`) where a "repair" step deletes a claim to fix entailment,
and assert that (a) the deletion gets logged as a skip, (b) the next
iteration re-runs coverage, not just entailment, and (c) if the deletion
creates a load-bearing omission, the page still fails and is not emitted —
gutting must not be a viable way to pass.

Second most important: prove chunk accounting is airtight. A gathered pool
with N chunks where the synthesized page cites some subset and the skip log
covers the rest **exactly** — off by even one uncounted chunk must raise,
not warn.

If either of these can't demonstrably fail, they're decoration — same
standard the phase-1 eval-harness work order set for itself.

---

## 6. Constraints

1. **TDD.** Tests before implementation, suite green at every commit.
2. **Every test offline**: `ScriptedClient` for all LLM calls (gather's
   gap-detection, write's synthesis call, both judges), a fake/stub
   `SearchEngine` for retrieval — mirror the `FakeEngine` pattern already
   used in `tests/test_eval_runner.py` and `tests/test_gather_completeness.py`,
   do not invent a third pattern.
3. **Do not modify**: `audit.py`, `coverage.py`, `gather_completeness.py`,
   `contradiction_golden.py`, `calibration_cases.py`, `golden.py`,
   `synthesis_golden.py`, anything under `index/` or `retrieval/`, `llm.py`
   (including its temperature=0 refuse-guard on the Codex-fast-tier models
   and its cache-busting nonce — both are load-bearing fixes for a real,
   confirmed gateway bug, not incidental code). If you believe one of these
   must change, STOP and report why before touching it.
4. **The single-shot-vs-disagreement trap is real, do not walk into it
   again**: `coverage.py`'s single-call `grade_skip` was measured to almost
   never self-report `borderline` even when explicitly licensed to (0/7 in
   the first real run). Any code path in this work order that needs to
   detect genuine ambiguity must use `grade_skip_twice`'s consensus
   mechanism, not a single call with a hopeful prompt.
5. No new dependencies without a documented reason in your report.
6. Determinism: given the same gathered pool and the same scripted LLM
   responses, `pipeline.py` must produce the same output. Non-determinism
   in judge orchestration (as opposed to the LLM calls themselves, which
   are inherently non-deterministic at nonzero temperature) is a bug.

---

## 7. Known traps

- `~/alexandria-corpus/wiki/` does not exist yet — create it, and gitignore
  it the same way `sources/`'s parent dir already is, so wiki content never
  accidentally lands in this repo's git history.
- `gpt-5.6-sol`/`terra`/`luna`/`gpt-5.5` are refused by `LLMClient` at
  temperature=0 — a real, confirmed-live gateway bug (cross-request answer
  corruption), not a stale rule. If your synthesis or judge calls need one
  of these models, pass a nonzero temperature explicitly (see `coverage.py`'s
  `grade_skip(..., temperature=...)` for the established pattern) — do not
  work around the guard, and do not remove it.
- `coverage-calibration-v1.jsonl`'s stratum 4 (borderline) entries are
  genuinely ambiguous by construction — if your repair loop or judge
  orchestration ever needs a "clean" example to test against, do not reach
  for stratum 4.
- ANY-OF discipline: `golden-synthesis-v1.jsonl`'s facts and
  `contradiction-pairs-v1.jsonl`'s claim_a/claim_b are tuples, not single
  doc ids, for a real, measured reason (this corpus restates facts across
  near-duplicate documents) — code that assumes a single canonical doc_id
  per fact will under-count correctness the same way two earlier ground-
  truth sets did before this was fixed.

---

## 8. Out of scope — do not build

**Full-corpus sweep orchestration / exhaustive topic enumeration** — the
full-sweep orchestration spec (see §0; now resolved in
`DECISIONS-phase2-execution-model.md`, but still deliberately out of this
work order's scope). Do not guess at this; a later work order
will cover it once the spec gap is resolved. This work order's
`pipeline.py` takes one topic/target as input and is invoked once; do not
build a scheduler, a queue, or a "run this over the whole corpus" driver.

Also out of scope: visibility/ACL **enforcement** (the field is a seam
only, per `DECISIONS-multi-actor-posture.md` — YAGNI'd deliberately, do not
build checking logic for it) · any graph-structured storage or retrieval
(rejected, see `DECISIONS-graph-vs-vector.md`) · the consumer-facing search
API / FastAPI HTTP layer (phase 3+) · a query-time router of any kind ·
index-time deduplication clustering (a separate, not-yet-scoped item) ·
`min_score` retrieval-abstention thresholds (same) · anything that touches
the live LLM gateway's configuration.

---

## 9. Verification before reporting done

```bash
.venv/bin/python -m pytest tests/ -q                    # all green, no skips masking failures
.venv/bin/python scripts/precommit-scan.py --all
```

Then, against the real corpus and a real (not scripted) LLM, for at least
one real topic drawn from `golden-synthesis-v1.jsonl`'s clusters: run the
full pipeline end to end, and report the actual gather/entailment/coverage
numbers you get — honestly, including if they're worse than the standalone
judge calibrations would predict (a full pipeline can fail in ways the
individual judges' isolated calibration runs didn't exercise).

---

## 10. Report back

- Modules built + test counts.
- Proof the anti-gutting guard and chunk accounting can both demonstrably
  fail (§5) — which test, what it asserts.
- The real end-to-end run's numbers (§9), compared honestly against the
  standalone judge baselines in §2.
- Any spec deviation, and why.
- Anything in §6/§7 that bit you anyway.
- (This item is now resolved — `docs/DECISIONS-phase2-execution-model.md`
  exists and covers the full-sweep execution model. No action needed; kept
  here only so the history of the gap and its resolution stays visible in
  this document, not silently edited away.)
