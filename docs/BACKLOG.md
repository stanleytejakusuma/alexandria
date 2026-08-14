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
| 44 | **`alexandria eval` cannot run offline** — `CrossEncoderReranker.rerank()` pulls a ~90MB HuggingFace model on first use, so the quality gate needs network + a download even when the golden set is present. Found 2026-08-13 while closing half of #20: a run with `HOME` redirected hangs on the download rather than failing fast. `scripts/synthetic-eval-gate.py` sidesteps it by constructing the engine with `IdentityReranker` directly, which is why the synthetic gate is clean-clone runnable and the CLI is not. Fix is to degrade gracefully — fall back to identity rerank with a loud warning when the model is unavailable, rather than blocking — but note that changes what `eval` measures, so the fallback must be recorded in the report, not silent |
| 45 | **Real negative set is 21/22 out-of-domain** (the unfinished half of #21) — its negative score median measures topic distance, not precision, so the clean floor derived from it is an artefact of easy negatives. Needs ≥10 in-domain negatives: questions the corpus plausibly *should* answer but genuinely does not. Requires the private corpus, so it cannot be done from the engine repo. `tests/fixtures/synthetic-negative-v1.jsonl` is the worked example of the shape wanted (10 of 12 in-domain, each with a note naming why it is unanswerable and a `verified_against` count the test suite checks for staleness) |
| 46 | **The synthetic gate's negative half reports but does not gate** — `scripts/synthetic-eval-gate.py` computes `separable` and prints it (`negative: n=12 separable=False` on every green run), but `separable` never enters the `failures` list, so no negative-side outcome can turn the gate red. Arguably correct as it stands: spec §9 Q5 established that a *score floor* is the wrong instrument rather than a mistuned one, so gating on separability would re-introduce the disproven instrument. The defect is presentational and therefore dangerous — a printed metric beside a `PASS` reads as an enforced check, and the 12 negative cases are described elsewhere as a "negative set" as though they gate. Either gate on something real (a per-case rule: no named unanswerable query may enter top-k above a positive) or relabel the line as an observable so no future session cites `separable` as a passing check. Found 2026-08-13 while verifying #20's gate could actually fail |
| 47 | **The synthetic gate REWARDS killing the vector store** — not merely blind to it. Measured 2026-08-13: with the dense leg forced empty at `retrieval/search.py:198`, recall@k goes 0.950 → **1.000** and MRR 0.514 → **0.988**, and the gate passes greener than baseline. Only BM25 death is caught (0.275 / 0.095, FAIL). Mechanism: the synthetic gate's `HashEmbedder` is semantically empty, so the dense leg injects noise into the RRF fusion at `search.py:215` and actively degrades BM25's ranking; deleting it removes the handicap. Consequences: (a) no dense-side regression is detectable by this gate, ever; (b) the published baseline 0.950/0.514 is not a quality measure but BM25 carrying a noise penalty; (c) anyone tuning against this gate is led toward deleting the vector store. Fix options: give the synthetic corpus a deterministic but *semantically real* embedder (TF-IDF/bag-of-words over the fixture vocabulary — no model download, dense leg becomes meaningful), or drop the dense leg from the synthetic gate and relabel it a lexical-harness check. Do NOT simply add a "dense must help" assertion — it would fail today, which is the finding, not the fix |
| 48 | **The significance bar added by `e2ac89f` gates nothing** — `mcnemar_exact` is never called anywhere under `scripts/` (verified by grep), so the Wilson interval and p-value are print-only decoration on both gate scripts. Compounding it, `.git/hooks/pre-commit` runs `precommit-scan.py` and `eval-gate.py` only — **it does not run pytest** — so `tests/test_synthetic_gate.py`'s 10 mutation-verified tests, which are where the significance machinery is actually exercised, never execute in the hook they were written to protect. The bar exists, is correct (statistics independently reviewed and found sound), and is load-bearing nowhere |
| 49 | **36 of 40 synthetic golden cases are grep-solvable** — measured 2026-08-13: 36 contain a term with document frequency exactly 1, i.e. a unique string that any lexical matcher finds; only 4 require real ranking. A plain grep scores ~0.90, and `RECALL_FLOOR` is 0.90, so the recall gate is set precisely at grep-grade and the 0.950 headline is ~4 discriminating cases wide. This is the same defect as #22/#45 (easy negatives) on the positive side: the set measures presence, not ranking. Needs cases where the answer term appears in several documents and only context disambiguates — `renewals-main` vs `renewals-annex` is the one pair already built for this and is the worked example |
| 50 | **[FIXED 2026-08-14]** **The write lock excludes nothing from `index` — and the drain now makes the race a scheduled one** — `promote_pending` (`src/alexandria/promote.py:83`) is the ONLY caller of `write_lock()`. `cmd_index` never acquires it, and on `--rebuild` calls `lexical.drop()`. A lock only excludes writers that also take it, so the weekly loop's index build and a promote can interleave freely. Before 2026-08-13 this needed a human to run `promote` mid-index; the serve drain now fires every 600s unconditionally, so each multi-minute weekly index has a real chance of overlapping a tick. Feared outcome: an entry whose marker is unlinked (→ considered promoted, never retried) but whose FTS rows were destroyed by a rebuild already in flight → permanently promoted-but-unsearchable, in a corpus with **no deletion path**. NOT yet traced to a confirmed interleaving — the drop happens before the pipeline, so the benign orderings may dominate; that trace is the task. **Deadline is real: weekly loop is Sunday 09:30, drain went live Thursday 23:53.** Fix is probably `cmd_index` taking the same flock, with the drain's existing `skipped_locked` path (W5) absorbing the loss. **Race CONFIRMED real, traced before any code was written:** `_load_chunk_records` snapshots at `cli.py:485`; a concurrent promote writes and indexes an entry after that; `cli.py:542-543` then calls `store.drop()` AND `lexical.drop()` unconditionally; the refill at `cli.py:555` runs with `append_only=args.rebuild` (`cli.py:560`), i.e. pure INSERT from the stale t0 snapshot, so the concurrently-promoted entry is never restored — and its pending marker was already unlinked, so nothing retries it. Note the loss is BOTH stores, not just FTS as originally feared. **Fixed:** `WriteLock.acquire()` gained `blocking=`/`timeout=` (default stays `LOCK_NB`, so the drain is untouched) plus `holder_pid()` and a hard `ValueError` on `blocking=True` without a positive timeout; `cmd_index` now acquires with `blocking=True, timeout=30s` and exits non-zero naming the holder pid rather than skipping silently. 7 tests in `tests/test_index_write_lock.py`, incl. one that deterministically reproduces the real race by pausing `BM25Index.drop` on a `threading.Event` and running a real `promote_pending` in the gap |
| 51 | **[FIXED 2026-08-14]** **`_warm_embedder` is half a warm-up — the reranker is still cold** — measured live 2026-08-13 immediately after the drain restart: first novel query **16.11s**, second 2.14s, third 0.80s. The embedder warm-up works and does correctly bypass `CachedEmbedder`, but `CrossEncoderReranker` (in the search path via `retrieval/search.py:20,65`) loads its ~90MB model lazily on the first search, so the first user after every restart still pays a cold load. This is the exact failure shape `_warm_embedder` was written to fix, one layer down: startup reports ready while the query path is not warm. Related to #44 (same model, offline-eval context) but distinct — this is serve's startup path. Fix: warm the reranker in `build_serve_context` too, and assert the invariant as "first query after start is not materially slower than the second" rather than pinning either component, so a third lazy component cannot reintroduce it. **Fixed:** `_warm_reranker` in `src/alexandria/serve.py`, best-effort (unlike the embedder — `search.py` is failure-tolerant on reranking, so a reranker that cannot load degrades ranking rather than answering nothing, and must not kill startup). Test `test_startup_leaves_nothing_in_the_query_path_lazily_loaded` asserts the invariant. Note this forced a real change to S3's `test_s3_the_model_loads_exactly_once_across_many_requests`: its counter was armed AFTER `_bind`, which observed zero constructor calls once the load moved to startup. Counter moved before bind; assertion still `== 1` and now spans startup plus five requests |

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
| 21 | Golden set structurally blind | **Substantially closed** by `3357fdd`. 22 hand-verified negative cases now exist and the gate fires on precision, not just recall: `regressions()` reports named unanswerable queries the engine grew more confident about. Measured separation — positive top-1 median 0.9819 vs negative 0.0238 — **partly** resolved spec Q5. **Corrected 2026-08-13:** the first published floor (0.12 retaining 100%) was computed from the wrong document — `separation()` read `scores[0]` rather than the score at the hit's own rank (fixed, `a5507a1`). Recomputed, the positive *minimum* collapses 0.1190 → 0.0274 while the median barely moves, and 0.12 retains 90.3%, not 100%. Five positives sit below any useful floor, all in the zero-overlap band, so a score floor is the wrong instrument rather than a mistuned one. See spec §9 Q5 — status there is PARTLY RESOLVED and gate R3 is PROVISIONAL. **What remains:** n=49 is still underpowered and there is still no significance bar, so a small recall move is not distinguishable from noise; and ≥10 in-domain negatives are needed before any floor is validated (21 of 22 current negatives are out-of-domain). #29 (offline policy tuning) is unblocked on the precision half only. **2026-08-13:** the significance bar now exists (`e2ac89f`: Wilson interval on recall, exact McNemar p-value in `compare()`), and #20's synthetic gate proves end to end that it fires — amputating the lexical leg loses 27 of 40 queries and the bar flags it, with a test that fails if `mcnemar_exact` is stubbed to 1.0. The in-domain-negative lesson is applied there too: the synthetic negative set is 10 of 12 in-domain by construction, with a test that fails if that ratio drops below 0.75. Neither fixes the *real* set, which is still n=49 with 21/22 out-of-domain negatives. |
| 20 | Golden set not in repo | **Half closed (2026-08-13).** A second, reproducible gate now ships in-repo: `tests/fixtures/synthetic-corpus/` (16 authored documents about a fictional public library), `synthetic-golden-v1.jsonl` (40 cases), `synthetic-negative-v1.jsonl` (12 cases), driven by `src/alexandria/eval/synthetic.py`, `scripts/synthetic-eval-gate.py`, and `tests/test_synthetic_gate.py` (10 tests). `eval-gate.py` now runs it **unconditionally** on retrieval-relevant commits, before the skippable private half. Proven to run with `env -i`, an empty `HOME` (so `~/alexandria-corpus` cannot exist) and `HF_HUB_OFFLINE=1`: 40/40 cases scored, recall@k 0.950 [0.835, 0.986], MRR 0.514, deterministic across 8 runs. All 10 tests mutation-verified — each was made to fail by breaking the thing it guards, then restored. **What this does NOT close:** the synthetic gate measures the HARNESS (scoring, recall, Wilson interval, McNemar bar, manifest/embed plumbing), never retrieval quality on real knowledge — its embedder is `HashEmbedder` (semantically empty, so the dense leg is noise and recall is carried by BM25) and its reranker is `IdentityReranker`. The real golden/negative sets remain private and unreproducible by a third party, which is unavoidable: their queries name private projects and infrastructure, and this repo is public and leak-scanned. **Newly discovered while doing this:** `alexandria eval` itself is not clean-clone runnable — `CrossEncoderReranker.rerank()` requires a ~90MB HuggingFace download, so even with an in-repo golden set the CLI gate cannot run on a network-free box. `scripts/synthetic-eval-gate.py` sidesteps this by constructing the engine directly; making the CLI degrade gracefully offline is untouched and still open. |
