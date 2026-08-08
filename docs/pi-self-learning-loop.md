# pi-self-learning-loop.md

# Alexandria self-improving memory/RAG loop (2026-08-08)

**Decided by:** Stanley (principal), pivoting from measurement-driven to
**usage-driven** improvement: "I'm all for improving Alexandria itself in
terms of memory generation, memory retrieval, and the general roundup of
retrieval-augmented generation... we would have implemented some kind of
learning loop... the crux of the loop for these RAG tools."

## The loop (use -> log -> distill -> synthesize -> tune)

```
you use Alexandria (search/answer in Pi)        [read side, LIVE]
        |
        v
queries logged to queries.sqlite (retrieved ids,
scores, latency, client)                        [phase-1 built, LIVE]
        |
        v
weekly sync: your sessions distil into notes    [write side, LIVE]
        |
        v
cluster/topic re-synthesis on demand: a topic
with enough new notes regenerates its wiki page [phase-2 pipeline]
        |
        v
weekly query-log review: gaps (zero-hit queries),
navigation quality (cluster jumps per query)    [scripts/query-log-review.py, NEW]
        |
        v
adaptation: re-synthesize gap topics; tune hybrid
weights only when a pattern persists >= 2 weeks [human-in-the-loop v1]
```

## What this is — and what it is not (honest claims)

- **True:** the system improves from usage without any model retraining.
  Every component in the loop is already built (phase-1 query log, the
  pi-sessions distiller, the phase-2 synthesis pipeline, calibrated
  clustering) or is a small script over that data.
- **Not (yet) claimed:** "reinforcement learning" in the training sense.
  The reward signal today is implicit (re-queries after a miss, corrections
  in conversation, accepted answers). A future explicit "was this useful?"
  rating on search results turns the loop into a weak-signal RL bandit over
  retrieval weights — worth building only when real usage data exists to
  tune against (ponytail: no bandit before data).
- **Boundary:** human-in-the-loop for any adaptation. The weekly review
  suggests; Stanley (or the agent acting for him) decides. No autonomous
  re-tuning without a recorded pattern.

## Instruments

- `scripts/query-log-review.py [--since N]` — weekly ritual: volume,
  clients, zero-hit gaps, cluster jumps (memory-to-memory navigation
  quality), latency/cache health, suggested next actions. Exit 0 always.
- `alexandria sync pi-sessions` — the memory-generation step (distil).
- Wiki re-synthesis per cluster — the compression step.

## Success criteria for this chapter (usage-driven)

1. Alexandria is the default memory layer for Stanley's Pi + agent work
   (search before asking; answer for synthesis).
2. The weekly review produces at least one real improvement per month
   (a re-synthesized wiki page, a retrieval fix, a weight change).
3. The contest becomes quarterly sanity telemetry — never the gate again.

## History

Replaces the measurement-driven loop (phase-2 golden sets, phase-3
contest cycles) as the primary improvement mechanism. The phase-2/3
records stand as the certified baseline; the signed loop-termination
contract remains the guardrail against unbounded re-measurement.
