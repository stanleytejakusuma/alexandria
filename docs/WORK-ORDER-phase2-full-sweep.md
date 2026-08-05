# WORK ORDER — Phase 2 full-corpus sweep orchestration

**Repo:** `~/codebase/alexandria` · **Branch:** `phase2-full-sweep`, not `main`
**Venv:** `.venv` (`.venv/bin/python`, never system python) · Python 3.12
**Baseline:** `main` at 328 passing tests. Do not regress it.

**DO NOT DISPATCH THIS ORDER UNTIL BOTH OF THESE ARE MERGED TO MAIN:**
`phase2-synthesis-core` (provides `src/alexandria/synthesis/pipeline.py`)
and `phase2-clustering` (provides `src/alexandria/synthesis/clustering.py`).
This document was drafted before either landed, so every reference to
those two modules' interfaces below is **provisional** — read the actual
merged code before writing anything, and if the real interface differs
from what's assumed here, follow the real code, and note the discrepancy
in your report (§7). This is exactly the trap the original "§6.1a" gap
created once already this project's history — a spec written ahead of the
code it depends on drifting from reality — do not let it happen twice.

---

## 0. Why this exists

`docs/DECISIONS-phase2-execution-model.md` settled the full-sweep
architecture: **exhaustive, cluster-based enumeration, processed as a
serial map-reduce fold with side-effect-free nodes.** This work order
builds that orchestrator. It is the third and final piece of phase 2's
core mechanism — `phase2-synthesis-core` proved the single-page pipeline
works, `phase2-clustering` proved topics can be found automatically and
validated against real ground truth, this order wires them into "sweep the
whole corpus."

Read `docs/DECISIONS-phase2-execution-model.md` in full — every design
choice below traces back to a specific, reasoned decision in that document,
not to this work order's own judgment.

---

## 1. What this order assumes exists (verify before building on it)

- `src/alexandria/synthesis/clustering.py`'s `find_topic_clusters(...)` —
  the enumeration units for this sweep. (Dedup clustering is a *different*
  concern — see §5, out of scope here.)
- `src/alexandria/synthesis/pipeline.py` — the single-topic pipeline
  (gather → synthesize → judge → repair → emit) from `phase2-synthesis-core`.
  This order calls it once per topic cluster; it does not reimplement any
  part of it.

If either module's actual shipped interface doesn't match what §2 assumes,
**adjust this order's code to the real interface, do not adjust the real
interface to match this document** — those two modules were independently
verified and merged first for a reason.

---

## 2. Deliverables

### 2.1 `src/alexandria/synthesis/sweep.py`

The serial map-reduce fold itself.

- **Enumeration**: call `find_topic_clusters` once at sweep start to get
  the full, fixed list of topics for this run. **Exhaustive accounting,
  same discipline as Judge 2's chunk-level version, one altitude up**:
  every document in the corpus must end up either (a) part of some topic
  cluster that gets processed, or (b) explicitly logged with a
  deterministic exclusion reason (`below_cluster_threshold`,
  `no_cluster_match`) — not silently absent from both. An unaccounted
  document is a build error, raised, not a warning.
- **Fixed, deterministic processing order.** Document and justify your
  ordering choice (cluster id, cluster size, corpus position — your call,
  but it must be reproducible: running the sweep twice against the same
  corpus snapshot must visit topics in the same order).
- **The fold state**: per `DECISIONS-graph-vs-vector.md`'s and
  `DECISIONS-phase2-execution-model.md`'s reasoning, the accumulated state
  must let topic N check "has this fact already been covered by an earlier
  page in this sweep" before re-synthesizing near-duplicate content across
  pages — this is the cross-page version of the exact near-duplicate
  problem that hit ground-truth construction three separate times in this
  project's history. Concretely: accumulate a mapping of covered
  facts/claims to the page that already covers them (the granularity is
  your design call — document it), and thread it into each `pipeline.py`
  call so a topic that turns out to duplicate prior coverage can link to
  the existing page rather than re-synthesize.
- **Side-effect-free nodes**: each per-topic call is
  `(topic, current_fold_state) -> (page_or_link, state_delta)`. It must
  not mutate shared state directly — the sweep loop is the only place
  state changes, by explicitly applying each delta before the next topic.
- **Strictly serial. No concurrency, no thread pool, no async fan-out.**
  This is not a performance default to optimize later — it is a direct,
  reasoned response to a real, confirmed bug found the same day this
  architecture was decided: request cross-contamination under concurrent
  load against the shared LLM gateway. Re-introducing concurrency here
  without re-litigating that decision explicitly is a spec violation, not
  an optimization.
