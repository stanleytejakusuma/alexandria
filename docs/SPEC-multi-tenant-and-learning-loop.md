# SPEC — Multi-tenant boundary + the learning loop

**Status:** draft, unimplemented · **Raised:** 2026-08-11 (deep audit session)
**Supersedes nothing.** Companion to `ARCHITECTURE.md` (current design) and
`pi-self-learning-loop.md` (the usage-driven improvement loop this makes real).

---

## 0. Why this exists

Alexandria was built and measured as a single-user personal knowledge engine. The
stated destination is different: **a system multiple users can use simultaneously and
concurrently, sellable into enterprise settings.** Everything below follows from that
one premise change.

The audit of 2026-08-11 found the gap is not in the algorithms — retrieval, caching and
synthesis all follow the intended five-part RAG architecture (ingestion → embeddings →
retrieval/generation → caching → monitoring). **The gap is in the boundaries.** Boundaries
are the expensive thing to retrofit; index types are not. This spec is therefore ordered
by retrofit cost, not by difficulty.

### The ordering principle

> Spend effort now on what is expensive to add later. Defer what is additive.
>
> - **Expensive later:** tenant scoping (touches every query path, cache key, audit row,
>   and index), auth, deletion semantics. Retrofitting these is a rewrite.
> - **Cheap later:** ANN index type (`create_index` is additive), sharding strategy
>   within a tenant, model swaps. Deferring these costs nothing.

---

## 1. Part A — The tenant boundary (**security-critical, do first**)

### A1. The live defect

`ResponseCache.key()` composes `(schema_version, "a", question, model, k,
prompt_version, generation)`. It carries **no scope dimension**. Two tenants asking the
same question resolve to the same cache row, so one tenant receives an answer
synthesized from another tenant's private documents, citations included.

`QueryCache.key()` does include `filters` and is safe *provided* tenant identity is
expressed as a filter — which is a convention, not an enforcement.

**Requirement A1:** every cache key carries an explicit `tenant` part. Not via `filters`,
not by convention — a required positional argument, so a caller cannot omit it silently.

**Gate A1:** a test that stores a response for tenant A and asserts a lookup as tenant B
misses. This test must fail against today's code.

### A2. Scope must be structural, not a filter

A filter is something a caller can forget. For a multi-tenant system holding customer
documents, "we always remember to pass the filter" is not a security posture.

**Requirement A2:** tenant is resolved once at the entry boundary (the serving layer or
CLI invocation) into a scope object that is threaded through retrieval, synthesis,
caching, and audit. No component reaches for a global corpus path.

**Requirement A3:** **physical separation by default** — one index per tenant, not one
index with a tenant column. This buys, in a single mechanism:

- isolation that cannot be defeated by a forgotten filter
- deletion / right-to-be-forgotten as `drop the index`
- blast-radius containment (one tenant's reindex cannot corrupt another's)
- per-tenant index sizes small enough that cheap index types remain correct
- **per-tenant policy tuning** (Part C) — the same mechanism that makes isolation work
  makes the learning loop possible

**Gate A3:** a test asserting that a query issued in tenant A's scope cannot return a
chunk id belonging to tenant B, with the tenant filter deliberately omitted.

### A4. Audit and attribution

`monitor.py` logs `client`, but **every recorded query to date logs `client='cli'`** —
attribution is structurally broken. Under multi-tenant this is both a compliance gap
("who accessed what") and a corruption of the training signal in Part C.

**Requirement A4:** audit rows carry resolved `tenant` and a real `client`/principal.
Attribution correctness is a precondition for Part C, not a nice-to-have.

**Gate A4:** distinct callers produce distinct `client` values in the audit log; no row
records a default.

---

## 2. Part B — The serving surface

There is no HTTP endpoint in the codebase, although the README promises "anything that
can call an HTTP endpoint". Only an in-process harness extension can consume Alexandria
today.

**Requirement B1:** one service — `alexandria serve` — exposing search and answer over
HTTP. Standard library only, consistent with the project's dependency posture.

