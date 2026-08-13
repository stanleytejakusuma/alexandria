# SPEC — data model and ambient capture

Status: revised after two adversarial review rounds; review budget spent.
Supersedes nothing; extends `SPEC-write-path-and-serve.md` (shipped) and absorbs
`SPEC-versioning-and-supersession.md` (written, unbuilt) as its §3.2.

**Round 1** found three defects that changed the design: the write-ordering
justification was factually false (D4b), the deduplication mechanism could not
catch its own motivating example (§3.4), and the headline acceptance gate passed
by construction (R4). Six further gates were vacuous as written.

**Round 2** reviewed round 1's *fixes*, which had been written by the same author
who made the original errors and had never themselves been examined. It found
that the largest of them, D4a, was half right: the durability half genuinely
fixed the replay bug, while the availability half left tombstones failing **open**
and revisions overwriting each other on disk. It also found that the published
relevance-floor measurement was computed against the wrong score, and that a
document making capture fully automatic contained **no policy on secrets** — in a
corpus with no deletion path.

Every correction is recorded in place, with the wrong claim left visible beside
it. A spec that quietly fixes its own errors teaches the next reader nothing, and
two of the three round-2 findings exist precisely because a confident earlier
claim went unchecked.

**Two rounds is the cap.** Remaining risk is carried as named requirements and
provisional gates (R3, H2), not as further review.

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

**What this does and does not establish.** It is one adverse day, n=1, and the
sample is *maximally unfavourable to Alexandria*: the session was **building
Alexandria**, where grepping the repo genuinely is the correct tool (cause 4),
against a corpus that had been **frozen since 2026-08-08** (cause 2 artificially
maximal). A fairer week would show a higher number. Calling this "confirmed
empirically" — as the first draft did — overstates it.

What it does establish is narrower and still decisive: **invocation-dependent
memory does not get invoked**, even by its author, even under a standing
instruction, even on the day he is most motivated. That supports making capture
automatic. It does *not* by itself prove capture is more valuable than a faster
read path — causes 1 and 2 argue for the read path, and Phase 4 is where they
are answered.

The honest summary: four causes, jointly sufficient to justify the direction of
Phases 3 and 4, not sufficient to declare either one "the product" in advance of
R4 measuring it.

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

- **`prefilter=True`** — metadata filtering executes *before* the vector scan.
  Worth keeping, but the first draft oversold it: with `_indices/` empty every
  query is already an exact scan, so prefilter saves distance computation on
  filtered-out rows, not an index traversal. Bounded by the measurements in
  §1.3, the ceiling is ~180ms (257.5ms at k=200 versus 75.7ms at k=10). Its
  durable value is guaranteeing k results under a selective filter without
  over-fetch heuristics — convenience, not the decisive argument.
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

### D4 — Append-only versioning, with revision state in frontmatter

The versioning fields live in the **document's own frontmatter**, and
`corpus.sqlite` is a rebuildable index over them (see D4a). Adapted from the
predecessor's `sync_entity_heads`:

```sql
-- a PROJECTION of sources/*.md frontmatter, not a source of truth
CREATE TABLE entity_heads (
  entity_id        TEXT PRIMARY KEY,
  kind             TEXT NOT NULL CHECK (kind IN ('observation','summary','prompt','inbox')),
  entity_rev       INTEGER NOT NULL,   -- deliberate change: predecessor's is TEXT
  supersedes       TEXT,               -- entity_id#rev this revision replaces
  deleted          INTEGER NOT NULL CHECK (deleted IN (0,1)),
  updated_at_epoch INTEGER NOT NULL
);
```

Two deliberate departures from the predecessor's schema, stated because the
first draft made one of them silently:

- `entity_rev` is `INTEGER`; the predecessor's is `TEXT NOT NULL`. We order and
  compare revisions, so the type should permit it.
- `operation_sha256` is **dropped**. The first draft copied it with no producer
  and no consumer defined anywhere in this spec — which is cargo cult, not
  design. If a content digest is wanted later it is `content_hash` (§3.4),
  which has a stated purpose.

