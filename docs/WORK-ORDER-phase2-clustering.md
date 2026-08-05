# WORK ORDER — Phase 2 clustering (dedup + topic)

**Repo:** `~/codebase/alexandria` · **Branch:** `phase2-clustering`, not `main`
**Venv:** `.venv` (`.venv/bin/python`, never system python) · Python 3.12
**Baseline:** `main` at 328 passing tests. Do not regress it.
**Independent of `phase2-synthesis-core`** — this order does not depend on
that one's `pipeline.py` and can be built and merged in either order. The
full-sweep orchestrator (a later, not-yet-written order) depends on *this
one's* output, not the reverse.

---

## 0. Why this exists

`docs/DECISIONS-phase2-execution-model.md` settled that full-corpus
"exhaustive enumeration" is cluster-based, via **two distinct clustering
passes sharing one embedding pipeline** — not one clusterer trying to do
both jobs, and not two separate embedding infrastructures. Read that
document in full before writing any code; this work order implements its
§"Two distinct clustering passes" section exactly.

- **Dedup clustering**: groups chunks that restate the *same fact* — tight
  similarity threshold. This is the previously-adopted-but-unbuilt
  "index-time near-duplicate dedup clustering" item, and the mechanism
  behind Alexandria's own measured quality defect (top-5 retrieval results
  often contain only ~3 distinct facts due to near-duplicate restatement).
- **Topic clustering**: groups chunks on the *same topic* asserting
  *different, causally or temporally linked* facts — the shape of the 8
  hand-built `golden-synthesis-v1.jsonl` clusters (bug → root cause → fix →
  regression chains). Loose threshold. This is what the full-sweep
  orchestrator will consume as its enumeration units.

**You have real, already-verified ground truth for both — use it, do not
invent synthetic calibration data.**

---

## 1. Real calibration data already on disk — use it, do not rebuild it

### For dedup clustering

Every ANY-OF entry with more than one candidate, across the three existing
golden sets, is a hand-verified duplicate/equivalent-document relationship
— someone read both documents and confirmed they assert the same fact.
Load these via the existing loaders (`alexandria.eval.golden.load_golden`,
`alexandria.eval.contradiction_golden.load_contradiction_golden`) and treat
every multi-candidate `must_retrieve` tuple, and every multi-candidate
`claim_a`/`claim_b` tuple, as a positive pair for calibrating your
similarity threshold. Real counts, verified before this work order was
written: 7 multi-target entries in `golden-v1.jsonl`, 8 in
`contradiction-pairs-v1.jsonl`. This gives you real positive pairs; you
will need to construct real *negative* pairs yourself (two chunks that are
topically similar but genuinely distinct facts — the retrieval golden set's
zero-overlap-band entries and the coverage-calibration set's `SS:tangential`
cases are a reasonable source to check first before constructing new ones).

### For topic clustering

`golden-synthesis-v1.jsonl`'s 8 clusters are hand-verified groupings of
2-6 real documents each into one coherent topic. Run your topic-clustering
algorithm over the corpus and check: **does it recover groupings that
overlap substantially with these 8 known-good clusters?** This is a direct,
concrete validation you did not have to construct — use
`alexandria.eval.synthesis_golden.load_synthesis_golden` to load them.

---

## 2. Deliverables

### 2.1 `src/alexandria/synthesis/clustering.py`

- Reuse the existing embedding pipeline (`MLXEmbedder`/`CachedEmbedder` in
  `src/alexandria/index/embedder.py`) — **do not build a second embedding
  path.** Both clustering passes embed with the same model; they differ
  only in threshold/algorithm parameters.
- `find_duplicate_clusters(chunks, *, threshold: float) -> list[DuplicateCluster]`
  — tight threshold, calibrated against §1's real positive/negative pairs.
  Report precision/recall against that calibration set in your final
  report (§6), not just "it runs."
- `find_topic_clusters(chunks, *, threshold: float) -> list[TopicCluster]`
  — loose threshold, calibrated against the 8 known-good
  `golden-synthesis-v1.jsonl` groupings. Report cluster-overlap (however you
  choose to define it — Jaccard on cluster membership is a reasonable
  starting point, document your choice) against those 8, not just "it
  runs."
