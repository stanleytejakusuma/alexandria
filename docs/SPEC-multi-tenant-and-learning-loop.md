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

## 5. Non-goals

- Rewriting retrieval, synthesis, or caching. The five-part architecture is implemented
  and measured; this spec adds boundaries around it.
- Multi-container decomposition (see B2).
- Online reinforcement learning against live traffic (see C3).
- Adopting an external memory engine (see the 2026-08-11 comparison).

---

## 6. Gates

Per project doctrine, phases advance on measurement, not on feeling done.

| # | Gate | Evidence |
|---|---|---|
| G1 | Cross-tenant cache isolation | Tenant B lookup misses a tenant A response; test fails against today's code |
| G2 | Cross-tenant retrieval isolation | Tenant A scope cannot surface tenant B chunk ids with filters omitted |
| G3 | Attribution correctness | Distinct callers produce distinct audit `client` values; no defaults |
| G4 | Concurrency safety | N concurrent readers during reindex: zero corrupt reads, zero lock timeouts |
| G5 | Staleness detection | A frozen test corpus produces a failing freshness check |
| G6 | Learning loop improves something | A policy tuned on implicit labels beats the current default on the held-out human golden set — **or is reverted**. No retry-until-success |

---

## 7. Open questions

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
