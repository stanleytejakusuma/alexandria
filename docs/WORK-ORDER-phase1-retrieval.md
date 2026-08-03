# WORK ORDER — Phase 1 retrieval layer

**Repo:** `~/codebase/alexandria` · **Branch:** work on `phase1-retrieval`, not `main`
**Venv:** `.venv` (use `.venv/bin/python`, never system python) · Python 3.12, `uv` for deps
**Status quo:** 146 tests passing, phase 0 complete. Do not break either.

---

## 0. What this project is (read before designing anything)

Alexandria is an open-source personal knowledge engine. Two layers:

- `sources/` — immutable, append-only, machine-written notes (23,113 markdown files today)
- `wiki/` — LLM-synthesized pages where **every claim carries a keyed citation** back to a
  source note (built in phase 2 — not your concern, but retrieval must anticipate it)

You are building **phase 1: retrieval only.** No synthesis, no answer generation, no UI.

The corpus lives at `~/alexandria-corpus` (private, gitignored from this repo, **never commit
corpus content into this repo** — see §7).

---

## 1. Deliverables

Build these, in this order, each with tests before the next begins.

### 1.1 `src/alexandria/index/embedder.py`

Pluggable embedding provider with a **local default**.

- `Embedder` protocol: `.embed(texts: list[str]) -> list[list[float]]`, plus `.dim` and `.name`.
- `LocalEmbedder` — sentence-transformers, default model `Qwen/Qwen3-Embedding-0.6B`,
  device auto-detect (`mps` on this Mac, else `cuda`, else `cpu`). Batch size configurable.
- `HashEmbedder` — deterministic, dependency-free, seeded from a content hash. **Required**:
  every test in this work order must run offline with no model download. This is the
  "ScriptedClient" of embeddings.
- **Content-hash cache** (`.alexandria/cache/embeddings.sqlite` in the corpus):
  key = `sha256(model_name + "\n" + text)`, value = vector. A re-index of unchanged content
  must be a cache hit and do zero model work. This is load-bearing: the first full build is
  ~33k chunks and slow; the second must be fast.
- **Resumable + observable**: embedding 33k chunks takes 15–40 min on MPS. Report progress
  (count, rate, ETA) at a configurable interval. If interrupted, re-running must resume from
  the cache rather than restart.

### 1.2 `src/alexandria/index/store.py`

LanceDB-backed vector + metadata store.

- Table schema per chunk: `chunk_id` (pk), `doc_id`, `text`, `heading_path`, `vector`,
  plus filterable metadata copied from the note's frontmatter:
  `type`, `project`, `status`, `source`, `tags` (list), `entities` (list), `layer`
  (`"sources"` or `"wiki"`, derived from the doc_id prefix), `generated_at`.
- `upsert(chunks)`, `search_vector(query_vec, k, where=None)`, `get(chunk_id)`, `count()`,
  `drop()`.
- **Metadata filter is the FIRST gate** (spec §8): filters apply before/within the vector
  search, never as a post-filter that silently returns fewer than `k`.
- Index dir: `~/alexandria-corpus/.alexandria/index/` (already gitignored).

### 1.3 `src/alexandria/index/bm25.py`

Lexical half of hybrid retrieval.

- SQLite **FTS5** (stdlib `sqlite3`, no new dependency) at `.alexandria/index/fts.sqlite`.
- `index(chunks)`, `search(query, k, where=None) -> [(chunk_id, score)]`.
- **Query handling matters.** Do NOT do `" OR ".join(words)` — that makes every token
  optional so a 12-word question matches almost anything. Use FTS5 proper syntax, escape
  user input (a stray `"` or `*` must not raise or silently change semantics), and handle
  the empty-after-stopword-removal case.

### 1.4 `src/alexandria/retrieval/fusion.py`

Reciprocal Rank Fusion + the layer boost.

- `rrf(rankings: list[list[str]], k=60) -> dict[chunk_id, score]`, standard RRF.
- **Boost, don't route** (spec §8.3, binding): `wiki/` results receive a score boost at the
  **fusion stage, before reranking** — never a hard router that only searches one layer.
  Boost factor configurable, default from config. Rationale: a wrong hard route silently
  loses recall with no A/B traffic to catch it.
- Boost must be a pure function of layer + config, fully testable without a model.

### 1.5 `src/alexandria/retrieval/rerank.py`

- `CrossEncoderReranker` — `BAAI/bge-reranker-v2-m3`, rerank top-N (default 20) → top-k
  (default 5).
- `IdentityReranker` — passthrough, for offline tests. Same protocol.
- Reranker failure must **degrade to fusion order**, never take down the query.

### 1.6 `src/alexandria/retrieval/search.py`

The pipeline, wired end to end:

```
query → metadata filter (first gate)
      → BM25 top-N  ‖  dense top-N        (parallel)
      → RRF fusion + layer boost           (§1.4)
      → cross-encoder rerank 20 → 5
      → SearchResult[]
```