**The caveat that must not be lost: this schema is unproven upstream.** All three
of the predecessor's sync tables hold **0 rows**, **12,437 of 12,444 records
never left rev 1**, and zero tombstones were ever written. It is a validated
*design*, not a validated *implementation*. Copying the shape is only defensible
because gates D4/D5/D10 exercise precisely what was never exercised there.

Rejected: in-place mutation. It destroys the record of what the system believed
at a past moment, which is the actual audit question — *"what did Alexandria
tell the person who made that decision?"*

### D4a — `corpus.sqlite` is a projection, never authoritative

**This is the decision the first draft hid in one line, and getting it wrong is
expensive.** The question: does the revision graph live in the database, or in
the documents?

It lives in the **documents**. `entity_id`, `entity_rev` and `supersedes` are
frontmatter fields on the markdown in `sources/`; `corpus.sqlite` is an index
built from them, rebuildable at any time by re-walking the corpus — exactly as
LanceDB and FTS5 already are.

Three things fall out, and each fixes a real defect in the first draft:

1. **It leaves the write-ordering contract entirely.** A projection cannot be
   "out of sync" in a way that loses data; it can only be stale, and stale is
   repaired by rebuilding. §4.2.1 stays a four-store contract. The first draft
   proposed a fifth store on a justification that is factually false (D4b).
2. **Revision assignment becomes content-derived, not read-modify-write.**
   `entity_rev` is written into the document at authoring time, so replaying the
   pending marker after a crash re-reads the same value. The first draft's
   "append rev+1" was a read-modify-write against the head row: a replay landing
   after the database write would compute rev+1 a second time and produce rev+2
   for identical content. Idempotency under replay is what makes the existing
   crash-recovery contract work, and it was about to be broken.
3. **`backup.py`'s scope is unchanged.** §6 of the shipped spec backs up state
   and explicitly not rebuildable indexes. A projection is a rebuildable index.
   Had it been authoritative, its loss would destroy the revision graph and the
   backup scope would have had to grow to cover it.

**Two consequences the first version of this decision did not follow through,
found in review. Both are load-bearing and both are requirements on Phase 1.**

**(a) A tombstone must be enforceable without the projection, or it fails open.**
A frontmatter-only revision — a tombstone, or adding a `supersedes` link — leaves
the body text unchanged. `chunk_id` derives from `doc_id`, heading path and text
(`index/chunker.py`), so the new revision produces *identical* chunk ids, and
`store.upsert` overwrites in place. Neither `SCALAR_FIELDS` (`index/store.py`)
nor `METADATA_COLUMNS` (`index/bm25.py`) carries `deleted`, `entity_id` or
`entity_rev`. So on the shipped schema a tombstoned document is **still fully
retrievable**, and D5's read-time resolution could only exclude it by joining the
projection on every search — putting a rebuildable index on the critical read
path, where a missing or stale one **fails open and serves deleted content**.
That is the opposite of what a tombstone is for.

> **Requirement:** `deleted` and `entity_id` join `SCALAR_FIELDS` and
> `METADATA_COLUMNS` in Phase 1, so exclusion is enforced by a filter the
> retrieval path already applies (`prefilter=True`) and fails **closed**. The
> projection then accelerates resolution; it never authorises it. Adding these
> columns is the same schema change §3.5 already requires, so the cost is shared,
> not new — but it means the tombstone work cannot be sequenced after the FTS
> rebuild, it must ride along with it.

**(b) Two revisions of one entity collide on disk and the older is destroyed.**
`source_filename()` (`corpus.py`) is deterministic on `(source, source_id,
title)`, and `Doc.write` is an unconditional `write_text`. Two revisions of the
same entity with an unchanged title therefore resolve to the *same path*, and
writing rev 2 silently destroys rev 1 — which defeats D4's append-only guarantee
at the storage layer, in a system whose entire audit claim rests on it. The
repo already names this hazard: `corpus.py`'s docstring advertises `body_hash`
as "the immutability tripwire", but its only non-test caller is `migrate.py` —
it is documented, not wired.

