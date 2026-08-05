# Decision record: phase-2 full-sweep execution model (reconstructing "§6.1a")

Date: 2026-08-05. Status: accepted.

## Why this document exists

`SPEC-phase2-eval.md` and `RUBRIC-skip-log-audit.md` both reference
"§6.1a execution-model invariants" as something already specified —
exhaustive enumeration, deterministic/logged skip predicates, side-effect-
free nodes with serial map-reduce fold, bounded repair loop with anti-
gutting guard. That reference was almost certainly decided verbally earlier
in the same working session that produced those two documents, and never
made it into a durable, findable artifact of its own — a real process gap,
found while drafting `WORK-ORDER-phase2-synthesis-core.md`, which could not
locate the source document in this repo, session history, or memory.

Rather than reconstruct it unilaterally from a secondhand paraphrase, it
was rebuilt here through direct confirmation, one real fork at a time (the
`grill-me` protocol), not assumed. Two of the five original items turned
out to already be built, just not under this name; two turned out to be
one unified architecture, not two separate ones; the actual open surface
was smaller than it first looked once broken apart.

**Anything that referenced "§6.1a" from here on should reference this
document instead.** The numbering was never real — there is no larger
numbered master document this section belongs to, as far as this project's
history shows. Treat that as closed; do not go looking for a "§6" elsewhere.

## The five original items, resolved

### 1 & 2. Exhaustive enumeration + deterministic/logged skip predicates — one principle, two altitudes

Not independent. Judge 2 (`coverage.py`) already enforces this at the
*chunk* level: every chunk in a gathered pool is either cited by a claim or
logged with a deterministic skip predicate (`duplicate_of:<id>`,
`below_salience:<score>`, `out_of_scope:<rule>`) — no silent drops,
enforced as a build error, not a warning.

"Exhaustive enumeration" is the same discipline one level up, at the
**document** level: every document in the corpus either lands in some
topic cluster (eventually becomes part of a synthesized page), or is
excluded with an equally deterministic, logged reason
(`below_cluster_threshold`, `no_cluster_match`). 100% accounting, at both
altitudes, by the same rule.

### The enumeration unit: cluster-based, confirmed

Considered three options: per-document (exhaustive but redundant — many
documents restate the same fact, a problem that hit ground-truth
construction three separate times this project's history), cluster-based
(groups related documents into topics first), and query/demand-driven
(reactive, not a batch sweep at all — probably not what "exhaustive" meant
originally, since the word implies a complete systematic pass).

**Decided: cluster-based.**

### Two distinct clustering passes, one shared embedding pipeline

Cluster-based enumeration needs a clusterer, and there is already a
different, previously-adopted clustering item on the roadmap: index-time
near-duplicate dedup clustering (adopted, not yet built, from the earlier
OSS-strengths deliberation). These are genuinely different similarity
notions, not the same job twice:

- **Dedup clustering**: finds chunks restating the *same fact* — needs a
  tight similarity threshold. A real prior check using 5-gram shingle
  similarity at ~0.30 still missed a genuine paraphrased duplicate
  (a same-fact pair recorded minutes apart, different wording), showing
  even a moderately tight threshold can be
  too loose or too strict depending on phrasing distance.
- **Topic clustering** (this document's subject): finds chunks on the
  *same topic* asserting *different, often causally or temporally linked*
  facts — the shape of the 8 hand-built `synthesis-clusters-v1.jsonl`
  entries (bug → root cause → fix → regression chains). This needs a much
  looser threshold; conflating it with dedup clustering would either merge
  distinct topics or fail to group a real causal chain.

**Decided: one shared embedding pipeline (the MLX embedder, already built
and measured), two distinct clustering passes with independently-tuned
parameters.** Not two infrastructures; one infrastructure, two configured
uses.

### 3 & 4. Side-effect-free nodes + serial map-reduce fold — one execution model, not two

Also not independent — describing the same architecture from two angles.
A "node" is one topic's full pipeline run (gather → synthesize → judge →
repair, already built per `WORK-ORDER-phase2-synthesis-core.md`).
**Side-effect-free** means each node is a pure function: `(topic, current
accumulated sweep-state) -> (page, state delta)`. It never mutates shared
state directly. **Serial map-reduce fold** is what that purity enables:
topics are processed one at a time, in a fixed deterministic order, and
each node's result folds into the accumulated state before the next topic
starts — not a parallel map with a separate reduce phase afterward.

**Decided: yes, as one unified architecture**, for three concrete reasons:

1. **Cross-page redundancy avoidance.** The same near-duplicate problem
   that hit the retrieval golden set, the coverage-calibration cases, and
   the contradiction pairs — three separate times this project's history —
   recurs at the *page* level in a full sweep. A serial fold lets topic
   N check the accumulated state and find "this fact is already covered by
   page K," citing it instead of re-synthesizing a near-duplicate page.
   Independent parallel runs have no way to know that.
2. **Deterministic, reproducible sweep ordering** — a fixed processing
   order makes a sweep re-run diffable against a prior one, consistent
   with this project's eval-gate discipline everywhere else.
3. **Concurrency risk against the shared LLM gateway is not hypothetical.**
   A real, confirmed bug was found and fixed the same day this document
   was written: request cross-contamination under concurrent load against
   the gateway multiple models were routed through. A full sweep issuing
   gather + synthesize + judge (+ possibly repair) calls per topic, across
   potentially hundreds of topics, is exactly the load shape that bug
   showed can go wrong in hard-to-diagnose ways. Serial processing is the
   safer default against a gateway now known to have real failure modes
   under concurrency, not caution for its own sake.

### 5. Bounded repair loop with anti-gutting guard — already built

Already implemented in `WORK-ORDER-phase2-synthesis-core.md` §4.4, quoting
`SPEC-phase2-eval.md`'s own text directly: entailment failures may be fixed
by finding a real citation or by removing the claim; a removal is logged as
a skip and re-triggers *both* judges on the next iteration, not just
entailment — deleting content must not be a viable way to quietly pass.
Bounded to a small, fixed iteration count; never retries unboundedly.

## What this unblocks

`WORK-ORDER-phase2-synthesis-core.md` §0 and §8 excluded full-corpus sweep
orchestration specifically because this spec was missing. It is no longer
missing. The core-mechanism work order's *scope* does not change, though —
the split between core pipeline and full-sweep orchestration was always a
deliberate mirror of how phase 1 itself was split (retrieval library, then
a separate eval-harness order): prove the mechanism on one page before
scaling to "sweep everything," regardless of whether the orchestration
spec was available. A full-sweep orchestration work order can now be
written against this document; whether to write it next is a separate,
open decision, not automatic.
