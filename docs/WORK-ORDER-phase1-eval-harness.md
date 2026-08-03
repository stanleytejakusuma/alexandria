# WORK ORDER — Phase 1 golden-set eval harness

**Repo:** `~/codebase/alexandria` · **Branch:** `eval-harness`, not `main`
**Venv:** `.venv` (`.venv/bin/python`, never system python) · Python 3.12
**Baseline:** `main` at 191 passing tests. Do not regress it.

---

## 0. Why this exists (read first — it determines the design)

Retrieval quality is currently unmeasurable, and that blocks three decisions:

- whether swapping the embedder (MLX, 3.18x faster) degrades results
- whether the cross-encoder reranker earns its ~900ms (84% of query latency)
- whether any latency/quality trade is acceptable

Right now we can only observe that results **change**, never whether they got
**worse**. This harness is the instrument that fixes that.

**The governing risk is a WRONG eval, not a missing one.** Two real precedents:

1. A studied project (NexusRAG) shipped published quality scores whose citation
   regex matched `[\d]` while production emitted `[a3x9]` — the metric was
   structurally incapable of failing and scored 1.0 forever.
2. **On this very corpus, last night:** a strict golden set scored 50% recall@5.
   Two of those "misses" were errors in the golden set, not retrieval — the
   health-db query returned exactly the two documents the spec names, at ranks 1
   and 2, while the golden entry targeted a third. Corrected set: 64.3%.

So: an eval that cannot fail correctly is worse than no eval. Design accordingly.

---

## 1. Where things live (hard constraint)

| Thing | Location | Why |
|---|---|---|
| Harness code | this repo, `src/alexandria/eval/` | public, generic |
| Golden set data | `~/alexandria-corpus/.alexandria/golden/golden-v1.jsonl` | **private** — queries name real private systems |
| Run history | `~/alexandria-corpus/.alexandria/eval_runs.jsonl` | private, append-only |

**Never** commit golden-set content, queries, or doc ids into this repo. Tests use
synthetic fixtures only. The leak scanner (`scripts/precommit-scan.py`) is a
pre-commit hook and will block you; if it fires, fix the content, don't weaken it.

---

## 2. Golden set format (already exists — 14 entries, do not redesign)

JSONL, one entry per line:

```json
{"id":"health-db-schema",
 "query":"health database schema gotchas canonical path",
 "must_retrieve":["sources/cipher/cipher-...2264672-health-db-schema-gotchas",
                  "sources/cipher/cipher-...1736167-health-db-schema-gotchas"],
 "k":5,
 "note":"ANY-OF: several documents legitimately answer this"}
```

- `must_retrieve` is **ANY-OF**, not all-of: the entry is a HIT if *any* listed
  doc_id appears in the top-k. This is load-bearing — it is the fix for the 50%
  false-negative described in §0.
- `k` is per-entry.
- `note` is human context, ignored by scoring.

---

## 3. Deliverables

### 3.1 `src/alexandria/eval/golden.py`

- `load_golden(path) -> list[GoldenEntry]` — parse + validate.
- **Validation must fail loudly**, not silently skip: unknown fields, missing
  `query`/`must_retrieve`/`k`, empty `must_retrieve`, duplicate `id`, malformed JSON
  → raise with the offending line number.
- `verify_targets(entries, corpus_path) -> list[str]` — returns entries whose
  target docs do not exist on disk. A golden set pointing at deleted documents
  scores 0% and looks like a retrieval collapse; this must be detected and reported
  **separately from a genuine miss**.

### 3.2 `src/alexandria/eval/metrics.py`

Pure functions, no I/O, exhaustively unit-tested:

- `recall_at_k(retrieved_ids, want_ids, k) -> bool` (ANY-OF)
- `reciprocal_rank(retrieved_ids, want_ids) -> float` (0.0 when absent)
- `mrr(per_query_rr) -> float`
- `summarize(results) -> EvalSummary` with `recall_at_k`, `mrr`, `n`, `hits`,
  `misses` (ids), `target_errors` (ids)

Edge cases that must be tested: empty retrieved list, k larger than result count,
k=0, duplicate ids in retrieved, want-id appearing multiple times.

### 3.3 `src/alexandria/eval/runner.py`

- `run_eval(engine, entries, *, k_override=None) -> EvalReport`
- Per entry record: `id`, `query`, `hit`, `rank` (0 = miss), `retrieved_ids`,
  `latency_ms`.