**Requirement B2:** the serving layer is where tenant resolution, authentication, and
rate limiting live. It is the only component that maps a credential to a scope.

**Explicitly rejected:** splitting into multiple containerized services (core / panel /
knowledge / proxy). At the current stage that multiplies operational surface without
buying isolation that per-tenant indexes do not already provide. The offline/online split
the architecture requires — ingestion must never touch the request path — is already
enforced by ingestion being a separate process invocation.

**Deferred, with a named trigger:** embeddings-as-a-service, if and when the embedder
needs to run on separate hardware from the query path.

### B3. Concurrency

Today every invocation is a fresh process. A serving layer introduces concurrent readers
and writers against SQLite and the vector store for the first time. `check_same_thread=False`
already appears in the embedding cache.

**Requirement B3:** define and test the concurrency model — connection handling, write
locking, and behaviour under simultaneous read + reindex.

**Gate B3:** a concurrent load test (N readers during an active reindex) with zero
corrupted reads and no lock-timeout errors.

---

## 3. Part C — Closing the loop (the learning system)

**Goal, in the principal's framing:** the memory stays fixed; the *saving*, *retrieval*,
and *generation* policies adapt to the underlying memory in question.

This is **learned retrieval policy over a fixed corpus** — not model fine-tuning, not
corpus mutation.

### C1. The signal already exists and is being discarded

Answer route traces record which retrieved chunks were actually **cited** in the final
answer. That is an implicit relevance label, produced free, on every real query. ~1,961
queries are already logged.

**Requirement C1:** extract `(query, chunk_id, rank, was_cited)` tuples from route traces
into a durable, per-tenant implicit relevance set.

This produces a golden set that grows on its own, versus the 49 hand-authored queries.

### C2. What becomes tunable

Once labels exist, these stop being magic constants:

| Knob | Today | Notes |
|---|---|---|
| RRF fusion weights | fixed | BM25 vs dense balance differs per corpus type |
| `wiki_boost` | `1.25` | never measured |
| rerank depth | fixed | cost/quality tradeoff |
| `k` (chunks to LLM) | `seed_k=8` | the spec's 5–10 band; optimum likely varies |
| chunk strategy | one policy | a code corpus and a contract corpus differ |
| enrichment targeting | all docs | hypotheticals are most valuable on zero-overlap docs |

**Per-tenant policies are the point.** The same mechanism that isolates tenants (Part A)
lets each tenant carry its own tuned policy — a genuine product differentiator, and the
literal expression of "tailored toward the underlying memory in question".

### C3. Method — offline tuning, not online RL

**Requirement C3:** collect → tune offline → validate against a held-out, human-authored
golden set → ship a policy change only on measured improvement.

**Explicitly rejected:** online bandits / exploration against live traffic. Exploration
means deliberately serving worse results to real users; unacceptable in an enterprise
setting, and unnecessary while offline labels are abundant.

### C4. The bias that must be designed for, not discovered

The citation signal is **exposure-biased**: only retrieved chunks can be cited, so naive
training reinforces whatever the current policy already surfaces.

**Requirement C4:** log the full retrieved set with ranks (not only the cited subset), and
apply either inverse-propensity weighting or a held-out exploration slice. A learning loop
built on unweighted citation counts will confidently converge on its own blind spots.

### C5. Freshness — the failure this system just experienced

The weekly loop never ran successfully once (a missing `mkdir -p` meant every redirect
failed before its command executed). The corpus froze for three days and **no gate fired**,
because recall@k on a fixed golden set stays green forever on a frozen corpus.

**Requirement C5:** a staleness metric — per tenant, the age of the newest indexed
document — that fails loudly past a threshold.

**Gate C5:** freezing a test corpus past the threshold produces a failing check.

> This is the general lesson worth encoding: **quality metrics do not detect liveness
> failures.** A system can be perfectly accurate about stale data.

---

## 4. Part D — Index strategy

Deliberately deferred, with named triggers so the deferral is falsifiable rather than
ambient.

