# Decision: do not index synthetic enrichment chunks

**Date:** 2026-08-11
**Status:** decided, acted on (weekly loop no longer passes `--enrich`)
**Scope:** the 85,788 synthetic hypothetical-question chunks produced by
`alexandria index --enrich`. Not the enrichment *store*, not the code.

## What was measured

This is the first valid measurement of enrichment. Every prior run measured a
frozen corpus (Aug 8 – Aug 11 the index was stale) or a half-built index, so
the feature had shipped and run for days with no evidence either way.

Predictions were written to `/tmp/alx/eval-prediction.md` **before** the eval
ran, because §G names post-hoc criteria as the phase-2 certification failure.

| arm | corpus | chunks | recall@k | MRR | zero band | literal band |
|---|---|---|---|---|---|---|
| A baseline | Aug-8 frozen | 40,460 | 0.673 | 0.563 | — | — |
| B control | current | 38,963 | **0.673** | **0.577** | **38.9%** | 87.5% |
| C treatment | current | 124,751 | 0.612 | 0.550 | 22.2% | 100% |

A→C alone is confounded: enrichment was added *and* the corpus gained 5,507
documents. Arm B rebuilds the identical corpus with no enrichment and separates
them. Corpus growth is neutral-to-positive (recall flat, MRR +0.014).
Enrichment alone costs **−6.1 pts recall@k and −0.027 MRR**.

## Pre-registered rule, and the verdict

- P1 recall > 0.673 — **failed** (0.612)
- P2 ≥4 of the 13 zero/partial misses convert — **failed** (1 conversion)
- P3 the 3 non-band misses do *not* convert — **failed**; the single conversion
  was `isolation-pinning`, one of the three predicted not to move
- P4 MRR > 0.5626 — **failed** (0.550)

Rule as written: *P1 fails and MRR falls → DROP; any recall drop > 2 pts → DROP
and investigate.* Verdict: **DROP.** The criterion was not revisited after the
number was known.

## Mechanism

The damage is not uniform, and its shape is the finding:

- **zero-overlap band: 38.9% → 22.2%** (43% relative degradation)
- **literal band: 87.5% → 100%** (enrichment marginally *helps*)

Synthetic chunks are short, question-shaped, and embedded in query space. A
user query is also short and question-shaped in query space, so it carries
moderate similarity to a great many synthetic questions belonging to unrelated
documents. Where BM25 anchors the ranking, that noise is filtered and the extra
signal helps. Where dense similarity decides alone — exactly the zero-overlap
band enrichment exists to serve — 85,788 near-neighbours crowd out real chunks,
and the collapse-onto-target step promotes whatever document they point at.

The idea is not refuted. The *unconditional* indexing of every hypothetical is.

## What changed

- `scripts/run-weekly-loop.sh` no longer passes `--enrich`. Unattended
  enrichment would have reintroduced this weekly, silently.
- `--enrich` remains a CLI flag. The enrichment store, its schema, and its
  tests are untouched; reattach replays it with no LLM calls, so re-testing a
  fixed variant costs ~3 minutes, not a re-enrichment run.

## Re-open triggers

Any one of these justifies retesting, and each is a *variant*, not a retry:

1. A similarity floor on synthetic hits (only collapse above threshold τ).
2. A per-document cap (index 1 hypothetical, not 3).
3. Synthetic vectors excluded from dense recall but retained for reranking —
   isolates the summary-augmentation benefit from the crowding cost.
4. Mean pairwise cosine among synthetic vs real vectors, to confirm or kill the
   homogeneity mechanism directly rather than by its signature.

Any retest must carry a no-enrichment control on the same corpus. A/C
comparison alone would have shipped the wrong conclusion today.
