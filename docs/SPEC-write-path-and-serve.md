# Write Path and Serve — Design Spec

**Status:** proposed, awaiting sign-off on the three open decisions in §7.
**Date:** 2026-08-12
**Supersedes:** the ingestion half of the weekly loop introduced in `cf70313`.

**Goal:** make a newly captured fact retrievable in minutes instead of days, without
adding a database server, without losing exact recall, and without rewriting the corpus.

---

## 1. The problem, stated as measurements

Everything below was measured on this machine against the live corpus (45,984 chunks),
2026-08-12. No estimates.

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
Three unrelated jobs — recording one fact, distilling sessions, and reconciling the
whole corpus — were bundled into one schedule, so the cheapest job inherited the
slowest cadence.

## 2. Decisions locked by measurement

**D1 — Storage stays LanceDB + SQLite FTS5. No vector database server.**
LanceDB is an embedded library, not a service: the corpus is 2.2 GB of Lance fragments
plus a 644 MB FTS index, opened in-process. It already supports single-record
`upsert()` (`merge_insert("chunk_id")`, `index/store.py:38`) with the new row searchable
immediately. Adding Qdrant/Milvus/Weaviate would replace the 0.076 s component and leave
the 16.036 s component untouched, while adding a daemon, a network hop, and a second
copy of a private corpus. Rejected on those grounds, not on principle.

**D2 — No ANN index yet. Exact brute-force search is retained.**
`_indices/` is empty and no `create_index`/IVF/HNSW/PQ call exists anywhere in the source.
Every query is an exact scan, so recall is 1.0 by construction and there is no index
that can go stale relative to new rows. HNSW would cut 0.076 s to roughly 0.003 s —
irrelevant against a cold query dominated by a 16 s model load — in exchange for
approximate recall, build cost on every change, and reintroducing write-staleness.

> **Re-entry trigger:** build an IVF index when the flat scan exceeds **250 ms at the
> working k**, i.e. roughly 150k–200k chunks (3–4× current). Prefer IVF over HNSW
> (additive, `nprobes` is a recall dial). Avoid PQ: it trades recall for memory, and
> recall is already the weakest surface. Note that sharding per tenant *delays* this
> threshold, because each tenant index is smaller than one shared index.

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

## 3. Architecture

Three write classes, three cadences. The whole design is the unbundling.

> **Ordering note.** This section is written drain-first because that is the order the
> design was reasoned in. Q1 later chose to *build* `serve` (§4) first. The architecture
> below is unchanged by that choice — with a warm server the same promote → embed →
> upsert → FTS → bump sequence runs inline on the write instead of on a timer, and
> §3.2's lock is required either way.

| class | trigger | latency to searchable | cost when idle |
|---|---|---|---|
| `remember` — one fact | the CLI call | append is instant; searchable at next drain | — |
| drain — promote + index pending | timer, every 10 min | ≤ 10 min + 16 s | one `stat` |
| session distillation — LLM | timer, gated on transcript idle | ≤ idle timeout | one `stat` |
| reconcile — full walk, compact, eval, verify | weekly | n/a | — |

```
remember ──► inbox/<date>.md  +  pending marker        (instant, no model)
                    │
              [drain, every 10 min]
                    │  stat pending → empty? exit 0 immediately
                    │  non-empty? load model ONCE, then per entry:
                    ├──► promote to sources/
                    ├──► embed  (~0.1 s)
                    ├──► LanceDB upsert  (~0.06 s)
                    ├──► FTS5 insert
                    └──► mark promoted, bump generation once per drain
                    │
              [weekly] reconcile · compact fragments · verify · eval
```

The drain amortizes one 16 s model load across every fact that accumulated. At an idle
poll it does a `stat` and exits, which is what makes a 10-minute cadence affordable
where a 10-minute *full reindex* would be absurd.

### 3.1 Why a pending marker rather than scanning the inbox

The drain must know what is unpromoted without re-reading and re-hashing every inbox
file. `remember` writes an entry id to a pending list; the drain consumes it and marks
it promoted. This makes the drain idempotent (a crash mid-drain leaves the entry
pending, and re-running promotes it exactly once because `upsert` is keyed on
`chunk_id`) and makes "is anything pending?" a file-existence check rather than a scan.

### 3.2 Concurrency — a race the drain creates