| Chunks per tenant | Index | Rationale |
|---|---|---|
| **< ~200k** (current: ~40k) | **Flat** | Exact; no build cost; no recall loss; milliseconds at this size |
| **200k – 10M** | **IVF** | Additive `create_index`; `nprobes` is a tunable recall knob |
| latency-critical | HNSW | Better latency/recall than IVF, but RAM-hungry and hostile to incremental update — poor fit for a frequently reindexed corpus |
| **memory-bound only** | PQ | **Avoid by default.** PQ trades recall for memory. The zero-overlap band is the weakest measured surface (38.9%); quantization degrades exactly that, to solve a constraint not currently present |

**Distributed ANN is deliberately not planned.** It addresses "one corpus exceeds one
node" — a single-namespace, web-scale problem. Enterprise knowledge scales on *number of
tenants*, each modest. Sharding by tenant addresses that axis and delivers isolation,
deletion, and per-tenant tuning in the same mechanism.

**Re-entry trigger:** a single tenant exceeding ~10M chunks, or a requirement for genuine
cross-tenant search.

---

## 5. Part E — Cache architecture for multi-tenant

An earlier draft of this analysis claimed multi-tenancy "inverts the caching
advantage", because every new tenant starts cold and the cold path is 25–33s.
**That was a diagnosis mistaken for a conclusion.** The cold-start problem is real,
but it is an argument for building the cache properly, not for accepting the
regression. This part specifies the proper build.

### E1. Shareability is a lattice, and provenance already computes it

The three cache layers do **not** have the same tenancy requirements. Treating them
uniformly is what makes multi-tenant caching look impossible.

| Layer | Key today | Correct scope | Why |
|---|---|---|---|
| **Embedding** | `sha256(model + revision + mode + text)` | **global** | Content-addressed. The embedding of a given string is identical for every tenant. No tenant dimension is needed or wanted |
| **Query** (query → chunk ids) | includes `filters` | **tenant** | The result *is* a set of that tenant's chunk ids. Never shareable |
| **Response** (question → answer) | no scope dimension | **derived** — see E3 | Depends entirely on what fed the answer |

**Requirement E1:** scope is a per-layer property, not a global policy.

### E2. The embedding cache should be shared, and it is the cold-start fix

The embedding cache is already keyed on a content hash with no tenant component, so
it is *already* correct for global sharing — no redesign needed, only a decision to
place it outside the tenant boundary.

This matters more than it sounds. A new tenant's onboarding cost is dominated by
embedding their corpus. Enterprise corpora overlap heavily on non-private material:
standards, licences, vendor documentation, framework READMEs, boilerplate policy.
Every byte of that overlap is embedding work already paid for.

**Requirement E2:** one shared, content-addressed embedding cache across tenants.

**Known side channel, accepted with rationale:** a cache hit reveals that *some*
tenant has previously embedded that exact string. Exploiting it requires already
possessing the exact text, so the disclosure is "someone else also holds this
document" — not its contents. For deployments where even that is unacceptable
(competitors in one installation), the cache partitions by tenant-group. Default
shared; partitioning is a deployment option, not a rewrite.

### E3. Response-cache scope is the join of its citations

This is the part the existing architecture makes possible and that a
less-instrumented system could not do safely.

Alexandria records **per-claim citations** — for any synthesized answer it knows
exactly which chunks fed it. Therefore an answer's cache scope is computable rather
than assumed:

> **scope(answer) = join(scope(chunk) for every cited chunk)** — the most restrictive
> scope among its sources.

So if a corpus is layered into shared material (public standards, vendor docs) and
tenant-private material, then an answer citing **only** shared chunks is safely
cacheable **globally**, and every tenant after the first gets it warm. An answer
citing even one private chunk is tenant-scoped, automatically.

This converts the provenance system from a trust feature into a performance feature.
The same instrumentation that makes citations auditable is what makes cross-tenant
cache sharing provably safe — the sharing decision is derived from recorded fact, not
from a human remembering to set a flag.

