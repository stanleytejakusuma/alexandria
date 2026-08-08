# Alexandria — full benchmark report (2026-08-08)

Canonical numbers for every eval that has ever run on Alexandria, on the
current corpus (28,640 docs, 38,960 chunks, 8 wiki pages). Private corpus:
`~/alexandria-corpus/.alexandria/` (golden/, eval_runs.jsonl, contest/).

## 1. Retrieval eval (the RAG benchmark — golden set)

`alexandria eval` against `golden-v1.jsonl` (49 hand-verified queries in
three overlap bands), k=5, hybrid BM25+dense+reranker
(bge-reranker-v2-m3). **Final run 2026-08-08 on the current corpus** —
appended to `eval_runs.jsonl` (run 12):

| Band | Recall@5 |
|---|---|
| literal | **87.5%** (7/8) |
| partial | **82.6%** (19/23) |
| zero-overlap | **38.9%** (7/18) |
| **Overall** | **67.3%** (33/49), MRR **0.577** |

Trend: 12 recorded runs (shas f8b4756b→3a51e5c0) at 33,320 chunks; the two
full-set runs both landed at 67.3%. Corpus growth to 38,960 chunks (v4 wiki
+ 5,490 distilled notes) did **not** move overall recall — zero-overlap
remains the structural weakness (the answer lives in one document retrieval
can't locate through paraphrased surface text). Known, documented in
`docs/DECISIONS-graph-vs-vector.md`.

## 2. Synthesis fact-recall (phase-2 gate)

Dual-grader consensus (sonnet-5 + gpt-5.6-terra) on 40 golden facts across
8 clusters. Gate was ≥90% — **never met**:

| Run | Pooled consensus | Notes |
|---|---|---|
| v1 | 45% (18/40) | FINAL_FAIL |
| v2 | 80% (32/40) | FINAL_FAIL |
| v3 | 85% (34/40) | waiver: stable stratum **97.1%** (34/35), Red-ratified |
| v4 | 70% (28/40) | FINAL_FAIL; convergence stop-rule → no v5 |

Round-4 fixes closed the temporal-layering class (breaker cluster 0.80 →
1.00) but two clusters couldn't emit (single-claim entailment each). The
compound/reversal class is a documented product-scope limitation
(`docs/pi-loop-termination.md` Backlog).

## 3. Contest vs incumbent (phase-3, recall@5, 40 frozen queries)

| Run | Alexandria | Incumbent | Verdict |
|---|---|---|---|
| cycle1 run1 | 0.509 | 0.491 | INVALID (disagreement 0.50 > 0.20) |
| cycle1 run2 | 0.532 | 0.468 | INVALID (disagreement 0.25 > 0.20) |
| cycle2 run1 | 0.521 | 0.479 | **FAIL** (valid; 12/40 adjudicated) |

Floor ≥0.60: **never met** (0.51 / 0.53 / 0.52). All CIs overlap — a
statistical dead heat against the incumbent. Reversal stratum improved
0.38 → 0.54 after the v4 wiki landed; operational 0.00 → 0.25 (n=4).
Per the signed loop-termination contract + usage pivot: no more contest
runs this cycle; the contest is quarterly telemetry
(`docs/pi-contest-cycle2-amendment.md`, `pi-self-learning-loop.md`).

## 4. Status

- **Read side (search/answer): LIVE, read-only** — Pi extension installed
  (verified end-to-end 2026-08-08 with a live cited answer).
- **Write side (ingestion): LIVE** — 5,490+ notes distilled from
  pi-sessions; weekly loop scheduled (LaunchAgent Sun 09:30).
- **Improvement loop: usage-driven** — query log (1,600 queries) + weekly
  review + on-demand wiki re-synthesis. Human-in-the-loop; no retraining.
- **Certification:** retrieval uncertified vs incumbent (dead heat), floor
  unmet; synthesis waiver-certified (stable stratum); extension remains
  read-only pending a future certified PASS.