There is no write coordination anywhere in the codebase today: no `flock`, no `fcntl`,
no lock file, no LanceDB commit-conflict retry, and no SQLite `busy_timeout`. This is
currently safe *by accident* — the weekly cron is the only writer, serialised by being
the only thing scheduled.

The drain breaks that assumption. It creates at minimum a second writer, and in practice
three: the drain, the weekly reconcile, and any manual `alexandria index`. Two concrete
failure modes follow:

- **SQLite FTS** (`index/bm25.py:28-29`) sets `journal_mode=WAL` but no `busy_timeout`,
  so a concurrent writer fails immediately with `database is locked` rather than waiting.
- **LanceDB** uses optimistic concurrency; the loser of a commit race raises a conflict,
  and nothing in `index/store.py` retries it.

This is the tax of embedded storage — a database server would provide write coordination
as part of what it is. Since D1 keeps storage embedded, the coordination is ours to write:

- `fcntl.flock(fd, LOCK_EX | LOCK_NB)` on `.alexandria/index/.write.lock`, held across
  the whole promote → embed → upsert → FTS → generation-bump section, so exactly one
  writer mutates the index at a time

> **Not an `O_EXCL` sentinel.** (Red, 2026-08-12; the first draft of this section had it
> wrong.) An `O_EXCL` lock file survives `SIGKILL` with no owner recorded and no way to
> distinguish a live holder from a dead one, so a single hard kill wedges every future
> writer silently — and §5's detector would not report it. `flock` is released by the
> kernel when the holding process dies, by any means. Stdlib, zero dependencies,
> strictly better.
- the drain **skips its run** rather than blocking if the lock is held: the weekly
  reconcile is the long job, and a skipped drain costs at most one interval of freshness
- `busy_timeout` set on the BM25 and embedding-cache connections so brief overlaps wait
  instead of failing

`serve` (§4) largely dissolves this later by making one long-lived process the sole
writer, but the lock is still required, because the CLI must remain usable when the
server is not running.

### 3.3 Generation and cache

Each drain that does work bumps the generation counter once — not once per fact —
which invalidates query and response caches. Measured cost of that invalidation: the
query cache delivers a genuine sub-10 ms fast path on **267 of 2,377** logged queries
(11%). Flushing an 11%-effective cache a few times an hour is the correct trade against
serving an answer that omits a fact the user recorded ten minutes ago.

> Do **not** adopt a "hot buffer" that holds new facts outside the index to dodge the
> generation bump. The query cache is consulted *before* retrieval and its key includes
> the generation, so a repeated query would return the cached result and never observe
> the buffer — reintroducing the same staleness bug with a quieter failure mode.

## 4. Phase 1 — `alexandria serve`

Built first (Q1). A long-lived process holding the model and index solves four problems
at once, which is why it is the destination rather than an optional extra:

1. the 16 s load is paid once at startup, so writes become genuinely inline
2. queries stop paying cold-start, addressing the measured 25–33 s cold path
3. it gives a remote harness a read path over HTTP — currently the blocking dependency
   for the second host, which cannot build its own index (a CPU embed of ~45k chunks
   exhausted that host and hard-rebooted it on 2026-08-11)
4. it is the prerequisite for per-tenant scoping under the multi-tenant premise

Shape: standard-library `http.server`, no framework, no MCP, no new dependency.
`README:201` already promises "anything that can call an HTTP endpoint"; this makes that
true for the first time.

### 4.1 Surface

| endpoint | method | in | out |
|---|---|---|---|
| `/health` | GET | — | status, generation, chunk count, uptime |
| `/search` | POST | `query`, `k`, `filters` | ranked results as JSON |

`/answer` and `/remember` are **deferred, not forgotten**. `/answer` is the 600 s LLM
pipeline, against which a 16 s model load is rounding error, so it gains almost nothing
from being warm and drags the whole LLM-client configuration surface across the network
boundary. `/remember` waits for §3.2's lock to land, because it is the write path and
must not race the CLI. Ship the read path, prove it end to end, then extend.

### 4.2 Bind policy

Default `127.0.0.1`. A non-loopback bind is refused unless
`ALEXANDRIA_SERVE_ALLOW_REMOTE=1` is set explicitly — fail closed, because the failure
being prevented is a default-open port serving a private corpus with no authentication.
Remote access is the SSH tunnel of Q2, not a bind-address change. No token scheme is
built, because building one would be building an alternative to the design already
chosen.