> **Requirement:** revision documents are path-disjoint — the filename carries
> `entity_rev`. Gate D4 must assert two revisions coexist *as files*, not merely
> as rows. A test that writes rev 2 and reads back a revision graph from the
> projection passes while rev 1 is already gone.

### D4b — The generation counter gates caches only

Recorded because the first draft asserted the opposite and built on it.

The claim was that `corpus.sqlite` must be written before the generation bump
because it "must be present before any reader can observe the new generation."
**That is false.** In `retrieval/search.py`, `generation` is read once (line 130)
and used once (line 140), in the query-cache key. An **uncached** query never
consults it at all — it reads LanceDB and FTS5 live.

So ordering a store relative to the bump protects nothing about that store's
visibility. The bump's real and only job is cache invalidation, which is why
§4.2.1 places it after the two stores whose contents a cached answer could
otherwise misrepresent. D4a removes the need for this ordering question to have
an answer at all.

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

### 3.3 The SQLite projection

New `corpus.sqlite` alongside the existing four. WAL, `busy_timeout`, one
connection helper, same pattern as `bm25.py` and `monitor.py`.

Per **D4a it is a projection of frontmatter, so it does NOT join §4.2.1's
write-ordering contract.** It is rebuilt from `sources/` on divergence, like any
other derived index. The write path stays a four-store contract and `test_w3a`
keeps its five SIGKILL points rather than gaining a sixth.

One consequence to design for rather than discover: **resolution must batch its
lookups.** D5 collapses by `entity_id`, and those keys live in a different store
from the vectors. `retrieval/search.py:218` and `index/store.py:119` both carry
the same warning from a measured regression — per-candidate lookups cost
"~494ms of pure overhead per query (up to 40 full table scans)", fixed by
batching. Resolution issues **one** keyed query per search, never one per
candidate.

### 3.4 Deduplication — at the source, not only at the hash

The first draft specified `content_hash` alone and claimed it "targets the
measured §1.4 duplicate." **It does not, and §1.4's own evidence shows why:**
those two documents have different slugs, different sources and different scores,
meaning different text — therefore different hashes. Exact-hash dedup catches
zero instances of that class. The predecessor's "0 duplicate `content_hash`
across 12,444 rows" proves only that it never wrote identical bytes twice; the
first draft quoted it as though it proved dedup worked.

Ambient capture makes the gap worse, because distillation is a nondeterministic
LLM call. The **same burst** distilled twice — two sessions ending together, a
retried failure, a crash-replayed marker — produces different wording each time
and sails past any hash.

So deduplication is enforced where it is decidable:

1. **Source-level idempotency (primary).** Each burst has a stable id. A burst is
   distilled **at most once**. This is the only mechanism that catches the
   nondeterministic-redistillation case, because it never invokes the model a
   second time.

   **Two corrections from review — the first draft described a mechanism the
   shipped code does not implement.**

   **The current burst id is not stable.** `connectors/pi_sessions.py` derives
   `burst_id` by hashing every message's role and text. An **open session gaining
   one more turn therefore produces a different id**, misses the `seen` check, and
   is redistilled — recreating exactly the §1.4 defect this clause exists to
   prevent. §5.1.1's periodic sweep makes it *more* likely, because it deliberately
   runs against sessions that are still live.

   > **Requirement:** derive `burst_id` from `(session path, first-message
   > timestamp, window ordinal)` — identifiers fixed at the moment a burst opens
   > and invariant to anything appended after. Gate D7 must distil a burst, append
   > a turn, and re-distil, asserting one set of observations.

   **Consumption is recorded *after* the writes, not before.** The first draft
   said before. `commit()` in `connectors/pi_sessions.py` records after, and its
   docstring gives the reason: a failed distillation must leave the burst
   unconsumed. Gate A3 requires that behaviour too — so the first draft's ordering
   contradicted both the code and its own gate. Recording before would convert
   every transient model failure into permanent silent data loss, which is far
   worse than the duplicate it prevents.

   > **Resolution:** consumption is recorded **after** the writes. Fail-safe beats
   > fail-clean here because the corpus has no deletion path but a burst can
   > always be redistilled. The duplicate window this leaves is closed by the
   > stable id above, not by the ordering.
