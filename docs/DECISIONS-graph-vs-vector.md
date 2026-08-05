# Decision record: graph-structured retrieval vs. hybrid vector/lexical

Date: 2026-08-05. Status: accepted, with a scheduled re-examination point.
Supersedes nothing; constrains phase 2 (synthesis gather) and sets an
explicit gate before phase 4 (product ship).

## The question

Whether Alexandria should adopt a persistent, standing knowledge-graph
index — entities, typed relationships, traversal — as a complement to its
existing hybrid retrieval (BM25 lexical + dense embeddings + cross-encoder
rerank), on the argument that the two answer genuinely different questions
("what's similar" vs. "what's connected") and therefore complete each
other's coverage.

## Inputs

Real evidence gathered in one sitting, not theory: a technical video digest
("Graphs vs Vectors: The Real Shift Happening in RAG," real benchmarks —
Microsoft GraphRAG, LazyGraphRAG, GraphRAG-Bench, HippoRAG, Anthropic's own
public KG cookbook); a full repo deep-dive of `TencentCloud/TencentDB-Agent-
Memory` (see `DECISIONS-multi-actor-posture.md`); a full repo deep-dive of
`abhigyanpatwari/GitNexus` (45k-star, actively maintained, real eval
discipline — code-structure graph via deterministic AST parsing, LLM used
only for output generation, never extraction); two `red-thinker` adversarial
rounds on this same question (`[red] hop=1 model=claude-fable-5`, both
rounds), the second specifically testing whether a stated architectural
ambition toward modularity/generality changes the verdict.

## The complementarity argument is correct — and insufficient on its own

Vectors and graphs do answer different questions, and a corpus can
genuinely have both kinds of information need. That is not in dispute. But
"conceptually complementary" is a low bar — nearly any additional signal
(recency, popularity, citation count) is complementary to some degree. This
project's own standard, applied everywhere else in its stack (BM25, dense
embeddings, and the reranker were each added because *measurement* showed
they closed a real gap the others left open, not because they were
abstractly synergistic), is: does adding this modality *measurably* close a
gap the current stack doesn't, at a cost justified by that measured gap.
By that standard:

- Alexandria's one measured retrieval weakness (zero-overlap-band recall
  38.9%, see the golden-set work) is a **representation** problem — the
  answer lives in one document, retrieval just can't locate it through
  paraphrase. A graph cannot fix this: traversal still needs an entry node,
  and finding that node is exactly the step that's failing.
- Alexandria's one **demonstrated** connectivity-shaped need — hand-building
  `synthesis-clusters-v1.jsonl`'s causal fact-chains across documents —
  already has a cheaper, working answer: a bounded, disposable gather loop
  at synthesis-write-time (seed retrieve → one LLM pass asking what's
  referenced-but-missing → one follow-up retrieve → merge, then discard).
  This is LazyGraphRAG's real contribution (defer structure-building to
  use-time) ported without the rest of GraphRAG's machinery, and it answers
  the "what's connected" question without ever persisting an edge.

## The asymmetry a straight complementarity argument misses

BM25 and dense embeddings are both index-once, low-maintenance: neither
needs ongoing re-verification that yesterday's entries are still correct.
A persistent graph over **prose** (not code — see the GitNexus finding
below) does: every new note is a new extraction pass, every superseded fact
is a stale edge someone must catch, and unlike a retrieval miss, a
**wrongly-extracted edge can actively mislead** a synthesis page rather than
merely fail to help. Every real system examined tonight confirms this cost
is not hypothetical: Anthropic's own public KG cookbook discloses a
Precision 1.0 / Recall 0.55 failure case (45% of true facts silently
missed); GraphRAG's extractor scored 18 points worse than a purpose-built
one at the identical task.

**GitNexus is the sharpest confirming evidence, from the opposite side of
the line.** A serious, 45k-star, actively-maintained tool graphs *code*
structure using 100% deterministic AST parsing — tree-sitter, no LLM
anywhere in the extraction path, LLM used only to narrate an
already-correct graph. Its own architects avoided exactly the
judgment-under-uncertainty step (LLM-inferred relationships) that every
prose-oriented graph system above pays a real, measured cost for. Alexandria
ingests chat logs, session transcripts, and notes — prose, not code. No
deterministic parser exists for "this decision causally supersedes that
one." Adopting graph structure for Alexandria's actual content means paying
the cost GitNexus's own design choices show is worth avoiding when it can
be avoided.

**The deepest asymmetry is observability, not cost.** A vector index's
failure mode is visible and already instrumented — the golden set, sliced
by overlap band, shows exactly where hybrid retrieval is weak, watchably,
over time. A graph's failure mode (silent extraction error, stale edge,
wrong entity resolution) is invisible by construction unless a *separate*
measurement apparatus is built for it — real, nontrivial work, demonstrated
by how much rigor this project's own coverage/contradiction-audit
calibration required for a narrower problem. This project's identity is
"measure, don't assume, judge before the player exists." Adopting something
whose failure mode is structurally harder to see, for a need the project's
own measurements don't show, is a bet against its own founding method, not
just a cost tradeoff.