### 4.3 Concurrency

`ThreadingHTTPServer` so one slow client cannot block another at the connection level,
with a single `threading.Lock` around engine use, since the engine holds a LanceDB
handle and a SQLite connection opened `check_same_thread=False`. This buys request-level
concurrency without concurrent engine mutation.

> `# ponytail: one global engine lock; per-request engines if throughput matters.`

### 4.4 Input validation at the trust boundary

This is the first time corpus access is reachable over a socket, so request handling
validates rather than trusts: bounded request body, `k` clamped to a maximum, query
length capped, filter keys checked against the known-field whitelist, and malformed JSON
answered with 400 rather than a traceback. None of this is optional laziness-eligible
surface.

### 4.5 Cache coherence — the failure a warm process introduces

Every CLI invocation today builds a fresh engine, so it physically cannot serve a stale
generation. A long-lived process can, and the codebase was already shaped to let it:
`retrieval/search.py` captured `self._generation` **once in `__init__`** and used it to
key every query-cache entry. A running server would therefore have kept answering from
pre-reindex cache entries for the life of the process — no error, no cache miss, no
signal. That is this system's characteristic failure mode: reporting healthy while
serving stale truth.

Fixed at the shared property rather than special-cased in the server, so the CLI and
every future consumer inherit it: `_generation` is now re-read per access. The cost is a
few hundred bytes of page-cached JSON against a query floor of ~75 ms.

launchd can start the server on demand and let it idle out, so this is not a 24/7 daemon
requirement. The CLI remains fully usable when the server is down — a hard constraint,
not a nicety.

## 4A. Phase 2 — the drain

Demoted to the offline fallback by Q3, but still required, because the CLI must work when
the server is not running and because the weekly reconcile still needs a trigger. Design
as described in §3; the lock of §3.2 lands with Phase 1, not with this phase.

## 5. Liveness

The failure mode this system has actually suffered is a step reporting success while
doing nothing: the weekly loop wrote `>> "$DIGEST"` into a directory that did not exist,
so every sync aborted on its own redirect while an `--allow-empty` commit manufactured
evidence of success. It ran zero successful times from `cf70313` until the fix, and
nothing noticed for three days.

**The primary signal is the age of the oldest unconsumed pending entry** — not a
`last_success_at` heartbeat. (Red, 2026-08-12; the heartbeat version of this section was
wrong.) `remember` writes the pending marker and only a successful promotion consumes it,
so oldest-pending age measures the actual promise the system makes — "searchable within
one interval" — rather than whether a process reported success.

A heartbeat fails here for exactly the reason the original bug survived three days: a run
that aborts early never writes `last_success_at` at all, leaving the file missing or
holding a healthy pre-regression timestamp, and the checker stays silent. Oldest-pending
age catches all three real failure shapes with one number — the job never launched (bad
plist), the job ran but promoted nothing, and the job aborted mid-run.

Rules:

- warn when oldest-pending age exceeds **2× the drain interval**
- **a missing or unparseable state file counts as stale and warns.** Fail closed;
  absence of evidence is not evidence of health
- every `alexandria` invocation performs this check and prints one line to stderr,
  requiring no new scheduled process — it piggybacks on interactive use
- `last_success_at`, `promoted_count`, and `generation` are still recorded, demoted to
  telemetry

Separately, to catch a run that promotes successfully but writes *garbage* embeddings:
after each drain, query one just-promoted fact using its own text and assert its
`chunk_id` appears in the top-k (~100 ms warm). This automates G2 on every run rather
than once at acceptance.

### 5.1 Tenancy — add the column now, not later

Add a `tenant` scalar column (default `"default"`) to the LanceDB schema and
`chunk_metadata`, and include tenant in the cache key, **before** the server ships.

The reason is migration cost, not present need. `index/store.py:49-59` already carries a
guard from an earlier review: a table created before the enrichment columns existed
*silently drops them on merge*, and the documented fix is `alexandria index --rebuild`.
The same trap applies to any column added later, and the rebuild cost grows with the
corpus — 2.2 GB today.

Measured mitigation: lancedb 0.36.0 exposes `Table.add_columns()` with SQL-expression
defaults, so the existing 45,984 rows can gain `tenant='default'` **without** re-embedding.
The migration is a scalar column append, not a rebuild — cheaper than Red assumed when
raising it.