2. **`content_hash` (supplement).** Cheap, `UNIQUE` where a source guarantees
   it, and genuinely useful for byte-identical re-ingestion — the
   re-running-a-connector case. Retained, but no longer load-bearing.
3. **Semantic near-duplicates across connectors** (the literal §1.4 pair) are
   **not** solved here and this spec does not claim to solve them. Detecting
   them requires a similarity threshold, and a wrong threshold silently discards
   real knowledge in a corpus with no deletion path. Deferred, deliberately, and
   recorded in §8.

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
them as new revisions rather than creating a second copy of the same
knowledge — which would be a 12,444-document instance of the §1.4 defect,
introduced by the very phase meant to fix it.

#### 3.6.1 This migration is itself the hazard §11 warns about

**Stated plainly because the first draft did not notice it.** §11 orders the
phases to avoid machine-speed irreversible writes to a corpus with no deletion
path — and then puts a **14,498-document bulk write inside Phase 1**, before
Phase 2's hygiene work, with no dry-run and no rollback. The argument
contradicted itself.

The migration therefore runs under its own protocol, and none of it is optional:

1. **Dry-run first, and it is the default.** `--dry-run` reports counts and a
   sample of proposed mappings and writes nothing. The real run is opt-in.
2. **Measure the mapping's error rate before trusting it, on a sample big enough
   to mean something.** Gate D9 checks one known observation — that proves the
   connector *can* map, not that it maps *correctly at volume*. Hand-check a
   random sample of 100 reconciliations and record the observed precision in the
   commit. A wrong `content_hash` + `source_id` match silently attaches a
   revision to the wrong document, which is worse than a duplicate: a duplicate
   is visible, a misattached revision looks like history.
3. **Corpus git commit immediately before the run**, so the document layer is
   recoverable by `git revert` even though the corpus has no deletion path. This
   is the only rollback that exists today, and it works because D4a keeps the
   revision graph in the documents.
4. **Batch, with the write lock, resumable.** Reuses the pending-marker
   mechanism: a batch that dies mid-run is replayed, not restarted.
5. **Phase 2 hygiene runs before this, not after.** Adding ~14.5k revisions to a
   store with 74 uncompacted fragments and no retention policy is how the
   §1.3 growth curve becomes a problem. See the amended ordering in §11.

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

### 5.1.1 The shutdown hook is not sufficient on its own

**A hook that only fires on clean shutdown captures nothing from the sessions
most worth capturing.** SIGKILL, a kernel panic, a battery dying, or a laptop
sleeping and never waking the process all end a session with no
`session_shutdown` — and this runs on a laptop. The precedent at
`kg-sync-trigger.ts.disabled` is shutdown-only too, so it inherits the same hole
rather than solving it.

The hook is therefore an **optimisation for the common case**, not the mechanism.
The mechanism is a **periodic catch-up sweep** that distils any burst which is
idle past the §5.2 threshold and not yet consumed, regardless of how its session
ended. It is the same code the hook invokes, on a timer.

Which process runs it must be named, not implied: **a launchd timer**, not
`serve`. `serve` is explicitly not required to be a 24/7 daemon (§5.7 of the
shipped spec), so hanging capture off it would make capture depend on a
liveness property that spec declines to promise. The sweep takes the §4.2 write
lock and skips cleanly when held, exactly as the drain does.

Gate A6 covers this: a session killed with SIGKILL, leaving no shutdown hook, is
still captured by the next sweep.

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

### 5.5 Secrets and third-party content — the highest-risk part of this package

**Added in review round 2. The first draft omitted this entirely, which was the
single most dangerous gap in it.**

Everything above this line makes capture automatic. Session transcripts contain
pasted credentials, API keys, tokens, private client material, and personal
information about third parties who never consented to being recorded. Today a
human decides what to write down and that judgement is the filter. Ambient
capture removes the human and keeps the filter's absence.