- A query that **raises** is recorded as an error, is NOT counted as a hit, and does
  not abort the run — but the summary must surface `errors > 0` prominently. A run
  that silently drops failing queries would inflate recall.
- Report includes: config fingerprint (embedder name, reranker name+precision,
  prefetch, top_k, rrf_k, wiki_boost), corpus chunk count, timestamp, git sha.
  **Without this a score is uninterpretable across runs.**

### 3.4 `src/alexandria/eval/history.py`

- Append each run to `eval_runs.jsonl` (configurable path). Append-only.
- `compare(previous, current) -> Delta` — recall/MRR deltas plus, importantly,
  **per-entry transitions**: which ids went HIT→MISS and MISS→HIT. Aggregate numbers
  can stay flat while results churn underneath; the transitions are what actually
  tell you a change was harmful.
- `regressions(delta) -> list[str]` — ids that went HIT→MISS.

### 3.5 CLI: `alexandria eval` (extend `cli.py`, argparse, no new framework)

```
alexandria eval [--golden PATH] [--k N] [--json] [--compare-last] [--fail-on-regression]
```

- Human-readable table by default: per-entry HIT/MISS + rank, then the summary.
- `--json` emits the machine-readable report.
- `--fail-on-regression` exits non-zero if any entry went HIT→MISS versus the last
  recorded run. This is the release gate.
- Exit codes: `0` pass, `1` regression/threshold failure, `2` bad usage or an
  unusable golden set (missing targets).

---

## 4. THE TEST THAT MATTERS MOST

**Prove the harness can fail.** A test that deliberately breaks retrieval — e.g. an
engine stub returning empty results, or returning ids that are never in
`must_retrieve` — and asserts the harness reports **0% recall, and a non-zero exit
code**. Also: a golden set whose targets do not exist must be reported as
`target_errors`, distinctly from misses.

If the harness cannot demonstrably go red, it is decoration. Both precedents in §0
failed exactly here.

---

## 5. Constraints

1. **TDD.** Tests before implementation, suite green at every commit.
2. **Every test offline**: no model downloads, no network, no real corpus. Use a
   fake engine returning canned ids, and synthetic golden entries in `tmp_path`.
   A test needing Qwen3 or LanceDB is a broken test.
3. **Do not modify**: `schema.py`, `corpus.py`, `migrate.py`, `connectors/`,
   `audit.py`, `grounding.py`, `decay.py`, `index/chunker.py`, `index/embedder.py`,
   `retrieval/search.py`, `retrieval/fusion.py`, `retrieval/rerank.py`. If you
   believe one must change, STOP and report why.
4. No new dependencies. stdlib `json`/`statistics` are sufficient.
5. Determinism: iterate golden entries in file order; never `set` ordering in output.

---

## 6. Known traps

- The corpus is at `~/alexandria-corpus`; `.alexandria/` inside it is gitignored by
  design — the harness must create parent dirs rather than assume they exist.
- Some golden entries have multiple `must_retrieve` ids (ANY-OF). Scoring all-of
  would reproduce the exact 50%-false-negative bug this harness exists to prevent.
- `doc_id` is the corpus-relative path **without** `.md`. Do not append it.
- Real measured baseline on this corpus: **recall@5 = 64.3%, MRR = 0.571**,
  9/14 hits. If your implementation reports a wildly different number against the
  real index, suspect the harness first, not retrieval.

---

## 7. Verification before reporting done

```bash
.venv/bin/python -m pytest tests/ -q          # all green, no skips masking failures
.venv/bin/python scripts/precommit-scan.py --all
.venv/bin/alexandria eval                     # against the real corpus + index
.venv/bin/alexandria eval --json | head -40
```

Report the real recall@5/MRR you measure and whether it matches the 64.3%/0.571
baseline. **A mismatch is a finding, not something to quietly reconcile.**

---

## 8. Out of scope

LLM-judge/faithfulness evaluation (that is `audit.py`, already built) · diversity
capping · changing retrieval behaviour of any kind · tuning · the phase-2 sweep.
This work order builds the measuring instrument only. **Do not "improve" retrieval
while building the thing that measures it** — that would leave us unable to
attribute any change.

---

## 9. Report back

Modules built + test counts · measured recall@5/MRR vs the 64.3%/0.571 baseline ·
proof the harness can go red (which test, what it asserts) · anything in §6 that bit
you anyway · any spec deviation and why.