**Requirement E3:** response-cache entries carry a scope computed from their citation
set. Default deny — an answer with unknown or unrecorded provenance is tenant-scoped.

**Gate E3:** an answer citing one private chunk must not be served to another tenant,
asserted by test. An answer citing only shared chunks must be, and the test must show
the second tenant's request was a hit.

### E4. The generation counter is global — one tenant's reindex evicts everyone

`cache.py:26` defines `GENERATION_FILE = ".alexandria/index/generation.json"` — a
single counter for the whole installation. Cache validity is bound to it, which is the
right mechanism (monotonic invalidation beats TTL guessing) applied at the wrong
granularity.

Under multi-tenant, **any** tenant reindexing bumps the shared counter and invalidates
**every** tenant's cached queries and answers. With continuous ingestion across many
tenants, the caches would approach a permanent cold state — the pathological case where
the system pays full cache cost and receives no cache benefit.

**Requirement E4:** per-tenant generation counters. The embedding cache keys on model
revision instead, since content-addressed entries never go stale.

**Gate E4:** tenant A's reindex leaves tenant B's cache hits intact.

### E5. No eviction policy exists

`cache.py` contains no LRU, no size ceiling, no per-tenant quota — the only deletion
path is `DELETE FROM cache`, a full clear. Single-user, unbounded growth is a slow
non-problem. Multi-tenant, it is a **noisy-neighbour** vector: one tenant with a large
corpus and heavy traffic consumes shared cache capacity that other tenants paid for.

**Requirement E5:** per-tenant cache quotas with per-tenant eviction. A tenant's
eviction pressure must not be a function of another tenant's volume.

### E6. Warm the cache off the request path

The architecture's core principle is that expensive work happens offline, never at
request time. Applied to onboarding: a new tenant's first queries should not be the
first time anything is computed.

**Requirement E6:** an onboarding warm-up pass — embed the corpus (largely served by
E2), then pre-run a seed query set derived from the tenant's own corpus structure
(most-linked documents, wiki entry points, cluster centroids). Answers land in cache
before the tenant's first real query.

This makes cold-start a **provisioning** cost, paid once, off the request path — rather
than a latency cost paid by the customer during evaluation.

### E7. Single-flight

Every invocation is currently a separate process, so identical concurrent requests have
never been possible. A serving layer (Part B) makes them routine, and an
uncoordinated cache miss on a popular query means N simultaneous identical LLM calls.

**Requirement E7:** single-flight — one computation per key, other waiters attach to it.

### E8. Measure the cold path as a first-class number

A fleet-wide cache-hit-rate average is dominated by the longest-lived tenant and will
look healthy while every new tenant has a poor experience. It is a metric that conceals
precisely the failure it should surface.

**Requirement E8:** cache metrics segmented by tenant age, and **time-to-first-useful-answer
for a new tenant** tracked as a gate. Warm-path latency alone is a vanity metric in a
multi-tenant product.

---

## 6. Part F — The untrusted-content boundary (prompt injection)

### F1. The current state

`synthesis/write.py:_writer_prompt()` interpolates retrieved text directly:

```python
lines = [f"<topic>{topic_query}</topic>", "<gathered_pool>"]
for chunk in chunks:
    lines.extend((f'<chunk doc_id="{chunk.doc_id}" chunk_id="{chunk.chunk_id}">',
                  chunk.text,
                  "</chunk>"))
```

`chunk.text` is inserted raw. A grep across `src/alexandria/synthesis/` for
`injection|untrusted|sanitiz|jailbreak` returns nothing. Two distinct defects:

1. **Delimiter escape.** A document containing `</chunk></gathered_pool>` terminates the
   data region and everything after it is read as prompt structure.
2. **No untrusted framing.** `WRITER_SYSTEM` instructs the model to answer "using only
   the supplied sources". That is a *grounding* instruction — it raises the model's
   deference to document content. Nothing anywhere tells the model that source content
   is data to be summarized, never instructions to be followed.

The same pattern applies to `gather.py:_gap_prompt()` and `repair.py:_repair_prompt()`.

