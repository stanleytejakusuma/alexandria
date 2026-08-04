# Phase-2 eval spec: judging the synthesis sweep

Date: 2026-08-04. Status: accepted, written BEFORE phase-2 code by design.
Constrains: the phase-2 work order and all synthesis-sweep implementation.
Inputs: README phase table (phase-2 gate), `DECISIONS-multi-actor-posture.md`
(adopted seams + dedup action space), §6.1a execution-model invariants
(exhaustive enumeration, deterministic/logged skip predicates, side-effect-free
nodes with serial fold, bounded repair loop with anti-gutting guard).

## Doctrine

The judge is built and calibrated before the player. The original vault died
from deferred synthesis judgment; the strongest competing system we've studied
(TencentDB-Agent-Memory) ships an LLM compression funnel with no public check
that compression preserves truth. Phase 2 does not begin generating pages until
every judge in this spec runs offline in CI and its gates are wired into
`eval-gate.py`.

A synthesized page can fail in exactly three ways. Each gets its own judge:

1. **It says something the sources don't support** → entailment judge (exists).
2. **It omits something the sources do support** → coverage judge (new).
3. **It misses a contradiction it should have surfaced** → gather judge (new).

## Judge 1 — Entailment (exists, calibrated)

`audit.py` v2 (strict relationship-span quoting), calibrated against RAGTruth
n=295: Evident Conflict 98.3%, Subtle Conflict 73.3%, Evident Baseless 86.7%,
Subtle Baseless 92.5%, clean false-positive 32.5% (`docs/calibration/`). The
elevated FP rate is an accepted precision/recall tradeoff: a good claim waiting
one extra review cycle is cheap; a fabricated claim shipping cited is the
failure this project exists to prevent.

**Gate (from README):** entailment audit ≥ 95% of claims supported, zero
contradicted. Claims flagged `not_supported` block page publication and enter
the bounded repair loop; the anti-gutting guard prevents the repair loop from
satisfying the entailment gate by deleting claims — deletions are counted by
Judge 2.

## Judge 2 — Coverage (new; closes the Goodhart gap)

The citation lint only punishes fabrication. It creates zero pressure against
silently dropping true facts to avoid citation risk — omission leaves no
artifact to lint. Judge 2 creates the artifact.

**Mechanism (leans on §6.1a, deterministic first, LLM second):**

- Exhaustive enumeration means every gathered chunk for a page is enumerated
  before writing. The sweep must account for 100% of enumerated chunks: each is
  either (a) **represented** — ≥1 page claim cites it, or (b) **skipped** —
  written to a skip-log with a deterministic predicate
  (`duplicate_of:<chunk_id>`, `below_salience:<score>`, `out_of_scope:<rule>`).
  An unaccounted chunk is a build ERROR, not a judgment call. This part is
  lint, not LLM.
- The LLM judgment lives in one place: **skip-log audit**. A stratified sample
  of skips is graded on one question: "does the skipped chunk contain a fact
  that contradicts or materially qualifies any claim on the page?" (Yes =
  load-bearing skip = failure.) Grader prompt calibrated the same way audit.py
  was: measured against constructed ground truth before it judges anything real.
- **Golden synthesis set** (`golden-synthesis-v1.jsonl`, private corpus,
  hand provenance): 8–12 topic clusters with hand-enumerated load-bearing facts
  each. Metric: load-bearing-fact recall per page.

**Gates:** chunk accounting = 100% (mechanical); load-bearing skips in audited
sample = 0; golden-synthesis load-bearing-fact recall ≥ 90%. The repair loop
may not fix a coverage failure by fabricating support (Judge 1 re-runs on every
repair iteration — the two judges are each other's anti-gaming guard).

## Judge 3 — Gather-completeness for CONTRA-SCAN (new)

CONTRA-SCAN cannot flag a contradiction its gather step never retrieved. This
judge measures the gather stage, not the scan itself.

- **Seeded-contradiction set**: 10–15 hand-curated pairs of genuinely
  contradicting entries from the real corpus (superseded decisions and
  corrected claims exist in quantity; each pair verified on disk, ANY-OF
  discipline as in the retrieval golden set). For each pair: given a synthesis
  target that cites member A, the gather stage must surface member B in its
  candidate pool.
- **Metric:** contradiction-pair recall in the gather pool. **Gate: ≥ 90%**,
  and every gather-stage miss is recorded with the same trace discipline as
  retrieval (degrade loudly — a gather that silently narrows its pool is the
  TencentDB failure mode wearing our clothes).

## Data model (seams inherited from the decision record)

Every synthesized page carries from day one:

- per-claim citations (`doc_id` + chunk ref) — existing lint target;
- `author`: the writing actor (`synthesis-sweep@<model>@<prompt-version>`) —
  attribution seam, also required for reproducing any page's provenance;
- `visibility`: optional, default absent — seam only, no enforcement;
- `skip_log`: the Judge-2 artifact, committed alongside the page.

Dedup during synthesis uses the adopted `store|update|merge|skip` action space.
Degraded dependencies (vectors, FTS) make dedup FAIL LOUD — a sweep run with
degraded dedup aborts; it never silently writes near-duplicates.

## Infrastructure

- All CI tests offline: `HashEmbedder` / `IdentityReranker` / `ScriptedClient`;
  graders exercised against scripted transcripts.
- Calibration scripts (real LLM, real cost) live in `scripts/`, run manually,
  raw logs committed to `docs/calibration/` — v2's near-loss of its breakdown
  is not repeated.
- `eval-gate.py` watched paths extend to `src/alexandria/synthesis/`; the gate
  runs synthesis judges against the golden-synthesis set with regression
  semantics identical to retrieval (`--fail-on-regression`).
- Every judge result appends to `eval_runs.jsonl` with `git_sha`, so any
  reported number is re-derivable from a recorded artifact at a known commit —
  the property that let an independent audit reproduce our retrieval numbers
  exactly.

## Out of scope

Consumer-facing search API (phase 3), auto-context/injection surfaces
(undecided), ACL enforcement (YAGNI per decision record), trajectory/procedure
page type (parked; will get its own eval addendum when built).

## Order of work

1. Ground truth first: golden-synthesis-v1 clusters + seeded-contradiction
   pairs (hand-curated, targets verified on disk).
2. Coverage-grader calibration (constructed omission cases; measured before it
   judges real output).
3. Judges wired into eval-gate + CI, all offline tests green.
4. Only then: the phase-2 synthesis work order is written, referencing this
   spec's gates as its acceptance criteria.
