# SPEC — data model and ambient capture

Status: draft, pre-review. Supersedes nothing; extends
`SPEC-write-path-and-serve.md` (shipped) and absorbs
`SPEC-versioning-and-supersession.md` (written, unbuilt) as its §3.2.

The write-path package made Alexandria *fast and safe to write to*. It did not
make anything *use* it. This package targets the two gaps that remain: a data
model poorer than the one we already replaced, and a capture path that depends
on a human remembering to invoke it.

---

## 1. Why — the measurements this package answers to

### 1.1 The author did not use it, on the day he built it

The query log for 2026-08-12 records 442 queries. Their real composition:

| Source | Count | What it actually was |
|---|---|---|
| Golden-set eval | ~430 | pre-commit gate, 49 questions × ~9 commits |
| `serve` canary | 5 | acceptance smoke tests |
| Genuine retrieval | ~8 | all from the *previous* session |
| **This session** | **0** | — |

During a 13-hour session that audited, specced, and rebuilt Alexandria, its
author's agent issued **zero** retrieval queries against it, despite a standing
system-prompt instruction reading *"Before answering anything about past work,
decisions, or project history: `alexandria-search` first."*

Four causes, in order of force. All four are design inputs, not discipline
failures:

1. **Latency trained the caller out of it.** Measured the same day:
   `50,214ms`, `36,613ms`, `33,474ms`, `28,565ms` — and one query recorded as
   `ETIMEDOUT`. Against a 200ms `rg` that answers the same question with
   certainty, the local decision to skip Alexandria is *correct* every time.
   The aggregate of correct local decisions is a system nobody uses.
2. **The corpus structurally cannot answer questions about the session in
   progress.** In-session knowledge is most of what a working session needs,
   and none of it is indexed until distillation runs. Discipline cannot fix
   this; only cadence can.
3. **The trigger is prose, not structure.** `memory_search` *was* called
   unprompted the same session, because it arrives as a structured policy block
   with explicit "use when" conditions. Alexandria's doctrine arrives as prose.
   A doctrine that competes with a cheaper habit loses regardless of wording.
4. **The caller routed around it to the substrate.** Session history was
   recovered by reading `~/.pi/agent/sessions/*.jsonl` directly and by
   `git log` — the same data Alexandria indexes. Any agent with filesystem
   access will beat the index for local facts, and should.

**Consequence for this spec:** ambient capture is not a convenience feature. It
is the product. Confirmed empirically, on the author, at maximum motivation.

### 1.2 The data model is measurably poorer than the one it replaced

A prior local memory system — decommissioned by choice on 2026-08-03, referred
to throughout as **the predecessor** (concrete paths in the private companion
note) — is **already in the corpus**: 14,498 documents under
`sources/<predecessor>/`. It arrived through a lossy path:

```
predecessor SQLite store → graph projection → Alexandria
generated.by: connector/graph
```

What survived, and what did not, measured against the live predecessor store
(12,444 observations):

| Field in the predecessor | In `sources/<predecessor>/` |
|---|---|
| `title`, `project`, `source_id` | preserved |
| 8-way `type` | **degraded to an unindexed tag**, vocabulary drifted (`discovery` → `kind/insight`) |
| `files_read`, `files_modified` | **0 of 14,498 documents carry either** |
| `prompt_number` | lost |
| `generated_by_model` | lost |
| `facts[]` / `narrative` structure | flattened into body prose |
| `content_hash` | present as `hash` / `source_hash` |

Surviving tag distribution in a 3,000-document sample:
`kind/insight 2013 · feature 335 · change 254 · decision 200 · bugfix 188 ·
refactor 10`. `security_alert` and `security_note` do not appear.

So the corpus contains the predecessor's *content* with its *structure*
stripped. This is the "better data model, worse retrieval / worse data model,
better retrieval" verdict, made concrete and local.

**It is also the cheapest possible validation corpus**: 12,444 correctly-typed,
file-linked, prompt-linked observations sitting in a read-only SQLite file on
this machine. A direct connector recovers all of it.