## Decision

**REJECT** a persistent, standing knowledge-graph index, now.
**ADOPT** (already in the phase-2 spec) the bounded, disposable
gap-detection gather loop as the complementary "what's connected" answer,
scoped to synthesis-write-time, nothing persisted.
**REJECT** building a graph-capability interface/seam ahead of a second
implementation — per round 2's finding, a code-abstraction seam guessed
before a real second implementation exists is more likely to lock in the
*wrong* boundary than to save future work; the cheap, correct seam already
exists by default (markdown/frontmatter and the eval infrastructure are
already schema-agnostic).

This is explicitly **not** "no, forever." It is "not yet, with a specific,
scheduled point of re-examination" — see below.



## Re-entry triggers, quantified (added 2026-08-05)

The two original triggers below were deliberately qualitative when this
document was first written; sharpened here into falsifiable numbers so the
phase 3 → 4 checkpoint has something concrete to check against instead of
re-litigating from scratch. Metrics 1-4 and 6 are **proposed operational
thresholds, not yet-measured facts** — no real phase-3 usage data exists
yet to calibrate them against. Metric 5 is a hard implementation gate, not
a trigger for starting work.

1. **Real (not simulated) gather-loop recall on relationship-shaped
   queries drops below ~75%.** Judge 3 already gates the *simulated*
   calibration set at ≥90% contradiction-pair recall (`SPEC-phase2-eval.md`).
   The real trigger is sustained recall below ~75% on actual phase-3
   shadow-run data, measured across at least 30 real synthesis pages (below
   that, small-n red-flag territory per the same Wilson-interval discipline
   used throughout this project's eval work). One bad run is not a trigger;
   a sustained pattern across multiple measurement rounds is.
2. **The gather loop's follow-up round fires on more than ~40-50% of all
   synthesis pages.** The loop assumes multi-hop need is the *exception* --
   most pages should resolve in one retrieval pass. If the second round is
   needed on most pages instead, the corpus is more relationship-dense than
   assumed, and a standing structure may genuinely amortize better than a
   disposable pass repeated constantly.
3. **Corpus growth stabilizes below ~5%/month chunk growth, AND
   relationship-shaped query volume sustains above roughly 50-100/day.**
   Both conditions must hold together -- high query volume over a
   still-growing corpus does not flip the math, because extraction cost
   keeps getting re-paid regardless.
4. **A real second corpus shows meaningfully higher relationship density
   than this one** -- e.g. 3x+ the cluster-shaped fact-chain density per
   chunk that `synthesis-clusters-v1.jsonl` found here (8 usable clusters
   out of ~33k chunks, and that took real, effortful, targeted search),
   measured under the same search effort.
5. **Hard gate on any real extraction pipeline, before it ships (not a
   trigger to start work):** ≥90% precision and ≥85% recall on a
   calibration set built the same way as `coverage-calibration-v1.jsonl`
   -- real ground truth, double-labeled, adjudicated -- specifically to
   prove it doesn't repeat Anthropic's own disclosed 45%-fact-loss failure
   mode before any output from it is trusted.
6. **Ongoing maintenance stays under a real time budget -- proposed ceiling
   ~30-60 min/week of manual review/correction, sustained.** If early
   experimentation shows upkeep costing meaningfully more than that on an
   ongoing basis, disqualifying regardless of how good metrics 1-4 look --
   there is no team to absorb it on a one-person project.

**Primary concern if this is ever built, named explicitly so it can't be
quietly forgotten under future time pressure:** not the engineering --
the risk that the eval-first discipline used to build `coverage-
calibration-v1.jsonl` and its rubric gets skipped the second time, exactly
when the temptation to skip it is highest (near a product deadline, at the
phase 3→4 boundary). A wrongly-extracted relationship does not just fail
to help -- it actively misleads a synthesis page, with nothing currently
watching for it. This project's own origin story is a vault that died from
exactly this shape of deferred judgment. Metric 5 exists specifically to
force the same discipline to happen again, not be assumed.

## Scheduled checkpoint, not just an ambient trigger

Per the README's phase table, **phase 3 (harness extensions, blinded
side-by-side against the incumbent memory tool) is precisely where trigger
1 gets a chance to actually fire or not** — real other consumers, real
usage, real relationship-shaped queries in the wild, not projected ones.
**This decision is formally re-examined at the phase 3 → phase 4 boundary**
— after real consumer usage data exists, before phase 4 finalizes the
answer endpoint and ships the product surface. This is a deliberate gate,
not a passive "revisit if something comes up": the phase 3 → 4 transition
does not proceed without an explicit yes/no re-check against all six
quantified metrics above, recorded as an update to this document either way
(including "no change, still REJECT" — a checked box, not silence).
