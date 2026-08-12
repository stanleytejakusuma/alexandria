# BACKLOG

Single list of open work. Opened 2026-08-11 after a full-system audit found open items
scattered across seven work orders with no index. **Re-ordered 2026-08-11** after two
adversarial audit passes and the deployment-model decision.

**Strategy:** single-tenant per install (on-prem / customer VPC). See
`SPEC-multi-tenant-and-learning-loop.md` §12. The stated requirement — "multiple users
simultaneously and concurrently" — is a **concurrency** requirement, not a
**tenant-isolation** one. Structural multi-tenant work is deliberately deferred; the
fundamentals it was sitting in front of are not.

**Ordering principle:** cheap and load-bearing first. Prefer what is expensive to retrofit
(boundaries, durable signal) over what is additive later (index types).

Audit sources: `/tmp/alx/audit/FINAL-GAP-PASS.md` (pass 1, 29 findings),
`/tmp/alx/audit/PASS2-CORRECTIONS.md` (pass 2, corrections + strategy).

---

## Top 10 — the critical path

| # | Item | Why now | Blocks |
|---|---|---|---|
| 1 | **SQLite concurrency: WAL + `busy_timeout` + per-connection handling** (N4) | ~30 lines, and it is the literal prerequisite for concurrent users — the actual reading of the requirement | `serve`, every concurrency test, #2 |
| 2 | **Generation-counter correctness** (N2 + N3) — re-read per query, locked bump | Two of the cheapest P1 fixes in the repo. `SearchEngine._generation` is frozen at construction, so a `serve` process serves stale answers after reindex; the bump is an unlocked read-modify-write that loses invalidations | Any trust in cache correctness once `serve` exists |
| 3 | **`alexandria serve`, single-tenant** | The only unlock that makes Alexandria something a customer can run against. Per §12 it does **not** need tenant-scope machinery first — only #1, #2, and ordinary per-user auth | Every "sellable" claim; dogfooding (§H) |
| 4 | **Cost ledger** (N8) + **move the UUID out of the system prompt** (N9) | `last_usage` is already parsed and never read (`llm.py:167`). Cheapest fix that gives cost visibility; today not even one logged run can be costed | Any "low opex" claim procurement would test |
| 5 | **Enrichment injection framing + retrieval-poisoning fix** (spec §F, corrected) | Framing is a string change; filter and invalidate-hatch are small. Hard blocker the moment any third-party document enters the pipeline — i.e. the first customer, not the second | #14 (enterprise connectors). Land before, not concurrently |
| 6 | **Deletion / erasure path** (N1) — delete verb, index reconciliation, embedding-cache escape hatch | Erasure survives in ≥6 unlinked copies; there is no code path at all. Any single customer can ask for their data back — this is not a multi-tenant-only problem | Any DPA a real customer signs |
| 7 | **Backup/restore of `.alexandria` state** (N5) — queries, audit, eval, state, generation (not the rebuildable indexes) | Total loss today permanently destroys the learning-loop training signal (#9). Cheap relative to what it protects | #9's credibility; incident recovery |
| 8 | **Real attribution, structurally verified** (N16 + pass-2 authz) | Today's `--user`/`--caller` is **worse than absent**: a forgeable, plausible-looking audit trail. Fix at the `serve` boundary as a verified value, never a passthrough string | Audit-log claims, chargeback, learning-loop attribution |
| 9 | **Citation-linkage build-out** (spec §C1, corrected) — `query_id` return, durable `(query_id, claim_id, doc_id, chunk_id, rank, verdict, source_round)` in `answers.jsonl` | The actual precondition for the learning loop. Both #12/#14 below and spec E3 currently rest on a false premise — citations are discarded at `cli.py:454` | Learning loop entirely; spec E3 |
| 10 | **Procurement floor** — `SECURITY.md`, published threat model, CI against `uv.lock` not floating specifiers, honest RTO (N18: measure one real rebuild, correct the false `<30min` in `README.md:116`) | Zero procurement artifacts exist. This is the gate a buyer hits *before* technical evaluation | Any actual sale |

---

## P1 — Also open, not on the critical path

| # | Item | Detail |
|---|---|---|
| 11 | **Verify the weekly loop actually runs** | `mkdir` fixed in `315418b` but **unverified in the wild** — the loop has never once completed. Do not trust it until one real run produces a digest |
| 12 | **Re-sync the frozen corpus** | Ingestion stopped ~Aug 8; the catch-up is still explicit work |
| 13 | **Staleness metric** (spec C5) | Age of newest indexed doc, failing loudly past a threshold. The gate that would have caught #12 on day one |
| 14 | **Measurement preconditions** (spec C6) | Three instances in one day of a correct metric measuring an unintended state. An eval invoked mid-index must refuse to report, not emit a delta |
| 15 | **Non-atomic rebuild** (N10) | A crashed rebuild serves stale answers silently; `--rebuild` appears in zero tests (N26), with no resume marker |
| 16 | **Hardcoded `~/alexandria-corpus`** (N6) in two signatures bypasses config; **owner-machine paths as CLI defaults** (N7) risk ingesting personal agent data |
| 17 | **Model supply chain unpinned** (pass 2) | A same-name weight swap on HuggingFace is invisible and would corrupt every vector |
| 18 | **No observability** (N19) | An operator would see nothing on failure |
| 19 | **Embedder change = global rebuild** (N11); same-dimension swaps silently write mixed-model vectors |
| 20 | **Golden set not in the repo** (N20) — the certification gate is unreproducible by anyone but the author |
| 21 | **Golden set structurally blind** (N21) — zero negative cases, recall-only, n=49. G6 is meaningless until this is fixed |
| 22 | **Nothing real is exercised** (N22) — real models, transport, and JSON contracts are never tested |
| 23 | **TTLs are decorative** (N13) — nothing is ever purged; `cache/embeddings.sqlite` is already 4.45 GB |
| 24 | **Audit log is empty** — `.alexandria/audit/answers.sqlite` and `state/queries.sqlite` are both 0 B. A log that exists but never records is worse than none |
| 25 | **No PII/secret detection at ingestion** (N14); **no encryption at rest** (N15) |
| 26 | **Gate-freezing discipline** (spec §G) | Gates frozen before runs; failures persist; post-hoc strata are findings, never certifications |

---

## P2 / P3 — Deferred, with named triggers

| # | Item | Trigger |
|---|---|---|
| 27 | **Tenant scope object** and **per-tenant sharding** (spec A2/A3) | **Not** "a second customer org". The real trigger is **the first customer requiring intra-organization document ACLs** — see spec §12.4. Ask this in the first sales conversation |
| 28 | Per-tenant generation counters (E4), cache quotas (E5), shared embedding cache (E2) | Same trigger as #27. The global counter is *correct* for one install |
| 29 | Offline policy tuning (spec C2–C3) | Blocked on **both** #9 (citation linkage) and #21 (golden set needs negative cases and a significance bar) |
| 30 | Wikilink-graph traversal | Targets the 38.9% zero-overlap band — highest-value external idea found. Unblocked, but below the critical path |
| 31 | Split bulk vs interactive LLM endpoints | Config, not services (~20 lines) |
| 32 | Phase-2 full sweep | `WORK-ORDER-phase2-full-sweep.md` written, never dispatched |
| 33 | Fresh-clone step 4/5 hard-fails | `wiki-site` exits 2, masked by `|| true` |
| 34 | CJK chunker under-count ~3× (N23) | No-tokenizer fallback path |
| 35 | IVF index | A corpus crosses ~200k chunks (currently ~40k pre-enrichment, ~125k after) |
| 36 | HNSW / PQ / distributed ANN | See spec §4. PQ avoided by default — it degrades the weakest measured surface |
| 37 | Enterprise ingestion connectors | Blocked on #5 (injection guard). Do not open a third-party ingestion path first |
| 38 | Proxy pattern; conversational distillation | Below the line until `serve` exists |
| 39 | Orphan 0 B `enrichment.sqlite` at corpus root (N28/pass-2 P3) | Ops-confusion hazard — the real DB is at `index/enrichment.sqlite` (26.9 MB). Already caused one auditor disagreement |

---

## Documentation debt

| # | Item |
|---|---|
| 40 | **Phase certification language** — 0 of 6 phase rows fully solid against their own gates while all read as certified. Re-measure or downgrade |
| 41 | **Phase-3 harness doc vs shipped extension** — decision says "two tools, nothing else"; four ship |
| 42 | **`bb7923b` "pre-registered" claim** — the correction lands only in follow-up `35c9e80` |
| 43 | **`README.md:201`** promises an HTTP endpoint that does not exist (closed by #3) |

---

## Closed by audit

- ~~Log the full retrieved set with ranks~~ — **already implemented**, 2,030/2,030 rows populated.
- ~~Add untrusted-data framing to `write.py`/`gather.py`/`repair.py`~~ — **already present** since 2026-08-05/07. The gap is `enrich.py` only (#5).

---

## Status of the Top 10 after the write-path package (2026-08-12)

The write-path/serve package (`docs/SPEC-write-path-and-serve.md`, commits
`d8f9589`…`18bfad2`) closed or moved several critical-path items. The table
above is **not** self-updating — this is the corrected status, verified against
code by two independent audits.

| # | Item | Status now |
|---|---|---|
| 1 | SQLite WAL + `busy_timeout` | **Partial.** WAL + explicit pragma on every writer (bm25, cache, embedder, monitor, sqlite store). But measured: CPython's `sqlite3.connect()` already defaults `busy_timeout` to 5000ms, so the pragma is redundant with the stdlib default and is belt-and-braces, not the mechanism. Narrow first-write race documented at `index/bm25.py`; unreachable on the promote path because §4.2's flock serialises it. |
| 2 | Generation-counter correctness | **Closed.** Re-read per access (`500cd9e`), locked bump + atomic write + fail-loud on corruption (`87f12df`). |
| 3 | `alexandria serve`, single-tenant | **Closed.** `src/alexandria/serve.py`, gates S0–S10. Also closes #43 (`README.md:201`'s promised HTTP endpoint now exists). |
| 4 | Cost ledger | **Partial.** `usage` table records model + prompt/completion/total tokens + cache_read per `answer_id` (`d8f9589`). Deliberately **no dollar figure** — no pricing table exists in the repo and inventing one would fabricate a number nobody measured. §9's F5 says "tokens and cost"; only tokens ship. |
| 5 | Enrichment injection framing | **Untouched.** Still open. |
| 6 | Deletion / erasure path | **Untouched.** Explicitly out of scope (§10.1) pending a policy decision: does erasure include the audit trail and git history, or stop at the retrievable surface? |
| 7 | Backup/restore of `.alexandria` state | **Closed.** `src/alexandria/backup.py`, gate B1. State only, never the rebuildable indexes; restore is allowlisted and traversal-safe. |
| 8 | Real attribution, structurally verified | **Advanced, still open.** `serve` now derives identity from the socket (one Unix socket per identity; TCP → reserved `local-anonymous`) and never from the request body, and the inbox sink rejects payloads that could forge entry structure or attribution (`1386c46`, extended to `from_`). What remains: `--user`/`--caller` are still unverified passthrough hints on the CLI path, which is the "worse than absent" trail this item names. |
| 9 | Citation-linkage build-out | **Untouched.** Still open. |
| 10 | Procurement floor | **Untouched.** Still open. |

### P1 items closed since (2026-08-13)

| # | Item | Status now |
|---|---|---|
| 17 | CLI attribution | **Closed** by `664d896` — `--user` deleted rather than validated (it defaulted to an env var, was written verbatim to `search.jsonl`, and nothing consumed it); identity now derived from the OS user. This is the CLI half of #8. |
| 21 | Golden set structurally blind | **Substantially closed** by `3357fdd`. 22 hand-verified negative cases now exist and the gate fires on precision, not just recall: `regressions()` reports named unanswerable queries the engine grew more confident about. Measured separation — positive top-1 median 0.9819 vs negative 0.0238 — resolved spec Q5 (relevance floor 0.12). **What remains:** n=49 is still underpowered and there is still no significance bar, so a small recall move is not distinguishable from noise. #29 (offline policy tuning) is unblocked on the precision half only. |
| 20 | Golden set not in repo | **Unchanged, now wider.** `negative-v1.jsonl` also lives in the corpus rather than the repo, so neither half of the gate is independently reproducible by a third party. |