### 1.3 Storage hygiene is degrading and nothing watches it

```
chunks.lance:  74 fragments · 75 versions · 16 deletion files
data:          2.6G   (2.2G earlier the same session — +400MB from upserts)
largest fragment: 168,251,786 bytes
compaction runs: none — compact_files() and cleanup_old_versions() are never called
```

Defensible at weekly cadence. **Not defensible once every session is a write.**

### 1.4 Duplicate capture is already measurable

The same fact, retrieved twice, from two connectors, in one query:

```
rank 1  sources/pi-sessions/...coverage-flagged-for-stocks...   0.999023
rank 2  sources/inbox/...market-data-service-s-coverage...      0.998535
```

Remembered by hand *and* extracted by the distiller. Ambient capture multiplies
this. The predecessor carries `content_hash` with **0 duplicates across 12,444 rows**;
we have no equivalent guard.

### 1.5 There is no deletion path at all

`--corrects` is written at `cli.py:209` and **never read** — zero references in
`retrieval/` or `synthesis/`, and it is absent from both `SCALAR_FIELDS` and
`ALL_FIELDS`, so retrieval cannot see it. Nothing that enters the corpus can
leave it.

This is why the build order in §11 is not the order the ideas were raised in.

---

## 2. Decisions

### D1 — Keep LanceDB for vectors; add SQLite for the relational model. Do not migrate to Chroma.

The question was raised as "LanceDB or Chroma". The answer is that Chroma's real
advantage is *not its vector store* — it is that its **metadata lives in SQLite**,
where schema evolution is free and typing is dynamic. We can have that without
giving up anything, because we already run four SQLite files.

What LanceDB buys that Chroma would cost us:

- **`prefilter=True`** — metadata filtering executes *before* the vector scan in
  the same columnar engine. It is the one piece of the reference RAG
  architecture we implement correctly, and it exists only because filterable
  scalars are colocated with vectors.
- **Native deletion (`_deletions/`)** and **native versioning (`_versions/`)** —
  §3.2 and §7 both depend on these.
- **`add_columns()`** (verified on lancedb 0.37.1) — scalar columns backfill
  without re-embedding.
- Faster scans at 46k chunks; measured k=10 → 75.7ms exact KNN.

What LanceDB genuinely costs, and how each is handled:

| Drawback | Handling |
|---|---|
| Arrow type inference freezes a column's type from the first batch — cost us an outage this session, surfacing as the opaque `LanceError(IO): Execution error: Spill has sent an error` | explicit `_lance_schema()` on every table (**already fixed**); §10 gate D1 pins it |
| Schema change friction (`index schema predates enrichment columns; run --rebuild`, `store.py:49-59`) | `add_columns()` for scalars; full rebuild accepted for vector-affecting changes, run overnight |
| Fragment and version accumulation, no compaction | §4 makes compaction scheduled and gated |
| Dependency weight (pyarrow + Rust ext) | accepted; already paid |

**Migrating to Chroma would cost a full re-embed of 46k chunks, lose prefilter,
lose native tombstones, and buy only dynamic metadata typing — which §3.3 gives
us for free.**

### D2 — Split metadata by access pattern, not by convenience

The rule, and it is mechanical:

> **If a field narrows a vector search, it lives with the vectors. If it
> describes or links a record, it lives in SQLite.**

**LanceDB (`SCALAR_FIELDS`)** — filterable, stable, small:
```
doc_id · chunk_id · layer · status · source · project · generated_at · obs_type
```

**SQLite (`corpus.sqlite`, new)** — rich, evolving, joinable:
```
entity_id · entity_rev · supersedes · deleted · content_hash
files_read · files_modified · prompt_number · concepts · entities
generated_by_model · agent_type · agent_id · relevance_count
```

`obs_type` crosses into LanceDB deliberately: it is a filter dimension
(`--type decision`), not a description.