The compounding factor is §1.5: **there is no deletion path.** Automatic capture
and permanent storage are each defensible alone. Together, without a filter,
they are a machine for producing unremovable secrets at machine speed — and the
first time it matters, it will have been running for months.

Requirements, all in Phase 3 and none deferrable:

1. **Redaction before distillation, not after.** A high-entropy/known-prefix scan
   (`sk-`, `ghp_`, `xox`, `-----BEGIN`, JWT shapes, `Authorization:` headers)
   runs over burst text *before* it reaches the model. Redact-then-distil, so a
   secret never leaves the machine in a prompt and never reaches the gateway.
   The repo already has the pattern list: `.leakpatterns.local` and
   `scripts/precommit-scan.py`. **Reuse the existing scanner rather than writing
   a second one** — two scanners drift, and the one that drifts is the one nobody
   is committing against.
2. **Fail closed.** A burst whose scan errors is skipped, not captured. It stays
   unconsumed and is retried; §5.1.1's sweep makes skipping cheap.
3. **A path exclusion list.** Sessions under configured paths are never captured
   — the mechanism for client work under NDA, and for anything the operator
   simply does not want recorded.
4. **This is not a solved problem and the spec does not pretend otherwise.**
   Entropy scanning has false negatives; a secret in prose ("the password is
   hunter2") defeats it. The honest claim is that it removes the *mechanical*
   class of leak, not that it makes capture safe. Which is why 5.6 exists.

### 5.6 A kill switch, and the ability to inspect before it lands

- `ALEXANDRIA_AMBIENT=0` disables all automatic capture, checked at the top of
  every trigger path. One environment variable, no restart, no config file. A
  feature that writes permanently to a corpus with no undo **must** have an off
  switch that a person can reach in five seconds.
- Distillation output lands in `inbox/` as a pending entry, so the existing
  `alexandria promote` path applies and there is a window in which a capture can
  be inspected and dropped **before** it becomes an indexed document. This costs
  nothing to build — it is the shipped write path — and is the only review
  opportunity that exists before permanence.

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
- **Semantic near-duplicate collapse across connectors** — the literal §1.4 pair,
  one fact worded twice by a human and a distiller. §3.4 handles byte-identical
  and same-burst cases; this needs a similarity threshold, and a wrong threshold
  silently discards real knowledge in a corpus with no deletion path. Re-entry
  when a measured duplicate rate justifies the risk, and not before erasure
  exists to undo a bad call.
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
- **Q5 — what is the relevance floor? PARTLY RESOLVED 2026-08-13. A first
  answer was published here and was wrong; this records the correction.** The
  blocker was that nothing existed to validate a threshold against. A negative
  set now does: 22 hand-verified queries the corpus cannot answer
  (`.alexandria/golden/negative-v1.jsonl`), measured against the 49-entry golden
  set at 46,021 chunks.

  **The first measurement was computed wrongly.** `separation()` read
  `scores[0]` for every positive, but `hit` only means the target appeared
  somewhere in top-k — so for a hit at rank 3 it scored a document that was
  *wrong*. Corrected to `scores[rank-1]` (`src/alexandria/eval/negative.py`,
  regression test `test_a_positive_contributes_the_score_of_its_hit_not_of_the_top_result`):

  | | published | corrected |
  |---|---|---|
  | positive top-1 median | 0.9819 | 0.9785 |
  | positive **minimum** | 0.1190 | **0.0274** |
  | recall at a 0.4409 floor | 87.1% | **83.9%** |
  | recall at a 0.12 floor | 100% | **90.3%** |

  The median barely moved because 23 of 31 hits are at rank 1; the *minimum*
  collapsed. **The floor decision depends entirely on the minimum**, so the
  error fell exactly where it mattered.

  | | positive (31 hits) | negative (22) |
  |---|---|---|
  | top-1 median | 0.9785 | 0.0238 |
  | min / max | 0.0274 / 0.9985 | 0.0031 / 0.4409 |

  **The original decision of 0.12 rested on "retains 100%", which is false.** It
  retains 90.3% and still admits 2 of 22 negatives. The trade-off curve is flat:

  | floor | retained | known-bad admitted |
  |---|---|---|
  | 0.12 | 90.3% | 2 |
  | 0.20 | 87.1% | 2 |
  | 0.3374 | 83.9% | 2 |
  | 0.4409 | 83.9% | 1 |

  **The finding that survives — and it is stronger than the one it replaces.**
  All **five** positives below 0.4409 are `overlap_band: zero` (0.0274, 0.0547,
  0.1032, 0.1671, 0.3262) — queries sharing no vocabulary with their target,
  precisely the class semantic retrieval exists to serve and grep cannot. The
  weakest of them scores 0.0274, *below* the negative median of 0.0238's
  neighbourhood and beneath 8 of the 22 unanswerable queries.

  **Therefore: no score floor separates the zero-overlap band from unanswerable
  queries.** This is not a threshold that needs tuning; it is the wrong
  instrument. Any floor high enough to exclude confident nonsense also excludes
  the hardest real hits, and the band it taxes (33.3% recall — the system's
  weakest surface) is the one least able to afford it.

  **Decision:** specify **0.12** for Phase 4 ambient injection, on the narrow
  and now-explicit grounds that injected context is advisory and a wrong chunk
  is cheaper than a missing one — *not* on any claim of clean separation. Treat
  it as a starting value to be replaced by observed data, per Q2's
  measure-then-bound discipline. **Any surface where a retrieved result is
  acted on rather than read must not use a bare score floor at all** until a
  better instrument exists.

  **Still unresolved, and why R3 stays provisional:** 21 of the 22 negatives are
  out-of-domain brand queries (Kafka, Stripe, MongoDB). The realistic ambient-read
  failure is the *in-domain near-miss* — a query about this corpus's own subject
  matter that it happens not to cover — and the two negatives scoring highest are
  the two closest to in-domain. The 0.0238 median is partly an artefact of how
  easy the negative set is. ≥10 in-domain negatives are required before any floor
  is treated as validated.

  **Known decay:** a negative case asserts absence, and absence expires as the
  corpus grows — including from this session being distilled into it, which will
  add documents containing every term above. `verified_against` records the chunk
  count at verification time so staleness is visible; re-verification belongs
  with each golden-set review.

---

## 10. Acceptance gates

Every gate is a test, and every test must fail against a deliberately broken
implementation. Written before the code, per the discipline that caught three
vacuous tests in the write-path package.

**Data model**
- **D1** A LanceDB table created with an all-empty column and later written with
  values **reads the values back with the declared dtype intact**. "Does not
  raise" is vacuous — it passes against a table that accepts writes and returns
  nothing, which is the exact failure mode being guarded against.
- **D2** A filtered search over a corpus where the filter excludes most
  documents returns **only** matching documents and returns the full requested
  `k` when that many exist. Behavioural: asserting the shape of a config list or
  that a kwarg equals `prefilter=True` passes against a mock and proves nothing.
- **D3** An observation with `type="bug"` is rejected at write with a
  diagnosable error and nothing is persisted — asserted at **every** write path,
  including a connector `sync`, which does not route through `promote.py`. The
  spec must name the single chokepoint that enforces this; today `Doc.write`
  performs no validation, so "enforced in code" (D3) has no home and would be
  enforced only on the path someone remembered.
- **D4** A correction creates `rev=2`; `rev=1` remains **as a distinct file on
  disk** and is retrievable via `--as-of` a date before the correction. The
  file-level assertion is load-bearing: revisions currently resolve to the same
  path and `Doc.write` overwrites unconditionally, so a projection-only
  assertion passes while rev 1 has already been destroyed (D4a(b)).
- **D5** A tombstoned document is absent from search, absent from FTS5, absent
  from synthesis, and its head row and audit rows still exist — **with
  `corpus.sqlite` deleted**. Exclusion must not depend on the projection, or it
  fails open and serves deleted content whenever the projection is missing or
  stale (D4a(a)).
- **D6** Read-time resolution returns exactly one revision per `entity_id`, and
  adds **no more than 50ms to p50** on the real corpus measured over ≥100
  queries. A threshold, because "measurably does not regress" is satisfied by
  any measurement whatsoever.
- **D6a** Resolution issues **one** store query per search regardless of
  candidate count — asserted by counting calls, not by timing. The §3.3 batching
  requirement, and the ~494ms regression it exists to prevent, is invisible to a
  latency assertion on a small fixture.
- **D7** The **same burst** distilled twice produces one set of observations,
  proven with a distiller stubbed to return *different wording* on its second
  call. A hash-based implementation fails this, which is the point: the first
  draft's mechanism could not catch its own motivating example (§3.4).
- **D7a** A byte-identical document re-ingested by re-running a connector
  produces one record. This is what `content_hash` genuinely covers.
- **D8** A title match outranks an equally-scoring body match. Note the cost this
  gate conceals: the FTS schema is `fts5(chunk_id UNINDEXED, text)` with no
  `title` column and `title` is absent from `METADATA_COLUMNS`, so satisfying it
  is a full ~46k-chunk FTS rebuild, not a ranking tweak. It sequences **inside
  Phase 2** alongside the `deleted`/`entity_id` columns D4a(a) requires, so the
  corpus is rebuilt once rather than three times.
- **D9** The predecessor connector recovers `type`, `files_read`,
  `files_modified`, `prompt_number` for a known observation, and upgrades the
  existing graph-projected document rather than creating a duplicate.
- **D10** SIGKILL at each of the four write-ordering steps reconverges from a
  cold reopen with all four stores agreeing, **and** `entity_rev` is identical
  after replay to what it was before the crash. The rev assertion is the load
  bearing half: a read-modify-write implementation produces rev+2 on replay and
  passes every other part of this gate (D4a).
- **D10a** Deleting `corpus.sqlite` entirely and rebuilding it from `sources/`
  frontmatter reproduces it exactly — the executable statement of D4a. If this
  fails, the projection is secretly authoritative and the backup scope is wrong.
  Two conditions, both from review: any column **not** derivable from frontmatter
  (`relevance_count` is derived from `queries.sqlite`) must be named as excluded,
  or the gate is unpassable by construction; and the rebuild must be exercised at
  **corpus scale**, not on a fixture — a rebuild that works on 5 documents and
  takes 40 minutes on 33,000 has not been shown to work.
- **D11** Every document carries a frontmatter `schema_version`. Three schema
  changes are proposed here (typed observations, revision fields, `deleted`);
  without a version, a future migration cannot tell a pre-migration document from
  a malformed one, and the repo already has this scar — `index/store.py` raises
  "index schema predates enrichment columns" because a schema changed with no way
  to detect which generation a record belonged to.

**Storage**
- **H1** Compaction reduces fragment count and leaves every chunk retrievable.
- **H2** `--as-of` inside the retention window resolves; outside it, fails loudly
  rather than answering wrongly. **Blocked on Q3** — until the window is chosen
  this is a claim, not a gate, and is marked as such rather than counted green.
- **H3** `/health`'s fragment count, version count and on-disk size **match
  values obtained independently** from the filesystem. Asserting only that the
  fields are present passes against a hardcoded `fragment_count: 1`.

**Ambient write**
- **A1** A session ending produces indexed, retrievable observations with **no
  human invocation** — the literal §1.1 defect, inverted.
- **A2** A session ending mid-burst is not distilled until the idle threshold.
- **A3** A distillation failure leaves the burst unconsumed and is recorded, not
  swallowed.
- **A4** Every distillation writes a ledger row attributable to the bulk model.
- **A5** Exceeding the daily token bound skips distillation and leaves work
  pending; it never fails a session or drops a burst.
- **A6** A session terminated by **SIGKILL** — no shutdown hook, no clean exit —
  is still captured by the next periodic sweep (§5.1.1). A1 tests the happy path;
  this tests the case the hook cannot see.
- **A7** Two sessions ending simultaneously produce one set of observations per
  burst, not two — asserted with **both writers reaching distillation**, not with
  one returning `skipped_locked`. The write lock makes the naive version nearly
  vacuous: the second writer does nothing, so burst idempotency is never
  exercised. Run the second writer *after* the first releases, against the same
  still-open session.
- **A8** A burst containing a credential-shaped string is redacted **before** the
  model is called (§5.5), asserted by inspecting the prompt, not the output. A
  gate checking only that the stored observation is clean passes against an
  implementation that already sent the secret to the gateway.
- **A9** `ALEXANDRIA_AMBIENT=0` disables capture on every trigger path — shutdown
  hook and periodic sweep both (§5.6).
- **A10** A session under a configured excluded path is never captured (§5.5).

**Ambient read**
- **R1** With `serve` down, a session starts normally — no error, no added
  latency beyond the probe timeout, no injected context.
- **R2** With `serve` up, relevant context is injected and recorded under a
  distinct `client`.
- **R3 — PROVISIONAL, not counted green.** Below the relevance floor of **0.12**
  (§9 Q5), nothing is injected. Asserted against the negative set: 20 of 22
  `negative-v1.jsonl` queries produce no injected context. **Not** "every
  golden-set hit clears the floor" — measurably false, 3 of 31 do not, and a gate
  asserting it would fail on correct behaviour. The gate records the retained
  fraction (90.3%) rather than asserting completeness.
  Provisional until ≥10 in-domain negatives exist (§9 Q5).
- **R4** Re-running the §1.1 audit after a week shows in-session queries that are
  **agent-initiated**, established by an **allowlist** of caller values — not by
  excluding `client=injection` alone. That exclusion is necessary but not
  sufficient: the launchd sweep, the weekly loop, and eval runs all write to the
  same table, so a deny-list leaves the count satisfiable by machinery. The
  reported figure is the **rate**: agent-initiated queries in ≥1 of every 3
  sessions, correlated to sessions by id, over n ≥ 20 sessions. "Non-zero over a
  week" is passable by a single query and would not distinguish a working system
  from a dead one.

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

**Amended after review:** the first draft's ordering was *half* right and
overstated. Phase 1 lands tombstones, so putting it before capture genuinely
protects the **retrieval surface** — a bad ambient capture can be hidden. It does
**not** protect *existence*: bytes in `sources/`, git history, and the embedding
cache persist through Phases 1-4 regardless of ordering, and only Q1/Phase 5
addresses that. "A corpus we cannot clean" was binary rhetoric for a partial
mitigation. The ordering stands; the claim for it is narrower.

Hygiene also moves **before** the predecessor migration, since §3.6.1 adds ~14.5k
revisions and doing that to an uncompacted store is how §1.3's growth curve
becomes a problem.

1. **Phase 1 — data model** (§3, excluding the migration). Types,
   `entity_id`/`entity_rev`/`deleted` in frontmatter, the SQLite projection,
   batched read-time resolution, burst-level idempotency, field-weighted FTS5.
2. **Phase 2 — storage hygiene** (§4). Compaction, retention, observability.
   Required before any increase in write volume — which now explicitly includes
   the migration below, not just ambient capture.
3. **Then the predecessor migration** (§3.6 under §3.6.1's protocol): dry-run
   default, sampled precision measurement, git commit as rollback, resumable
   batches. Deliberately **after** Phase 2 rather than inside Phase 1, because it
   is a ~14.5k-revision bulk write. It is the volume validation for Phase 1 —
   the first real evidence the data model holds at scale.
4. **Phase 3 — ambient write** (§5). The periodic sweep (§5.1.1) as the
   mechanism, `session_shutdown` as its fast path, idle gate, one week of cost
   measurement, then the bound.
5. **Phase 4 — ambient read** (§6). `serve` under launchd, optional degradable
   injection, then re-run the §1.1 audit as R4 — excluding injections.
6. **Phase 5 — erasure** (§7). Blocked on Q1.

Each phase is independently shippable and safe to stop at. Reversing the data
model and ambient write would leave bad captures unhideable, since tombstones
are what Phase 1a buys.