- Both functions take a fixed corpus snapshot as input and return a value —
  no hidden state, no writes, no side effects. This code will be called
  from inside a serial map-reduce fold (the future full-sweep order); it
  must be safe to call repeatedly and must not assume anything about
  execution order or prior calls.

### 2.2 Dedup action space, wired to `find_duplicate_clusters`

Per `DECISIONS-multi-actor-posture.md`'s adopted action space:
`store | update | merge | skip`. For each duplicate cluster found, decide
which action applies — you'll need real judgment calls here (is one
member strictly more complete/recent, in which case `update`/`merge`
toward it, or are they genuinely equivalent, in which case `skip` all but
one). **Fail loud on degraded dependencies**: if the embedder or corpus
index is unavailable, the dedup pass must abort visibly, never silently
skip and report false success — this was an explicit, deliberate rejection
of the pattern found in a comparable system's dedup implementation
(silent-skip-when-vectors-unavailable), named in
`DECISIONS-multi-actor-posture.md`.

---

## 3. Constraints

1. **TDD.** Tests before implementation, suite green at every commit.
2. **Every test offline**: `HashEmbedder` for all clustering-logic tests —
   real MLX embedding calls belong only in the calibration-report run
   against the real corpus (§6), never in the test suite.
3. **Do not modify** anything under `index/`, `retrieval/`, `eval/`, or
   `llm.py`. If you believe you need to, STOP and report why before
   touching it.
4. **Determinism**: given the same chunk set and the same threshold, both
   clustering functions must produce the same clusters every time. No
   randomized tie-breaking without a fixed seed.
5. No new dependencies without a documented reason in your report.

---

## 4. Known traps

- Do not conflate the two clustering jobs into one threshold. A threshold
  tuned to catch near-duplicates will be too strict to catch topic
  relatedness — a real prior check using 5-gram shingle similarity at
  ~0.30 still missed a genuine paraphrased duplicate, which tells you
  something about how narrow the margin between "duplicate" and
  "unrelated" can be; do not assume a single number generalizes across both
  jobs.
- The dedup calibration set (§1) is small (7 + 8 positive pairs). Report
  your precision/recall honestly with the small-n caveat this project
  applies everywhere else (a Wilson interval or equivalent, not a bare
  percentage implying more precision than the sample supports).
- `golden-synthesis-v1.jsonl`'s clusters were hand-built by searching for
  *causally connected* facts, not just topically adjacent ones — a topic
  clusterer that recovers documents about the same subject but misses the
  causal chain shape (sequential, not just co-occurring) may score well on
  naive overlap while missing the actual point of the ground truth. Read a
  couple of the real clusters' `load_bearing_facts` before tuning, not just
  their `source_docs` lists.

---

## 5. Out of scope

The full-sweep orchestrator itself (a separate, later work order — this
order produces clusters as a value, it does not consume them into pages).
Any UI or reporting surface for cluster review. Automatic execution of the
dedup action space's `merge`/`update` decisions against the live corpus —
this work order decides *what* action applies per cluster and returns that
decision; actually mutating corpus files is a separate concern this order
does not touch.

---

## 6. Verification and report back

```bash
.venv/bin/python -m pytest tests/ -q
.venv/bin/python scripts/precommit-scan.py --all
```

Then, against the real corpus: run both clustering passes for real, and
report honestly:
- Dedup clustering's precision/recall against the real 7+8 known-duplicate
  pairs from §1, with the small-n caveat stated plainly.
- Topic clustering's overlap against the 8 real `golden-synthesis-v1.jsonl`
  clusters, with your chosen overlap metric named and justified.
- How many clusters each pass found across the real corpus, and roughly
  how that compares to the corpus's real document count (~23k) — a sanity
  check that the thresholds aren't producing something absurd (one giant
  cluster, or thousands of singleton "clusters").
- Any spec deviation, and why.