### D3 — Typed observations use a closed vocabulary enforced in code

Adopt the predecessor's eight, with one rename for accuracy:

```python
OBSERVATION_TYPES = frozenset({
    "discovery", "decision", "bugfix", "change",
    "feature", "refactor", "security_alert", "security_note",
})
```

In the predecessor, `type` is plain `TEXT` with an index and **no constraint** — its
clean 8-value distribution across 12,444 rows is prompt discipline, not schema
enforcement. Ours is validated at write and rejects unknown values, in the same
place and manner `_reject_inbox_injection` already rejects malformed fields.
Without enforcement the vocabulary drifts to `bug`/`bugfix`/`bug-fix`/`fix` and
the filter silently stops meaning anything — a drift already observable in the
graph projection, where `discovery` became `insight`.

### D4 — Append-only versioning. Never mutate a record in place.

Adopt the *shape* of the predecessor's `sync_entity_heads`:

```sql
CREATE TABLE entity_heads (
  entity_id        TEXT PRIMARY KEY,
  kind             TEXT NOT NULL CHECK (kind IN ('observation','summary','prompt','inbox')),
  entity_rev       INTEGER NOT NULL,
  operation_sha256 TEXT NOT NULL,
  deleted          INTEGER NOT NULL CHECK (deleted IN (0,1)),
  updated_at_epoch INTEGER NOT NULL
);
```

**With an explicit caveat that must not be lost: this schema is unproven.** In
the source system all three sync tables hold **0 rows**, and **12,437 of 12,444
observations never left `entity_rev=1`**. Zero tombstones were ever written. It
is a validated *design*, not a validated *implementation*. We copy the shape and
do our own proving.

Rejected: in-place mutation. It destroys the record of what the system believed
at a past moment, which is the actual audit question — *"what did Alexandria
tell the person who made that decision?"*

### D5 — Resolution happens at read time, reusing machinery that exists

Collapsing an `entity_id` to its newest live revision is the same operation as
the synthetic-chunk collapse already running at `retrieval/search.py:198-229`.
Extend it; do not write a second collapser. That code path is well understood —
it is where this session's scoring bug was found and fixed.

### D6 — Ambient write is mandatory; ambient read is optional and degradable

Per §1.1, capture that depends on invocation does not happen. Capture becomes
automatic.

Retrieval injection is different: **if `serve` is down, the session proceeds
normally with no error and no delay.** Alexandria is never a hard dependency of
starting work. The probe is short-timeout, the failure is silent, the fallback
is "no injected context" — exactly the posture the extension already takes when
`tryServe` returns null.

### D7 — Distillation routes through the model gateway on a cheap bulk model

Split bulk from interactive (BACKLOG #31, ~20 lines, `--base-url`/`--api-key-env`
already landed in `da2993d`). The distiller is latency-tolerant and
high-volume; the answer path is quality-critical and rare. Atlas remains
available as redundancy, but the default route is the model gateway under Alexandria's
own key so the cost ledger attributes correctly.

### D8 — A direct predecessor connector, read-only

The predecessor's store is a static, read-only, 320MB SQLite file that will
never change again. Reading it directly recovers `type`, `files_read`,
`files_modified`, `prompt_number`, `generated_by_model` and structured `facts[]`
for 12,444 observations — data that exists nowhere else, since the graph
projection dropped it.

This is not primarily a migration. **It is the acceptance corpus for the entire
typed data model**: real data, at volume, with ground truth already assigned.

---

## 3. Phase 1 — the data model

### 3.1 Typed observations