### F2. Why the blast radius is larger than it looks

Stated honestly: the synthesis LLM holds no tools, so an injected instruction cannot
directly exfiltrate or execute. The immediate damage is a poisoned answer.

But the risk does not stop there, because of what Alexandria is *for*:

1. A poisoned answer arrives **carrying citations**, inheriting the credibility of the
   provenance system. The differentiator amplifies the attack.
2. Alexandria's purpose is **feeding agent sessions**. Those agents do hold tools. The
   real chain is *poisoned document → poisoned answer → agent acts on it*, and the
   effective blast radius is the downstream agent's tool access, not this LLM's.

In enterprise, ingestion is the attack surface: a hostile wiki edit, a crafted PDF, a
malicious message in an indexed archive. Ingestion is exactly the surface the product
is meant to expand.

### F3. Requirements

- **F3a** — encode or strip delimiter sequences in chunk text so content cannot escape
  its data region. Structural, not a blocklist of known attack strings.
- **F3b** — explicit untrusted-data framing in every system prompt that receives
  retrieved content: source text is material to be summarized and cited, and any
  instruction appearing inside it is data, not a directive.
- **F3c** — apply to all three prompt builders (`write.py`, `gather.py`, `repair.py`),
  not only the writer. One unguarded path is the whole surface.
- **F3d** — record injection-suspicion in the route trace, so the monitoring loop
  (Part C) can observe attempts rather than only successes.

**Gate F:** a corpus fixture containing a hostile chunk — delimiter escape plus an
embedded instruction — produces an answer that does not follow the instruction. This
test must fail against today's code.

**Not claimed:** these measures reduce injection risk, they do not eliminate it. No
known prompt-level defense is complete. The durable mitigation is that the synthesis
LLM holds no capabilities, which must stay true — **do not give the synthesis path tool
access.**

---

## 7. Part G — Measurement discipline

This part specifies process, not code, and it is here because the audit found it to be
the project's deepest systemic issue.

### G1. The pattern found

Across phases, a gate was set, the run missed it, and **the gate moved**:

- Phase 3's cap was rewritten `0.20 → 0.40` after two INVALID runs.
- Phase 2's threshold moved `45 → 80 → 85 → 70`, finished FINAL_FAIL, and the
  certified 97.1% is a post-hoc stratum that excludes the failing cluster.
- Phase 1's rebuild gate says `<30min`; the measurement is ~80min; the row reads green.
- Phase 0's gate was never measured at all.

The result is that 0 of 6 phase rows are fully solid against their own gates while all
six read as certified.

### G2. Why this is the most dangerous item in this document

It is **self-concealing**. Every other defect in this spec leaves evidence — a leak, a
stale corpus, a slow query. A moved gate leaves behind an artifact that looks like a
pass. The failure mode is invisible from inside.

It also contradicts the product's own value proposition. Alexandria sells verifiable
provenance. A measurement regime whose thresholds are set after seeing results cannot
support that claim, and an enterprise buyer who finds one moved gate has grounds to
discount every number in the repository.

### G3. Requirements

- **G3a** — a gate is frozen in a commit **before** the run it judges. A threshold
  chosen after seeing results is not a gate; it is a description.
- **G3b** — a failed run leaves a **FAIL row that persists**. Failures are never
  deleted, overwritten, or retried into a pass. The record of what did not work is what
  gives the passes meaning.
- **G3c** — post-hoc stratification is a **finding, never a certification**. "97.1% on
  subset X, 70% overall" is honest and useful. "97.1%" alone is not.
- **G3d** — every certified number carries a pointer to the commit that froze its gate
  and the run that produced it, so a third party can re-derive it.

**Gate G:** an outside reader, given only the repository, can verify for any certified
number which commit froze the gate, which run produced the value, and whether earlier
runs failed. This is the same audit trail an enterprise buyer will ask for, so it is
not overhead — it is a deliverable built early.

---

## 8. Part H — Dependability

### H1. The tell