Per-tenant generation counters are explicitly **not** added. A global counter
over-invalidates across tenants, which is a performance cost only, never a correctness or
isolation failure. Tenant striping of the cache key is the part that matters, because
`ResponseCache.key()` currently omits filters entirely — two tenants asking the same
question would collide on one cache row and one would receive an answer synthesised from
the other's private documents, with citations.

## 6. Deliberately not built

- ANN index (see D2 trigger)
- vector database server (see D1)
- automatic compaction after every write — fragment count is monitored, compaction runs
  weekly; 60 fragments currently, one per index run
- a separate small embedding model for inserts: mixing models in one vector space makes
  similarity scores incomparable, the same trap as the MLX/torch cache-key split
- online RL / bandit tuning of retrieval: exploration means deliberately serving worse
  results to real users
- deletion UX beyond tombstones — `_deletions/` exists in the store, so this is a
  product gap, not a storage limitation, and it belongs to the versioning spec

## 7. Decisions — resolved 2026-08-12

**Q1. Order — `serve` first.** Chosen over drain-first because it is the only option that
unblocks the second harness, and because inline writes through a warm process subsume the
drain's purpose on the interactive path. Accepted risk: the write path and the network
boundary get built at the same time rather than sequentially, so §3.2's lock and §5's
detector must land *with* the server, not after it.

**Q2. Bind address — localhost by default, remote via SSH tunnel.** `127.0.0.1` is the
default for every deployment, personal and enterprise alike; a non-loopback bind must be
explicit and opt-in. The remote harness reaches the server through an SSH tunnel, which
adds no new authentication surface because the tunnel already authenticates. This keeps
the single-user case zero-configuration while making the networked case a deliberate,
auditable step rather than a default-open port holding a private corpus.

**Q3. Cadence — absorbed by `serve`.** With a warm process the write is inline and the
fact is retrievable in ~165 ms, so no polling interval governs the interactive path. The
drain remains only as the offline fallback for when the server is not running, defaulting
to 10 minutes. It skips rather than queues when the write lock is held, so a long weekly
reconcile cannot cause drains to pile up.

## 8. Acceptance gates

Each gate is a test, not a claim.

Phase 1 (`serve`):

- **S1** `/health` returns 200 with a chunk count matching the corpus, and the server
  binds `127.0.0.1` by default.
- **S2** A non-loopback bind is refused without `ALEXANDRIA_SERVE_ALLOW_REMOTE=1`.
- **S3** Warm `/search` p50 is under 500 ms — versus the measured 25–33 s cold path —
  and the model is loaded exactly once across N requests.
- **S4** After an external `alexandria index` bumps the generation, the **running**
  server returns fresh results rather than a pre-reindex cached page. *(Regression test
  landed ahead of the server:
  `tests/test_search.py::test_reindex_invalidates_cache_for_a_long_lived_engine`;
  mutation-verified by pinning `_generation` back to construction time.)*
- **S5** Malformed JSON, an oversized body, an out-of-range `k`, and an unknown filter
  key each return 4xx, not a traceback.
- **S6** The CLI works normally while the server is running, and while it is not.

Phase 2 (drain):

- **G1** `remember` returns in under 500 ms and does not load the embedding model.
- **G2** After a drain, a fact written moments earlier is returned by `search` — verified
  end to end against the real corpus, not a fixture.
- **G3** A drain with nothing pending exits non-zero-work in under 200 ms and does not
  load the model.
- **G4** A drain interrupted mid-run leaves the entry pending; re-running promotes it
  exactly once (no duplicate chunk, verified by `chunk_id` count).
- **G5** Generation bumps once per drain, not once per fact.
- **G6** With the state file artificially aged, an ordinary `search` prints the staleness
  warning to stderr and still returns results.
- **G7** The weekly job no longer performs ingestion, and a fact remains retrievable
  across a full reconcile run.
- **G8** With the write lock held by another process, a drain exits cleanly without
  mutating the index and without raising — verified by holding the lock and asserting
  chunk count is unchanged.
- **G9** A drain and a reconcile started simultaneously produce no `database is locked`
  error and no LanceDB commit conflict; the corpus is intact afterwards.

## 9. First test case

`inbox/2026-08-11.md` currently holds 12 entries written during the flush, none of them
promoted or indexed. The Telethon peer-type entry is the designated end-to-end fixture:
it is real, it is currently unretrievable, and it must become retrievable within one
drain cycle.
