# Architecture

A map of the codebase for a new contributor: what each module owns, how a
document becomes a retrievable, cited fact, and the invariants that keep
that honest. Companion to `QUICKSTART.md` (how to run it) — this is how it
works.

## Data flow, end to end

```
harness-native store (session memory, distillation cache, session transcripts, ...)
        |  connectors/*.py  (pull-based, idempotent, no rewriting)
        v
sources/*.md + wiki/*.md   (the corpus: frontmatter + body, schema.py validates)
        |  index/chunker.py  (structure-aware: headings, not fixed windows)
        v
chunk records (chunk_id, doc_id, text, heading_path, layer, ...)
        |  enrich.py (optional)      index/embedder.py
        |  summary/keywords/            |
        |  hypothetical Qs               v
        |  -> synthetic vectors    index/store.py (vectors) + index/bm25.py (lexical)
        v                                  |
        +---------------- retrieval/search.py (hybrid: RRF fusion + rerank)
                                  |
                    cache.py (query cache, generation-bound)
                                  |
                                  v
                         SearchResult[] -> CLI / Pi extension
                                  |
                    synthesis/pipeline.py (gather -> write -> judge -> repair)
                                  |
                                  v
                        wiki/<topic>.md  (cited synthesis page)
                                  |
                          cache.py (response cache) + auditlog.py (route trace)
```

Every stage is independently testable and independently degradable: a
missing reranker falls back to fusion order, a missing vector index falls
back to BM25-only, a failed enrichment call never blocks indexing.

## Module map

| Module | Owns |
|---|---|
| `connectors/` | Pulling raw material from harness-native stores. `base.py` defines the `Connector` protocol (`discover()` + `normalize()`) and `NoStateMixin` for content-hash-idempotent connectors (no LLM). `pi_sessions.py` distils session transcripts (the one LLM-backed connector); `md_memory.py`, `inbox.py`, `journal.py` are deliberate-write, no-LLM. |
| `corpus.py` | The `Doc` type (frontmatter + body), path/slug conventions, read/write. |
| `schema.py` | Frontmatter validation: required fields per profile (source vs wiki), the actor-convention check (`connector/<n>`, `sweep/<m>`, `agent-deliberate/<h>`, `human:<n>` — never a bare free-text string), tag/type enums. `lint` runs this over every doc. |
| `index/` | `chunker.py` (heading-aware splitting), `embedder.py` (`CachedEmbedder` wraps a provider — MLX/local/hash — keyed by **model + revision + mode + text**, so query-space and document-space vectors of the same string never collide), `bm25.py` (lexical), `store.py` (vector store: LanceDB with a SQLite fallback; `ALL_FIELDS` includes the enrichment columns, coerced to `""` never `None` — a LanceDB `merge_insert` bug crashes on NULL in a newly-added column). |
| `retrieval/` | `search.py` (`SearchEngine`: embed → BM25 + vector search in parallel → RRF fusion → layer boost → **synthetic-record collapse** (a hypothetical-question hit resolves to its real target chunk, never surfaced itself) → rerank → cache write), `fusion.py` (RRF + layer boost), `rerank.py` (cross-encoder with a graceful-degrade path). |
| `enrich.py` | Per-document LLM enrichment (summary, keywords, hypothetical questions) — **one call per document**, persisted to `EnrichmentStore` immediately (the store is the checkpoint), keyed by content fingerprint + recipe (model+prompt version) so either changing invalidates. Hypotheticals become query-space vectors (`synthetic_records`) for question-to-question matching. The pool streams results via `as_completed` — never a barrier that would hide progress or lose completed work on a crash. |
| `synthesis/` | `gather.py` (two-round retrieval into a candidate pool), `write.py` (LLM drafts a cited page), `judge.py`/`audit.py` (entailment + coverage graders — a claim must be *supported*, not just plausible), `repair.py` (bounded repair loop with an anti-gutting guard), `pipeline.py` (composes the four; records per-stage timings). |
| `cache.py` | Query cache and response cache — both **generation-bound** (a reindex invalidates every cached entry via a monotonic counter, not a fragile TTL guess), canonically-serialized keys (not `repr()`), and cacheability gated on "no degraded stage" (a partial BM25/vector failure is never cached as if it were healthy). |
| `auditlog.py` | JSONL trail for every search/answer/sync: caller/user scope, per-stage timings, cache hit source (query-cache vs embedding-cache, never conflated), and for answers a **route trace** (which chunks were retrieved, which were cited, how many synthesis iterations). |
| `wiki_site.py` | Static site renderer for the wiki + the audit trail (dark/light, card index, per-answer route-map visualization) — stdlib only, zero JavaScript. |
| `decay.py` | Proposes eviction from a capped memory store once its contents are durably indexed elsewhere. |
| `cli.py` | The single entry point (`alexandria <verb>`) wiring all of the above; no framework, argparse only. |

## Invariants worth knowing before you change anything

1. **Provenance never lies.** Every doc's `generated.by` must match the
   actor convention; distillation never edits a source; wiki pages are
   never re-ingested as sources (no feedback-loop pollution).
2. **Failure is data, not silence.** A grader failure raises rather than
   defaulting to a pass; an enrichment failure is never stored (so it stays
   retryable); a degraded retrieval stage is flagged in the trace, not
   hidden.
3. **Every long-running mutation is resumable.** Connectors are
   idempotent by content hash; enrichment persists per-document; sync
   commits per-burst. A crash loses at most the one thing in flight.
4. **The corpus generation is the cache's source of truth.** `index`
   bumps a monotonic counter on success; caches key on it. No cache
   entry can outlive the index it was computed against.
5. **Read-only until certified.** The Pi extension and any external
   surface stay read-only (search/answer) plus one explicit write
   (`remember`, user-confirmed only) until a contest cycle produces a
   real PASS — see `docs/pi-loop-termination.md` and
   `docs/pi-activation-decision-2026-08-08.md` for why and how that's
   enforced, not just documented.

## Where the numbers come from

`docs/benchmark-report-2026-08-08.md` is the measured baseline (retrieval,
synthesis, contest). `docs/pi-self-learning-loop.md` is the usage-driven
improvement loop that replaces further golden-set measurement cycles.