The corpus was frozen for three days and no human noticed. That is not a monitoring
finding; it is a usage finding. A system genuinely load-bearing in daily work surfaces a
dead corpus within hours, because someone asks it something recent and gets nothing.

Read together with the other measured facts — only one harness can reach it, 25–33s
cold, 2 of 8 recent queries exceeding a 30s timeout, all 1,961 queries logging
`client='cli'` — the picture is a system that is **built and measured but not yet
depended upon.**

### H2. Why this outranks features

A product nobody depends on cannot be sold as dependable, and the real friction is
undiscoverable until something breaks that was actually needed. Every defect in this
spec was found by audit rather than by use — which is exactly what one would predict
for a system used lightly.

It is also currently *unreasonable* to depend on it: no HTTP surface exists, so the
system cannot be reached from any tool but one. Dependability is blocked on Part B, not
on discipline.

### H3. Requirements

- **H3a** — after Part B ships, Alexandria becomes the default retrieval path for its
  own maintainers' daily work, across more than one client.
- **H3b** — track first-party usage as a metric. Sustained low usage is a product
  signal, not a discipline failure, and should be read as such.
- **H3c** — until H3a holds, the freshness alarm (C5) is the **only** thing standing
  between a dead corpus and a customer discovering it. Treat it as load-bearing
  infrastructure rather than a nice-to-have.

---

## 9. Non-goals

- Rewriting retrieval, synthesis, or caching. The five-part architecture is implemented
  and measured; this spec adds boundaries around it.
- Multi-container decomposition (see B2).
- Online reinforcement learning against live traffic (see C3).
- Adopting an external memory engine (see the 2026-08-11 comparison).
- Eliminating prompt-injection risk (see F3). Reducing it is in scope; claiming
  elimination is not.
- Giving the synthesis LLM tool access — explicitly and permanently out of scope, since
  its lack of capability is the durable bound on injection blast radius.

---

## 10. Gates

Per project doctrine, phases advance on measurement, not on feeling done.

| # | Gate | Evidence |
|---|---|---|
| G1 | Cross-tenant cache isolation | Tenant B lookup misses a tenant A response; test fails against today's code |
| G2 | Cross-tenant retrieval isolation | Tenant A scope cannot surface tenant B chunk ids with filters omitted |
| G3 | Attribution correctness | Distinct callers produce distinct audit `client` values; no defaults |
| G4 | Concurrency safety | N concurrent readers during reindex: zero corrupt reads, zero lock timeouts |
| G5 | Staleness detection | A frozen test corpus produces a failing freshness check |
| G6 | Learning loop improves something | A policy tuned on implicit labels beats the current default on the held-out human golden set — **or is reverted**. No retry-until-success |
| G7 | Citation-derived cache scope | An answer citing one private chunk is never served cross-tenant; an answer citing only shared chunks is, and hits |
| G8 | Generation isolation | Tenant A's reindex leaves tenant B's cache hits intact |
| G9 | Cold-start budget | Time-to-first-useful-answer for a freshly provisioned tenant, measured and bounded |
| G10 | Injection resistance | A hostile chunk (delimiter escape + embedded instruction) does not steer the answer; fails against today's code |
| G11 | Gate auditability | For any certified number, an outside reader can identify the freezing commit, the producing run, and any prior failures |

---

## 11. Open questions

1. **Tenant identity model** — is a tenant an organization, a workspace, or a user? This
   decision propagates into every scope key and is expensive to revise.
2. **Enterprise ingestion surface.** Current connectors read harness-native local stores.
   Enterprise sources (wikis, chat, document stores, drives) are the actual product
   surface and are entirely unbuilt.
3. **Embedding model migration.** Changing the embedder invalidates every vector. With
   many tenants this needs a staged re-embed story, not a global rebuild.
4. **Cost attribution and rate limiting per tenant.** LLM spend is per-request; enterprise
   buyers expect caps and chargeback.
5. **Exposing provenance to end users.** Per-claim citation and route traces are the
   differentiator; they are currently only visible in a static site renderer, not through
   any consumer-facing surface.