- **Resumability.** A full sweep across a real corpus's worth of topics is
  a long-running process. This project's own `CachedEmbedder`
  (`src/alexandria/index/embedder.py`) already establishes the pattern —
  its own docstring states cache entries are "durable across interrupted
  index runs," keyed by content hash, specifically so a long embedding run
  survives a restart without recomputing already-done work. This sweep
  should do the equivalent at the topic level: persist the fold state and
  last-completed topic periodically (your call on cadence — every topic
  is the simplest correct default; document if you choose otherwise), so
  an interrupted sweep resumes from its last checkpoint rather than
  restarting from scratch. This is a real operational property with an
  established precedent in this exact codebase, not a nice-to-have — name
  it explicitly in your report if you scope it down for time.

### 2.2 `scripts/run-phase2-sweep.py`

A thin CLI driver: load corpus, call `sweep.py`'s entry point, report
progress (topics processed / total, in the same style as
`calibrate-coverage-grader.py`'s `10/71` progress lines — reuse that
convention, don't invent a new one) and a final summary (pages emitted,
documents excluded with reasons, any topics that failed judging and were
not emitted per the repair loop's own bounded-failure behavior).

---

## 3. Constraints

1. **TDD.** Tests before implementation, suite green at every commit.
2. **Every test offline**: fake/stub `pipeline.py` and `clustering.py`
   calls (they're already independently tested in their own repos' test
   suites — this order's tests are about orchestration logic: fold
   correctness, exhaustive accounting, determinism, resumability — not
   about re-testing gather/synthesize/judge/repair or clustering
   themselves).
3. **Do not modify** `pipeline.py`, `clustering.py`, any of the three
   judges, `llm.py`, or anything under `index/`/`retrieval/`. If you
   believe you must, STOP and report why first.
4. Determinism: given the same corpus snapshot, the same cluster list, and
   the same scripted pipeline/clustering responses, `sweep.py` must
   produce the same sequence of decisions and the same final fold state
   every time.

---

## 4. THE TEST THAT MATTERS MOST

**Prove exhaustive accounting is airtight across a full sweep**, the same
standard `phase2-synthesis-core`'s work order set for single-page chunk
accounting: construct a scripted corpus where some documents cluster,
some don't, and assert every single one ends up accounted for — either
in a processed cluster or in a logged exclusion — with an off-by-one
(one document silently missing from both) causing a hard failure, not a
warning.

Second: **prove resumability actually resumes**, not just that a
checkpoint file gets written. Interrupt a scripted sweep partway through,
restart it, and assert it picks up from the checkpoint rather than
re-processing already-completed topics (which would also violate the
cross-page-redundancy-avoidance the fold state exists for).

---

## 5. Out of scope

**Dedup clustering's execution** — `phase2-clustering` decides *what*
dedup action applies to a cluster; actually applying `merge`/`update`
against corpus files is not this order's job either (same exclusion as
that work order's own §5). Any consumer-facing trigger for running a
sweep (a cron job, a webhook, a UI button) — this order provides the
mechanism and a CLI to invoke it manually, not a scheduler. Partial/
incremental sweeps that only process new documents since a prior run — a
real, likely-valuable future optimization, but a different scope; this
order's job is proving the full, exhaustive sweep works correctly, once.

---

## 6. Verification

```bash
.venv/bin/python -m pytest tests/ -q
.venv/bin/python scripts/precommit-scan.py --all
```

A real end-to-end run against the real corpus is expensive (hundreds of
LLM calls across gather/synthesize/judge for every real topic cluster) —
do not run one as a matter of course. Instead, run against a small, real
subset (e.g., corpus-filtered to the 8 documents/clusters already in
`golden-synthesis-v1.jsonl`, so the topic-cluster count is small and known)
and report those real numbers, same honesty standard as every other
calibration result in this project: if it's worse than the standalone
`pipeline.py`/`clustering.py` numbers predicted, say so.

---

## 7. Report back

- Modules built + test counts.
- Proof exhaustive accounting and resumability can both demonstrably fail
  (§4) — which test, what it asserts.
- The real small-scale end-to-end run's numbers (§6).
- Whether `pipeline.py`'s and `clustering.py`'s actual merged interfaces
  matched what this document assumed in §1/§2, and what you had to adjust
  if not.
- Any spec deviation, and why.
