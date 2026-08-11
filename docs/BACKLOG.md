# BACKLOG

Single list of open work. Opened 2026-08-11 after a full-system audit found open
items scattered across seven work orders, three decision docs, and no index.

**Ordering principle:** by retrofit cost, not by difficulty. Boundaries first
(expensive to add later), then blind spots (cheap, and everything else depends on
seeing), then features, then deferred items with named triggers.

Legend: **P0** liability · **P1** blocks other work · **P2** valuable · **P3** deferred

---

## P0 — Liabilities

| # | Item | Detail |
|---|---|---|
| 1 | **Cross-tenant response-cache leak** | `ResponseCache.key()` carries no scope dimension; two tenants asking the same question share a cache row. Harmless at one user, a breach at two. `SPEC-multi-tenant-and-learning-loop.md` §A1 |
| 2 | **Verify the weekly loop actually runs** | `run-weekly-loop.sh` missing-`mkdir` fixed in `315418b`, but the fix is **unverified in the wild** — the loop has never once completed successfully. Do not trust it until one real run produces a digest. |
| 3 | **Re-sync the frozen corpus** | Corpus stopped ingesting ~Aug 8. Even with the loop fixed, the three-day gap needs an explicit catch-up sync. |

---

## P1 — Blind spots (everything else depends on measuring correctly)

| # | Item | Detail |
|---|---|---|
| 4 | **Staleness metric** | Age of newest indexed doc, per scope, failing loudly past a threshold. This is the gate that would have caught #3 on day one. Quality metrics cannot detect liveness failures. |
| 5 | **Fix caller attribution** | All 1,961 logged queries record `client='cli'`. Per-consumer and per-tenant analysis is impossible, and it is already corrupting the signal the learning loop would train on. |
| 6 | **p50 latency measures the wrong thing** | Eval now records 0.4–1ms cache lookups, so the `<500ms` gate is unobservable rather than passing. Needs a cold-path measurement, or the gate is theatre. |
| 7 | **MRR drifted −3.1% with the gate green** | The regression gate did not fire on a real quality drop. Either the threshold is too loose or the metric is the wrong one. |

---

## P2 — Build

| # | Item | Detail |
|---|---|---|
| 8 | **`alexandria serve`** | One stdlib HTTP service. Top unlock: no consumer except in-process Pi can reach Alexandria today, though `README.md:201` promises "anything that can call an HTTP endpoint". Also the natural home for auth and tenant resolution. |
| 9 | **Tenant scope object** | Resolved once at the entry boundary, threaded through retrieval / synthesis / cache / audit. Structural, not a filter a caller can forget. Spec §A2–A3 |
| 10 | **Per-tenant index sharding** | Physical isolation; also delivers deletion-as-drop-index and per-tenant policy tuning in the same mechanism. Spec §A3 |
| 11 | **Concurrency model** | A serving layer creates the first concurrent readers/writers against SQLite + vector store. Untested territory. Spec §B3 |
| 12 | **Citation-label extraction** | Route traces already record which retrieved chunks were cited — a free implicit relevance label on every real query, currently discarded. Extract `(query, chunk_id, rank, was_cited)`. Spec §C1 |
| 13 | **Log the full retrieved set with ranks** | Precondition for #12 being unbiased. Only retrieved chunks can be cited, so naive training reinforces existing blind spots. Spec §C4 |
| 14 | **Offline policy tuning** | Tune RRF weights, `wiki_boost` (a `1.25` nobody measured), rerank depth, `k`, chunk strategy — validated against the held-out human golden set. Spec §C2–C3 |
| 15 | **Wikilink-graph traversal** | Borrowed from the Tencent comparison: BFS over wikilinks reaches documents with zero lexical or vector overlap. Directly targets the 38.9% zero-overlap band, which is the weakest measured surface. Highest-value external idea found. |
| 16 | **Split bulk vs interactive LLM endpoints** | As **config, not services** (~20 lines). Enrichment/synthesis is latency-tolerant and wants a cheap model; the answer path is quality-critical. `--base-url`/`--api-key-env` already landed in `da2993d`; per-invocation flag juggling has already caused one slip. |
| 17 | **Phase-2 full sweep** | `WORK-ORDER-phase2-full-sweep.md` was written and never dispatched. The 97.1% figure is a post-hoc stratum that excludes the failing cluster; the sweep is what would make it real. |
| 18 | **Fresh-clone step 4/5 hard-fails** | `wiki-site` exits 2 in the clean-clone path, masked by a `|| true`. The phase-4 "works from a fresh clone" claim rests on a suppressed error. |

---

## P3 — Deferred, with named triggers

| # | Item | Trigger |
|---|---|---|
| 19 | IVF index | A tenant crosses ~200k chunks (currently ~40k). Additive `create_index`; no refactor. |
| 20 | HNSW | Latency becomes critical **and** RAM is cheap **and** reindex frequency drops. Hostile to incremental update. |
| 21 | PQ quantization | Memory-bound only. Trades recall for RAM, and would degrade the 38.9% zero-overlap band — already the weakest surface. Measure the recall cost if ever adopted. |
| 22 | Distributed ANN | A single tenant exceeds ~10M chunks, or genuine cross-tenant search becomes a requirement. Sharding by tenant addresses the actual scaling axis. |
| 23 | Proxy pattern | Inject memory into any OpenAI/Anthropic-compatible client without per-harness extensions. Wait until #8 exists — the proxy is a thin layer over it. |
| 24 | Conversational distillation layer | Compress raw session logs before indexing. Wait for evidence that raw sessions are hurting retrieval. |
| 25 | Enterprise ingestion connectors | Current connectors read harness-native local stores. Wikis / chat / document stores / drives are the actual product surface. Blocked on the tenant model (#9). |

---

## Documentation debt

| # | Item | Detail |
|---|---|---|
| 26 | **Phase certification language** | Audit found 0 of 6 phase rows fully SOLID against their own gates, while docs read as certified. Either re-measure or downgrade the claims — the discrepancy is the problem, not the numbers. |
| 27 | **Phase-3 harness doc vs shipped extension** | The decision text says "two tools, nothing else"; the installed extension ships four, including a working write verb. Reconcile. |
| 28 | **`bb7923b` "pre-registered" claim** | The admission that it was not pre-registered lands only in follow-up `35c9e80`. The original claim should carry the correction inline. |
| 29 | **README endpoint promise** | `README.md:201` promises an HTTP endpoint that does not exist. Fix the README or build #8. |

---

## Open questions

Carried from `SPEC-multi-tenant-and-learning-loop.md` §7 — these block design, not code:

1. **Is a tenant an org, a workspace, or a user?** Propagates into every scope key.
2. **Embedding-model migration.** Changing the embedder invalidates every vector; with many tenants this needs a staged re-embed, not a global rebuild.
3. **Cost attribution + rate limiting per tenant.** Enterprise buyers expect caps and chargeback.
4. **Exposing provenance to end users.** Per-claim citation and route traces are the differentiator, currently visible only in a static site renderer.