`SearchResult`: `chunk_id, doc_id, text, heading_path, layer, score, rank`, plus a
`trace` dict recording **what each stage did** — candidates in/out per stage, per-stage
scores, timings, whether the boost changed an ordering, cache hit/miss. This trace is a
phase-1 deliverable, not a nicety: it is the retrieval-trace view's data source, and the
thing that makes a golden-set regression diagnosable.

### 1.7 `src/alexandria/monitor.py`

Query logging to `.alexandria/queries.sqlite`:
`{query_id, ts, q, filters, tier, retrieved_ids, scores, latency_ms, cache_hit, client}`.
Append-only. Must not fail a query if logging fails.

### 1.8 CLI verbs (extend `src/alexandria/cli.py`, argparse — do not add a CLI framework)

- `alexandria index [--rebuild] [--limit N] [--workers N]` — chunk + embed + store the corpus,
  resumable, progress reporting.
- `alexandria search "<query>" [--k 5] [--type observation] [--project X] [--layer wiki]
  [--trace]` — `--trace` prints the per-stage trace.

---

## 2. Configuration

Read `~/.config/alexandria/config.toml` if present, else these defaults. Env-overridable.

```toml
[corpus]  path = "~/alexandria-corpus"
[embed]   provider = "local"                    # local | hash
          model = "Qwen/Qwen3-Embedding-0.6B"
          batch_size = 32
[rerank]  model = "BAAI/bge-reranker-v2-m3"
          prefetch = 20
          top_k = 5
[index]   store = "lancedb"
          chunk_tokens = 512
          chunk_overlap = 0.15
[search]  wiki_boost = 1.25                     # fusion-stage multiplier, see 1.4
          rrf_k = 60
```

---

## 3. Non-negotiable constraints

1. **TDD.** Tests before implementation, every module. The suite must be green at every commit.
2. **Every test runs OFFLINE with no model download and no network.** Use `HashEmbedder` and
   `IdentityReranker`. A test that needs `Qwen3` is a broken test.
3. **Do not modify** `schema.py`, `corpus.py`, `migrate.py`, `connectors/`, `audit.py`,
   `grounding.py`, `decay.py`. If you believe one needs changing, STOP and report why.
4. **`chunker.py` is done** — use it, do not rewrite it. `chunk_document(doc_id, markdown)`.
5. **No new heavyweight dependency** without justification in your report. `lancedb`,
   `sentence-transformers` are expected and fine. Anything else, argue for it.
6. **Determinism**: chunk ids, cache keys and fusion output must be stable across runs.
   Sort before iterating anything derived from `rglob`/`glob` — filesystem order is not stable.
7. **Failure posture**: degrade, never crash a query. Reranker down → fusion order. Cache
   corrupt → recompute. Logging broken → still answer. Never silently return fewer results
   than requested without saying so in the trace.

---

## 4. Performance targets (spec §15 phase-1 gate)

- Full index rebuild of 33k chunks: **< 30 min** on this machine.
- Warm re-index (no content change): **< 2 min** (cache hits only).
- p50 query latency, local, warm: **< 500 ms**.

Report measured numbers. If a target is missed, say so plainly — do not tune the measurement.

---

## 5. Verification you must run before reporting done

```bash
.venv/bin/python -m pytest tests/ -q          # all green, no skips hiding failures
.venv/bin/python scripts/precommit-scan.py --all
.venv/bin/alexandria index --limit 500        # real corpus smoke, measure it
.venv/bin/alexandria search "how does the sweep handle a page that fails lint" --trace
```

Then a **real full index** if time allows, and report the wall-clock numbers.

---

## 6. Known traps (learned the hard way in this project — do not rediscover them)

- The local gateway **streams by default**; unrelated here but the same class of bug: never
  assume a library's default matches your assumption. Check.
- The corpus has **duplicate and near-duplicate notes** (same content, different ids). Do not
  dedup them away in the index — that is a phase-2 decision. But do not let one document's
  20 near-identical chunks crowd out every other document in top-k either. If you have a
  cheap idea here, propose it; do not implement it unasked.
- Median chunk is 356 tokens, p95 508, max 603. A few chunks are dense non-prose (hashes,
  base64). The embedder must not choke on them.
- `sources/_unparsed/` contains 23 quarantined files with no frontmatter. **Skip them.**
- Some notes have no `entities` or `tags`. Metadata filtering must handle absent fields.

---

## 7. Privacy (hard rule)

**No corpus content in this repo. Ever.** Tests, fixtures, docstrings, commit messages:
synthetic data only. The leak scanner (`scripts/precommit-scan.py`) runs as a pre-commit hook
and will block you; if it fires, fix the content, do not weaken the scanner. Absolute paths
containing a username, private hostnames, IPs (except loopback and RFC 5737 documentation
ranges) and key-shaped strings are all blocked.

---

## 8. Out of scope — do not build

Synthesis/sweep · `/answer` · FastAPI server & HTTP endpoints · the web UI · any Pi/harness
extension · authentication · deployment. Phase 1 is the retrieval **library** plus CLI only.

---

## 9. Report back

- What you built, module by module, with test counts.
- Measured performance against §4, honestly.
- Any spec deviation, and why.
- Anything in §6 that bit you anyway.
- What you would do differently with more time.