`connectors/pi_sessions.py`'s distiller prompt gains a required `type` field
constrained to D3's vocabulary, with the type definitions stated once and
concretely. Unknown or missing type → the observation is rejected and logged,
never silently defaulted (a default would recreate `discovery`-as-catch-all,
which is 56% of the predecessor's corpus).

`files_read` / `files_modified` are extracted **deterministically from tool-call
events in the transcript**, not asked of the model. The transcript already
records every `read`/`edit`/`write` target; asking an LLM to recall them invites
fabrication, and this is exactly the class of field where a plausible wrong
answer is worse than none.

`prompt_number` comes from burst position — already tracked by `segment_bursts`.

### 3.2 Versioning, supersession, tombstones

Absorbs `SPEC-versioning-and-supersession.md`.

- Every document gains a stable `entity_id` and a monotonic `entity_rev`.
- Migration is mechanical: `entity_id := doc_id`, `entity_rev := 1`.
- A correction appends `rev+1` carrying `supersedes`; the prior revision stays
  on disk and stays auditable.
- `--corrects` becomes load-bearing: written **and read**, indexed, honoured at
  retrieval. Today it is written and ignored.
- `deleted=1` is a tombstone: the record stops being retrievable and the chunks
  are removed from LanceDB (`_deletions/`) and FTS5, while the head row and the
  audit trail survive. **Erasure of source files, git history, and the audit
  trail is Phase 5 and needs a policy decision (§9 Q1).**
- `--as-of <date>` answers "what did we believe then", using the already-indexed
  `generated_at`.

### 3.3 The SQLite relational store

New `corpus.sqlite` alongside the existing four. WAL, `busy_timeout`, one
connection helper, same pattern as `bm25.py` and `monitor.py`.

**This extends §4.2.1's write-ordering contract from four stores to five.** That
is the real cost of this phase and it is not small: the ordering argument is what
holds the write path together, and every crash-recovery test (`test_w3a`, five
SIGKILL points) gains a step. Proposed position — after FTS5, before the
generation bump, on the same grounds as FTS5: it is derived state that must be
present before any reader can observe the new generation.

### 3.4 Deduplication

`content_hash` on every record, `UNIQUE` where the source guarantees it.
Ingestion checks the hash before writing. Targets the measured §1.4 duplicate
and the ambient-capture amplification of it.

### 3.5 Field-weighted lexical search

FTS5 gains separate `title` / `heading_path` / `text` columns so `bm25()` can
take per-column weights, matching the predecessor's `observations_fts(title,
subtitle, narrative, text)`. Today we index one flat blob and cannot express
that a title match outranks a body match. Small change, lands naturally with the
typed model.

### 3.6 The predecessor connector

`connectors/predecessor.py`, read-only over the predecessor's store.
Recovers the fields §1.2 shows were lost. Reconciles against the 14,498
already-present graph-projected documents by `content_hash` + `source_id`, upgrading
them in place as new revisions rather than creating a second copy of the same
knowledge — which would be a 12,444-document instance of the §1.4 defect,
introduced by the very phase meant to fix it.

---

## 4. Phase 2 — storage hygiene

Mandatory *before* write volume increases, per §1.3.

- `compact_files()` when fragment count crosses a threshold; `cleanup_old_versions()`
  with a retention window that must be long enough for `--as-of` to remain honest.
- Both run inside the §4.2 `flock`, never concurrently with a promote.
- Fragment count, version count, and on-disk size exposed on `/health` and
  logged per run, so growth is observable rather than discovered.
- Embedding-cache location resolved and documented — the configured path
  (`.alexandria/cache/embeddings.sqlite`) is 0 bytes while a ~4.59GB cache
  demonstrably exists. This blocks any honest backup or erasure scope.

---

## 5. Phase 3 — ambient write

### 5.1 The trigger

Pi exposes a full hook surface, verified present:

```
session_start · session_info_changed · agent_start · after_provider_response
message_end · tool_execution_end · session_before_compact · session_shutdown
project_trust · pi.events.on("subagents:*")
```

and a **working precedent already on disk**: `~/.pi/agent/extensions/
kg-sync-trigger.ts.disabled` fires on `session_shutdown` and spawns a detached
background sweep. This is the same pattern the predecessor used, via its own
harness's hooks.

So ambient capture is **not a new subsystem**. Capture, telemetry-stripping,
burst segmentation, substance gating, distillation, promotion and indexing are
all built and tested. The gap is the trigger.

`session_shutdown` → detached `alexandria sync pi-sessions && alexandria promote`.
Never blocking, never holding the session open, failures logged not raised.

### 5.2 The idle gate

A burst distilled mid-work fragments one decision across partial captures — and
because documents are immutable, that fragmentation is permanent. Distil a
session only once it has been idle past a threshold, or on explicit shutdown.

### 5.3 Cost

Every distilled burst is an LLM call. The usage ledger (`model`,
`prompt_tokens`, `completion_tokens`, `cache_read` per id) already exists, so
this is measurable rather than estimated.

**Ship measurement before autonomy**: run ambient capture for one week with the
ledger on, audit actual spend, then set a bound. A per-day token ceiling that
skips distillation (and leaves the burst unconsumed for retry) rather than
failing. Routing per D7.

### 5.4 Second-harness capture

The second harness already reaches the corpus without a bridge: its memories
project to the graph vault, Syncthing carries them, the graph connector
ingests them — 1,366 documents already present. Ambient write there needs
nothing new. Only the *read* path needs `serve`, which exists and was proven
cross-host on 2026-08-12.

---

## 6. Phase 4 — ambient read

Last, because it depends on `serve` being a supervised daemon and on §1.1's
latency finding being closed.

- `serve` under launchd, restarted on failure, model held warm. Today it is
  started and stopped by hand.
- `session_start` hook probes `/health` with a short timeout. Unreachable →
  session proceeds silently with no injected context and no delay (**D6**).
- Reachable → a bounded, relevance-floored context block, scoped to the current
  project. Below the floor, inject nothing: irrelevant chunks polluting reasoning
  on an unrelated task is worse than no memory at all.
- Injection is recorded in the query log with a distinct `client`, so its cost
  and its hit rate are measurable from day one and the §1.1 audit can be re-run
  against it.

---

## 7. Phase 5 — erasure

Blocked on §9 Q1. Tombstones (§3.2) remove a record from *retrieval*; erasure
removes it from *existence*. A document survives in: `sources/*.md`,
`chunks.lance`, `fts.sqlite`, `enrichment.sqlite`, `queries.sqlite`
(`retrieved_ids`, which is also the learning signal), corpus git history, `wiki/`
pages, the response cache, and the embedding cache. Real erasure crosses all of
them, and git history means rewriting or crypto-shredding.

---

## 8. Deliberately not built

- ANN indexing (IVF/HNSW/PQ). Re-entry at flat scan >250ms at working k,
  ≈150–200k chunks. Currently 46k.
- Migration to Chroma, Qdrant, Milvus or any vector server (**D1**).
- Multi-tenant scope objects and per-tenant generation counters. Trigger
  unchanged: the first customer requiring intra-organization ACLs.
- Online RL or bandit tuning. Exploration means deliberately serving worse
  results to real users.
- Automatic supersession detection. A model deciding which prior belief a new
  statement invalidates is a correctness risk with no undo, and there is no
  deletion path to recover from a wrong call.
- Merge/branch CRDT semantics for concurrent revisions.

---

## 9. Open decisions — these need Stanley

- **Q1 — erasure scope.** Does deletion stop at the retrievable surface
  (tombstone: gone from search and synthesis, present in git and audit), or does
  it include source files, git history, and the audit trail? The first is a
  weekend; the second needs history rewriting or crypto-shredding, and it
  trades away the immutable audit trail that is the product differentiator.
  **Phase 5 cannot start without this answer.**
- **Q2 — ambient-read scope.** Project-scoped by cwd, or global? Project-scoped
  is more precise and is the tenancy boundary rehearsal; global catches
  cross-project transfer, which is a real part of the value.
- **Q3 — retention window for `cleanup_old_versions()`.** Directly bounds how
  far back `--as-of` can honestly answer.
- **Q4 — does ambient capture cover subagent sessions?** `pi.events.on("subagents:*")`
  exists and subagents do substantial work, but their transcripts are numerous
  and often narrow. Defaulting to no.

---

## 10. Acceptance gates

Every gate is a test, and every test must fail against a deliberately broken
implementation. Written before the code, per the discipline that caught three
vacuous tests in the write-path package.

**Data model**
- **D1** A LanceDB table created with an all-empty column and later written with
  values does not raise; the explicit schema holds the declared type.
- **D2** A field classified as SQLite-side is absent from `SCALAR_FIELDS`; a
  filterable field is present in both, and a filtered query still uses
  `prefilter=True`.
- **D3** An observation with `type="bug"` is rejected at write with a
  diagnosable error and nothing is persisted.
- **D4** A correction creates `rev=2`; `rev=1` remains on disk and is retrievable
  via `--as-of` a date before the correction.
- **D5** A tombstoned document is absent from search, absent from FTS5, absent
  from synthesis, and its head row and audit rows still exist.
- **D6** Read-time resolution returns exactly one revision per `entity_id` and
  measurably does not regress p50 on the real corpus.
- **D7** Ingesting the same fact twice via two connectors produces one record.
- **D8** A title match outranks an equally-scoring body match.
- **D9** The predecessor connector recovers `type`, `files_read`,
  `files_modified`, `prompt_number` for a known observation, and upgrades the
  existing graph-projected document rather than creating a duplicate.
- **D10** Crash injection at each of the now-five write-ordering steps
  reconverges from a cold reopen, with all five stores agreeing.

**Storage**
- **H1** Compaction reduces fragment count and leaves every chunk retrievable.
- **H2** `--as-of` inside the retention window resolves; outside it, fails loudly
  rather than answering wrongly.
- **H3** `/health` reports fragment count, version count, and on-disk size.

**Ambient write**
- **A1** A session ending produces indexed, retrievable observations with **no
  human invocation** — the literal §1.1 defect, inverted.
- **A2** A session ending mid-burst is not distilled until the idle threshold.
- **A3** A distillation failure leaves the burst unconsumed and is recorded, not
  swallowed.
- **A4** Every distillation writes a ledger row attributable to the bulk model.
- **A5** Exceeding the daily token bound skips distillation and leaves work
  pending; it never fails a session or drops a burst.

**Ambient read**
- **R1** With `serve` down, a session starts normally — no error, no added
  latency beyond the probe timeout, no injected context.
- **R2** With `serve` up, relevant context is injected and recorded under a
  distinct `client`.
- **R3** Below the relevance floor, nothing is injected.
- **R4** Re-running the §1.1 audit after a week shows a non-zero, attributable
  in-session query count.

---

## 11. Build order — and why it is not the order these were raised in

The ideas arrived as: ambient capture, then typed observations, then versioning.
**The dependency runs the other way, and the reason is §1.5.**

There is no deletion path. Turning on ambient capture first would generate
permanent, unremovable records at machine speed. The predecessor is the warning,
in its own numbers: **7,029 `discovery` observations accumulated, and not one
tombstone ever written** — with ambient capture running the entire time, and the
deletion machinery already built. Our position would be worse: the capture
without even the machinery.

1. **Phase 1 — data model.** Types, `entity_id`/`entity_rev`/`deleted`, the
   SQLite split, read-time resolution, dedup, field-weighted FTS5. Validated
   against the predecessor corpus (§3.6): 12,444 real typed observations.
2. **Phase 2 — storage hygiene.** Compaction, retention, observability. Must
   precede any increase in write volume.
3. **Phase 3 — ambient write.** The `session_shutdown` trigger, idle gate, cost
   measurement week, then the bound.
4. **Phase 4 — ambient read.** `serve` under launchd, optional degradable
   injection, then re-run the §1.1 audit as the real acceptance test.
5. **Phase 5 — erasure.** Blocked on Q1.

Each phase is independently shippable and safe to stop at. Reversing 1 and 3
produces a corpus we cannot clean.
