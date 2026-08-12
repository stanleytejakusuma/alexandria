# Package: Concurrent Write Path and `alexandria serve`

**Status:** proposed. Decisions in §11 are settled; the package is ready for adversarial
review.
**Date:** 2026-08-12
**Supersedes:** the ingestion half of the weekly loop introduced in `cf70313`.

**The claim this package makes:** *a fact you record is retrievable in seconds, the
corpus is safe to write to from more than one process, and a second harness can read it
over the network without a forgeable audit trail.*

Everything in the package serves that one sentence. Anything that does not is listed in
§10 with a named re-entry trigger.

---

## 0. Manifest

| in | why it is in |
|---|---|
| SQLite WAL + `busy_timeout` (backlog #1) | literal prerequisite for a second writer |
| Locked generation bump (backlog #2b) | unlocked read-modify-write loses invalidations |
| `flock` write lock | serialises the promote → index → bump section |
| `remember` → pending marker → inline promote | the freshness fix itself |
| Drain (offline fallback) | the CLI must work when the server is down |
| Liveness on oldest-pending age | the corpus must not be able to freeze silently again |
| `alexandria serve` — `/health`, `/search`, `/answer`, `/remember` | the unlock |
| Attribution as a property of the channel (backlog #8) | today's `--user` is forgeable |
| Backup/restore of `.alexandria` state (backlog #7) | this package *creates* new state |
| `cache_hit` metric correctness | otherwise the package cannot be honestly measured |
| Index manifest (provider/model/revision) | today nothing records which model built the index |
| Tenancy tripwire (executable) | keeps a latent bug from going live unnoticed |
| Pi extension routes through `serve` | otherwise the primary write path bypasses the server |
| Host-portable deployment (§5.8) | the server must not assume the operator's laptop |

| out | re-entry trigger |
|---|---|
| Session-distillation idle gate | after this package ships; `remember` freshness is the actual complaint |
| Tenant column / scope object | first customer requiring intra-org document ACLs |
| Per-tenant generation counters | same trigger; the global counter is correct for one install |
| Bearer-token auth | clients become dynamic rather than a fixed small set |
| Deletion / erasure path | deliberate: needs a policy decision first (see §10.1) |
| IVF index | flat scan exceeds 250 ms at working k (~150–200k chunks) |
| HNSW / PQ / distributed ANN | see D2 |
| Cost ledger, citation linkage, procurement floor | next package |

---

## 1. The problem, stated as measurements

Measured on this machine against the live corpus (45,984 chunks), 2026-08-12. No
estimates.

| operation | measured |
|---|---|
| embedding model cold load (MLX, Qwen3-Embedding-0.6B-8bit) | **16.036 s** |
| embed one new fact (marginal, model warm) | 0.040 – 0.187 s |
| upsert one chunk into LanceDB | 0.064 s |
| flat exact vector scan, k=10, full corpus | 0.076 s |
| flat exact vector scan, k=50 | 0.118 s |
| flat exact vector scan, k=200 | 0.258 s |
| vector scan with metadata prefilter | 0.059 – 0.071 s |
| full reindex (walk all documents) | ~168 s |
| **time before a remembered fact becomes searchable, as shipped** | **up to 6 days** |

The last row is the defect. The work required is ~165 ms; the delay is ~518,000 s.

Root cause is not storage and not scale. `alexandria remember` appends a line to
`inbox/<date>.md` and returns. Promotion to `sources/` and indexing happen **only**
inside the weekly cron, because that cron is the only caller of `sync` and `index`.
Three unrelated jobs — recording one fact, distilling sessions, and reconciling the whole
corpus — were bundled into one schedule, so the cheapest job inherited the slowest
cadence.

> **On causation.** The concurrency defects in §3 are *not* multi-tenant debt. They exist
> because the weekly cron was the only writer, which made single-writer-ness true by
> accident. Adding any second write trigger — which the freshness fix requires — makes
> both reachable at **one user**. They are siblings of the staleness bug, not scope creep.

## 2. Decisions locked by measurement

**D1 — Storage stays LanceDB + SQLite FTS5. No vector database server.**
LanceDB is an embedded library, not a service: the corpus is 2.2 GB of Lance fragments
plus a 644 MB FTS index, opened in-process. It already supports single-record `upsert()`
(`merge_insert("chunk_id")`, `index/store.py:38`) with the new row searchable
immediately. Adding Qdrant/Milvus/Weaviate would replace the 0.076 s component and leave
the 16.036 s component untouched, while adding a daemon, a network hop, and a second copy
of a private corpus. Rejected on those grounds, not on principle.

**D2 — No ANN index yet. Exact brute-force search is retained.**
`_indices/` is empty and no `create_index`/IVF/HNSW/PQ call exists anywhere in the source.
Every query is an exact scan, so recall is 1.0 by construction and there is no index that
can go stale relative to new rows. HNSW would cut 0.076 s to roughly 0.003 s — irrelevant
against a cold query dominated by a 16 s model load — in exchange for approximate recall,
build cost on every change, and reintroducing write-staleness.

> **Re-entry trigger:** build an IVF index when the flat scan exceeds **250 ms at the
> working k**, i.e. roughly 150k–200k chunks (3–4× current). Prefer IVF over HNSW
> (additive, `nprobes` is a recall dial). Avoid PQ: it trades recall for memory, and
> recall is already the weakest surface. Sharding per tenant *delays* this threshold,
> because each tenant index is smaller than one shared index.

**D3 — The fixed cost to amortize is the embedding model load, not storage.**
16.036 s of the write path is `MLXEmbedder._load()`. This single fact determines the
design: any scheme that starts a fresh process per fact pays 16 s per fact. Inline
indexing inside the CLI is therefore rejected — it would be slower per fact than Mem0,
whose latency is an LLM call.

**D4 — Documents stay immutable and content-hashed.** Supersession follows
`docs/SPEC-versioning-and-supersession.md` (stable `entity_id` + monotonic `entity_rev`,
append-never-edit, collapse at read time). This spec does not change document identity.

**D5 — The weekly job is kept, but stops being the ingestion path.** It becomes
reconciliation, compaction, integrity verification, and eval. Nothing waits on it.

**D6 — WAL and `serve` are orthogonal; both are required.** WAL + `busy_timeout` is
correctness under two writers. `serve` amortizes the 16 s model load, kills the 25–33 s
cold query, and provides the network read path. WAL does not amortize a model load, and
`serve` does not make SQLite safe. `serve` becoming the primary writer does **not**
retire the lock, because the CLI must remain fully usable when the server is down — a
hard constraint, which guarantees the possibility of two writers permanently.

---

## 3. Foundations — reachable-today bugs, fixed first

These land before `serve`, because `serve` makes each of them reachable in normal use.

### 3.1 SQLite concurrency (backlog #1)

`grep -rn busy_timeout src/` returns **zero hits**. `index/bm25.py:28-29` opens with
`check_same_thread=False` and sets `journal_mode=WAL`, but with no busy timeout a
concurrent writer fails *immediately* with `database is locked` rather than waiting.
Set `busy_timeout` on the BM25 and cache connections; verify WAL is actually in force
rather than assumed.

### 3.2 Generation-counter correctness (backlog #2)

Two halves. The first is **done** (`500cd9e`): `SearchEngine._generation` was captured
once in `__init__` and keyed every cache entry, so a long-lived server would have served
pre-reindex results for the life of the process — no error, no cache miss, no signal. It
is now re-read per access.

The second is **open**. `cache.py:64`:

```python
gen = read_index_generation(corpus) + 1   # unlocked read-modify-write
```

Two concurrent bumps both read N and both write N+1, silently losing one invalidation.
Unreachable today with one scheduled writer; reachable the moment a drain and a server
coexist. Fix under the same `flock` as §4.2.

### 3.3 The index does not record which model built it

`.alexandria/index/generation.json` contains only `{"finished_at", "generation"}`, and
`index/store.py` has no provider, model-name, or revision field anywhere. **The index has
no idea which embedding model produced its vectors.**

This is a live bug today, not merely a remote-hosting concern. The embedding *cache* key
is `sha256(name + revision + mode + text)`, so flipping `ALEXANDRIA_EMBED_PROVIDER`
correctly invalidates the cache — but the *index* is a different store. A subsequent
incremental `index` run (`upsert`, not `--rebuild`) writes torch vectors into a table
that still holds MLX vectors, in one column, with no error. The result is silently
incomparable similarity scores: not a crash, not a visibly wrong answer, just quietly
degraded ranking.

Fix: write an index manifest at index time recording provider, model name, revision,
embedding dimension, and creation time. Both the CLI and `serve` verify it on open and
refuse to proceed on mismatch. This is the precondition for gate S9, and therefore for
safe remote hosting — without it there is nothing to compare against.

### 3.4 `cache_hit` metric correctness

`cache_hit` currently conflates two different events: `retrieval/search.py:123` sets 1
for a query-cache hit, and the `answer` path also records 1 for a cached *retrieval*
while the synthesis LLM still runs. Measured consequence: of 1,199 rows flagged
`cache_hit=1`, only **267 are actually sub-10 ms**; 932 exceed 100 ms. The true fast-path
rate is **267/2,377 ≈ 11%**, not the 79.7% the flag implies.

This is in the package because `serve` adds a *third* cache dimension (warm in-process vs
on-disk). Building that telemetry on a flag that already conflates two things makes every
measurement of this package untrustworthy. Separate the codes; keep `tier` and `client`
in mind as already-dead discriminators (`tier` is always `map`, `client` always `cli`).

---

## 4. Architecture

Three write classes, three cadences. The whole design is the unbundling.

| class | trigger | latency to searchable | cost when idle |
|---|---|---|---|
| `remember` — one fact | the CLI call or `/remember` | ~165 ms warm | — |
| session distillation | idle-gated, hourly | one cycle | a `stat` |
| full reconcile | weekly | n/a — nothing waits on it | scheduled |

With `serve` running, the promote → embed → upsert → FTS → bump sequence runs **inline on
the write**. With `serve` down, the same sequence runs on the drain's timer. Identical
code path, different trigger.

### 4.1 Why a pending marker rather than scanning the inbox

The drain must know what is unpromoted without re-reading and re-hashing every inbox
file. `remember` writes an entry id to a pending list; promotion consumes it. This makes
promotion idempotent — a crash mid-promote leaves the entry pending, and re-running
promotes it exactly once because `upsert` is keyed on `chunk_id` — and makes "is anything
pending?" a file-existence check rather than a scan. It is also the input to §7's
liveness signal.

### 4.2 The write lock

`fcntl.flock(fd, LOCK_EX | LOCK_NB)` on `.alexandria/index/.write.lock`, held across the
whole promote → embed → upsert → FTS → generation-bump section.

> **Not an `O_EXCL` sentinel.** An `O_EXCL` lock file survives `SIGKILL` with no owner
> recorded and no way to distinguish a live holder from a dead one, so one hard kill
> wedges every future writer silently — and §7's detector would not report it. `flock` is
> released by the kernel when the holding process dies, by any means.

The drain **skips its run** rather than blocking when the lock is held: the weekly
reconcile is the long job, and a skipped drain costs at most one interval of freshness.

### 4.3 Generation and cache

Each promotion cycle that does work bumps the generation once — not once per fact —
invalidating query and response caches. Given §3.3's real fast-path rate of ~11%, that
invalidation is cheap against the alternative of answering without a fact recorded
minutes ago.

> Do **not** adopt a "hot buffer" holding new facts outside the index to dodge the bump.
> The query cache is consulted *before* retrieval and its key includes the generation, so
> a repeated query would return the cached result and never observe the buffer —
> reintroducing the same staleness bug with a quieter failure mode.

---

## 5. `alexandria serve`

Standard-library `http.server`. No framework, no MCP, no new dependency. `README.md:201`
already promises "anything that can call an HTTP endpoint"; this makes that true for the
first time.

### 5.1 Surface

| endpoint | method | notes |
|---|---|---|
| `/health` | GET | status, generation, chunk count, uptime, oldest-pending age |
| `/search` | POST | the hot path; no LLM |
| `/answer` | POST | the 600 s synthesis pipeline — see §5.4 |
| `/remember` | POST | write path; requires §4.2's lock |

### 5.2 Bind policy

Default `127.0.0.1`. A non-loopback bind is refused unless
`ALEXANDRIA_SERVE_ALLOW_REMOTE=1` is set explicitly — **fail closed**, because the
failure being prevented is a default-open port serving a private corpus. Remote access is
an SSH tunnel, not a bind-address change.

### 5.3 Attribution — identity is a property of the channel

Today's `--user`/`--caller` is worse than absent: a forgeable, plausible-looking audit
trail (backlog #8).

The deciding constraint is that **over an SSH tunnel every connection arrives as
`127.0.0.1`**. The server cannot distinguish callers by inspecting the connection, so
identity must come from *which channel was reached*, never from the request body.

**Decision: one Unix domain socket per client identity.** SSH forwards Unix sockets
natively; access is enforced by kernel file permissions; there is no secret to store,
leak, or rotate. Identity is unforgeable because it is not transmitted.

`--user`/`--caller` supplied in a request body may be retained as an unprivileged *hint*
field, but is never recorded as who did this. Bearer tokens are deferred to the same
trigger as tenancy: clients becoming dynamic rather than a fixed small set.

### 5.4 Concurrency, timeouts, and the `/answer` problem

`ThreadingHTTPServer` so one slow client cannot block another at the connection level,
with a single `threading.Lock` around engine use, since the engine holds a LanceDB handle
and a SQLite connection opened `check_same_thread=False`.

`/answer` runs a pipeline measured at up to **600 s**. It must therefore **not** hold the
engine lock for its duration — it holds the lock only for its retrieval phase and
releases it before synthesis. A request timeout bounds the handler so a wedged LLM call
cannot pin a worker forever.

> `# ponytail: one global engine lock; per-request engines if throughput matters.`

### 5.5 Input validation at the trust boundary

First time corpus access is reachable over a socket, so requests are validated rather
than trusted: bounded request body, `k` clamped, query length capped, filter keys checked
against the known-field whitelist, malformed JSON answered 400 rather than a traceback.

### 5.6 Cache coherence

Fixed ahead of the server in `500cd9e` (§3.2). Recorded here because it is the
characteristic failure this system produces: reporting healthy while serving stale truth.

### 5.7 Lifecycle

launchd may start the server on demand and let it idle out; this is not a 24/7 daemon
requirement. **The CLI works identically whether or not the server is running.**

### 5.8 Deployment topology — default local, remote supported

**Default: the operator's own machine, `127.0.0.1`.** For a single user this is the
whole story and requires no configuration — the corpus, the model, and the server all sit
on one host, and nothing is exposed.

**But the server must not assume that host is a laptop.** It must be deployable on an
always-on machine — a NAS, a spare box, a customer's VM — with clients reaching it over
an SSH tunnel (§5.2/§5.3). Nothing in the implementation may hard-code the operator's
machine, a macOS-only path, or launchd as the only supervisor.

> **The binding constraint is the embedding provider, not the network.** The corpus is
> indexed with exactly one embedding model, and the cache key is
> `sha256(name + revision + mode + text)` where `name` is the model. MLX uses
> `mlx-community/Qwen3-Embedding-0.6B-8bit`; torch uses `Qwen/Qwen3-Embedding-0.6B`.
> These are different vectors in different spaces, and mixing them silently produces
> incomparable similarity scores. **A host serving an index must embed queries with the
> same provider that built it.** Since MLX is Apple-Silicon only, moving the server to a
> Linux host requires the index to have been built with `ALEXANDRIA_EMBED_PROVIDER=local`
> (torch) — a full re-embed, not a file copy.

This is why remote hosting is a **supported topology rather than a default**: it is a
one-time re-embed decision made at install, not a runtime switch. The installer should
ask which host will serve, so the index is built with the right provider the first time.

Second constraint, learned the hard way on 2026-08-11: **serving is not indexing.**
Embedding one query is ~1/46,000 of the work of building the corpus. A modest
always-on host can comfortably *serve* an index it could never *build* — a CPU embed of
45,984 chunks exhausted such a host and hard-rebooted it. Build on a capable machine,
serve anywhere.

### 5.9 The Pi extension routes through `serve`

The `alexandria-remember` / `alexandria-search` extension tools currently shell out to
the CLI, which means the primary write path pays the 16 s model load per call and
bypasses the server entirely. They route through the server when it is reachable, and
fall back to the CLI when it is not — the same reachability rule as §5.7, so the tools
never become less reliable than they are today.

---

## 6. Backup and restore (backlog #7)

In this package because the package *creates new state that nothing backs up*: the
pending list and the liveness state file. Total loss today also destroys the accumulated
query and citation signal — 2,377 logged queries — which is the training input for any
future learning loop.

Scope: back up `.alexandria` **state** — `queries.sqlite`, audit logs, eval history,
liveness state, `generation.json` — and explicitly **not** the rebuildable indexes
(`chunks.lance` 2.2 GB, `fts.sqlite` 644 MB). Restore must be exercised, not assumed;
an unverified backup is the same class of claim as an unverified cron.

---

## 7. Liveness

The failure this system actually suffered was a step reporting success while doing
nothing: the weekly loop wrote `>> "$DIGEST"` into a directory that did not exist, so
every sync aborted on its own redirect while an `--allow-empty` commit manufactured
evidence of success. It ran zero successful times from `cf70313` until the fix, and
nothing noticed for three days.

**The primary signal is the age of the oldest unconsumed pending entry** — not a
`last_success_at` heartbeat. `remember` writes the marker and only a successful promotion
consumes it, so oldest-pending age measures the actual promise — "searchable within one
interval" — rather than whether a process reported success.

A heartbeat fails for precisely the reason the original bug survived: a run that aborts
early never writes it, leaving the file missing or holding a healthy pre-regression
timestamp, and the checker stays silent. Oldest-pending age catches all three real
shapes with one number — never launched, ran but promoted nothing, aborted mid-run.

Rules:

- warn when oldest-pending age exceeds **2× the drain interval**
- **a missing or unparseable state file counts as stale and warns** — fail closed;
  absence of evidence is not evidence of health
- every `alexandria` invocation performs the check and prints one line to stderr,
  requiring no new scheduled process; `/health` exposes the same number
- `last_success_at`, `promoted_count`, `generation` are retained as telemetry only

Separately, to catch a run that promotes successfully but writes *garbage* embeddings:
after each promotion cycle, query one just-promoted fact by its own text and assert its
`chunk_id` appears in the top-k (~100 ms warm).

---

## 8. Tenancy — a tripwire, not a column

`BACKLOG.md` #27/#28 defers tenant scope with a named trigger (first customer requiring
intra-organization document ACLs) and states the global generation counter is correct for
one install. **That deferral stands.**

It was briefly overridden on the argument that adding a `tenant` column later would force
a full 2.2 GB rebuild — `index/store.py:49-59` shows a table created before the
enrichment columns silently drops them on merge, with `--rebuild` as the documented fix.
That argument was tested and is **false**: `lancedb` 0.36.0 `Table.add_columns()`
backfilled `tenant='default'` across a 100-row table with vectors intact and no
re-embedding. Retrofit is cheap, so there is no cost argument for building it early.

What *is* real is a latent bug. `ResponseCache.key()` (`cache.py:172`) composes
`(schema, "a", question, model, k, prompt_version, generation)` — **no filters**. Today
that is harmless because `answer` accepts no filter arguments and `gather.py` reads none.
It becomes a wrong-answer bug the moment `answer` gains filters, and a cross-tenant data
leak the moment a second tenant exists.

**Implemented as an executable tripwire, not a prose warning:** a test asserting that
either `answer` exposes no filter arguments, or `ResponseCache.key()` includes them. A
prose caveat rots; a failing test does not. Whoever adds `--project` to `answer` gets a
red test in the same commit.

---

## 9. Acceptance gates

Each gate is a test, not a claim.

**Foundations**
- **F1** Two processes writing the FTS index concurrently both succeed; neither raises
  `database is locked`.
- **F2** Two concurrent generation bumps produce N+2, not N+1.
- **F3** `cache_hit` distinguishes query-cache hits from answer-path retrieval hits; a
  sub-10 ms fast-path hit is separable in the logs.
- **F4** An index carries a manifest naming its embedding provider, model, revision, and
  dimension; opening it with a different provider fails loudly instead of mixing vector
  spaces in one column.

**Write path**
- **W1** `remember` returns in under 500 ms and does not load the embedding model.
- **W2** A fact written moments earlier is returned by `search` — end to end against the
  real corpus, not a fixture.
- **W3** A promotion interrupted mid-run leaves the entry pending; re-running promotes it
  exactly once, verified by `chunk_id` count.
- **W4** Generation bumps once per cycle, not once per fact.
- **W5** With the write lock held by another process, a drain exits cleanly without
  mutating the index and without raising.
- **W6** A drain and a reconcile started simultaneously produce no `database is locked`
  and no LanceDB commit conflict; the corpus is intact afterwards.
- **W7** With the state file artificially aged — and separately, deleted — an ordinary
  `search` prints the staleness warning to stderr and still returns results.

**Serve**
- **S1** `/health` returns 200 with a chunk count matching the corpus; default bind is
  `127.0.0.1`.
- **S2** A non-loopback bind is refused without `ALEXANDRIA_SERVE_ALLOW_REMOTE=1`.
- **S3** Warm `/search` p50 under 500 ms versus the measured 25–33 s cold path; the model
  loads exactly once across N requests.
- **S4** After an external `alexandria index` bumps the generation, the **running** server
  returns fresh results rather than a pre-reindex cached page. *(Landed:
  `tests/test_search.py::test_reindex_invalidates_cache_for_a_long_lived_engine`,
  mutation-verified.)*
- **S5** Malformed JSON, oversized body, out-of-range `k`, and unknown filter key each
  return 4xx, not a traceback.
- **S6** The CLI works normally while the server runs, and while it does not.
- **S7** A request arriving on socket A is attributed to A's identity even when the body
  claims to be someone else.
- **S8** A slow `/answer` does not block a concurrent `/search`.
- **S9** A server whose embedding provider does not match the index manifest (§3.3)
  **refuses to start** with a named error, rather than silently serving vectors from a
  different model. Depends on F4.
- **S10** With the server stopped, the Pi extension still answers via the CLI; with it
  running, the same query does not reload the model.

**Backup**
- **B1** A restore from backup reproduces query history, audit log, and liveness state —
  exercised, not asserted.

**Tenancy**
- **T1** The tripwire test fails if `answer` gains a filter argument while
  `ResponseCache.key()` still omits filters.

---

## 10. Deliberately not built

- ANN index (D2 trigger)
- vector database server (D1)
- automatic compaction after every write — fragment count is monitored, compaction runs
  weekly; 60 fragments currently, one per index run
- a separate small embedding model for inserts: mixing models in one vector space makes
  similarity scores incomparable, the same trap as the MLX/torch cache-key split
- online RL / bandit tuning: exploration means deliberately serving worse results to real
  users
- bearer tokens, tenant scope object, per-tenant generation counters (§8, backlog #27/#28)

### 10.1 Deletion — deferred pending a policy decision, not effort

Kept as a reference point by explicit decision. Recorded here because the *shape* of the
problem determines whether it is cheap or expensive later.

A document currently survives in: `sources/*.md`; `chunks.lance` (2.2 GB, `_deletions/`
exists so tombstones are supported); `fts.sqlite` (644 MB); `enrichment.sqlite` (26 MB);
`queries.sqlite` `retrieved_ids` (which is also the learning signal); the corpus **git
history** (34 commits); `wiki/` pages citing it (13); the response cache; and the
embedding cache — **whose location is currently unresolved**, since the documented path
`.alexandria/index/embeddings.sqlite` is 0 B. *You cannot erase from a store you cannot
locate; finding it is a prerequisite for any erasure work.*

Two tensions make this a policy question rather than an engineering one:

1. **Erasure versus audit.** An immutable, provenance-bearing audit trail is the product
   differentiator; erasure requires records be destroyable. Both cannot be absolute.
2. **Git.** Real erasure means history rewriting, which breaks every clone. The standard
   escape is crypto-shredding — encrypt per subject, delete the key — which
   `SPEC-versioning-and-supersession.md` names and puts out of scope.

**The decision to make before building anything: does erasure include the audit trail and
git history, or does it stop at the retrievable surface?** If audit is exempt, deletion
is a weekend. If it is not, crypto-shredding must be designed in before the corpus grows
much further — the one item here that genuinely gets more expensive with time.

---

## 11. Decisions — resolved 2026-08-12

- **Order:** `serve` first, over drain-first, because it is the only option that unblocks
  the second harness. Accepted risk: the write path and the network boundary are built
  together, so §4.2's lock and §7's detector land *with* the server.
- **Bind:** localhost default; non-loopback fails closed; remote via SSH tunnel.
- **Cadence:** absorbed by `serve` — inline at ~165 ms. The drain survives only as the
  offline fallback, defaulting to 10 minutes, skipping rather than queuing under lock.
- **Endpoints:** `/answer` and `/remember` are **in** v1, which pulls the `flock` and the
  `/answer` lock-release requirement (§5.4) into scope with them.
- **Attribution:** one Unix socket per client identity; request-body identity never
  recorded as authoritative.
- **Backup:** in the package.
- **Tenancy:** deferred per backlog; enforced by an executable tripwire (§8).
- **Golden set:** used as-is, as a **regression guard** ("did we break retrieval?") and
  explicitly not as a progress measure. This package changes freshness and concurrency,
  not retrieval quality, so the golden set's known brittleness (63.3% recall, 18 misses
  against brittle `must_retrieve` ids) is not load-bearing here. No repair work.
- **Serve host:** default is the operator's own machine; remote hosting on an always-on
  box is a supported topology, gated on the embedding-provider constraint in §5.8.
  Building the server is not blocked on choosing a host.
- **Session distillation:** out of this package; tracked separately.
- **Extension routing:** in — the extension calls `serve` when reachable, CLI otherwise.
- **Embedding cache in backup:** out (rebuildable), but it must first be *located* — the
  documented path is 0 B while a ~4.59 GB cache demonstrably exists. Tracked separately.
- **Deletion:** out (§10.1).

---

## 12. Acceptance

The package is done when a **fresh Pi session** and **H‍ermes on the second host** both
answer a question correctly from the same corpus, over the real path, where the answer
depends on a fact recorded *after* the server started — and neither could have known it
otherwise.

That is the canary test. It is the finish line, not a separate objective, and it is
deliberately the last step: it can only be written once this spec is reviewed and built,
never before.

`inbox/2026-08-11.md` — 12 entries written at 23:50 against a corpus last indexed at
20:34, still unretrievable hours later — is the designated pre-package measurement of the
defect.
