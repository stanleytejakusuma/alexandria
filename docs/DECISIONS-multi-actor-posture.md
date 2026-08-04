# Decision record: multi-actor posture & TencentDB-Agent-Memory deliberation

Date: 2026-08-04. Status: accepted. Supersedes nothing; constrains phase 2+.

## Context

Before starting phase 2 (synthesis sweep), we evaluated whether Alexandria is
under-built for multi-actor use — "Person A starts 40% of the work, Person B
continues" — using TencentCloud/TencentDB-Agent-Memory (~13k stars, MIT) as the
strongest available counter-design. Evidence: full repo deep-dive with file:line
receipts, independent video-review digest, one adversarial review round, and two
of the reviewer's claims hand-verified against the cloned source. Two of our own
initial claims were corrected by that verification (recorded below, deliberately
— wrong-then-corrected is evidence the process works).

## Verdict on the counter-design

A well-engineered delivery mechanism around a **publicly** unvalidated epistemic
core ("publicly" because the code drop strips all tests and evals; vitest
configs reference dead test paths, so internal evals likely exist). They solved
distribution — proxy interception with no chat-LLM on the hot path, KV-cache-
aware injection placement, real pure-function ACLs with agent-as-subject —
and skipped verification: no public eval, no faithfulness checking, no reranker,
one self-run unreproduced benchmark.

Their sharpest flaw, verified at source (`tdai-l1-recall-injector.ts`):
**collision-without-reconciliation**. Memories from different agents' scopes
meet at read time via "borrowed agent" recall, merged purely by similarity
score, injected with zero reconciliation. Contradiction handling is scope
isolation at write time + silent collision at read time. (Our first reading —
"contradictions never meet" — was wrong; the truth is worse for them. Also
corrected in their favor: the injected block does carry per-memory provenance
tags and an ephemerality disclaimer, so model-level citability survives; the
blindness is at the application layer, where logging, lint, and debugging live.)

## Decisions

### Adopted

1. **`store|update|merge|skip` action space** for write-time near-duplicate
   handling — the shape (new entry → recall top-k similar → decide) folds into
   our parked index-time dedup work and phase-2 synthesis (multiple extractions
   describing one fact). Adopt the action space, not their LLM implementation;
   our retrieval stack feeds the decision. Their silent-skip-when-degraded
   behavior is explicitly NOT adopted — degraded dedup must fail loud.
2. **Attribution seams, no enforcement.** Entries gain optional `author`
   (which actor wrote this) and `visibility` fields now, while the schema is
   young and phase-2 synthesis can inherit them from day one. Retrofitting
   provenance onto synthesized pages after the fact is the expensive path; the
   fields are the cheap path. No ACL checking, no identity service, no
   enforcement of any kind until a real second writer exists.

### Rejected, with reasons

3. **Interception-as-philosophy** (memory injected by a proxy, invisible to the
   application). Poisoning amplifier at team scale: recall ranks purely by
   similarity, so the more relevant the query, the higher a poisoned memory
   ranks; novel wrong claims bypass near-duplicate gates by definition; and the
   application has no record that memory shaped the answer, so a wrong output
   cannot be traced to the memory that caused it. Alexandria stays
   explicit-retrieval: the caller sees the result list, synthesis cites doc_ids,
   the faithfulness grader checks claims against those sources.
4. **Service topology as a virtue.** Their own premise ("four data shapes want
   four search strategies") justifies retrieval *policies behind one interface*,
   not network services. Physical distribution buys independent scaling, team
   deploy cadence, and fault isolation — none apply at single-user scale, and
   every boundary spends serialization/network/auth against a 500ms p50 budget.
   Additional hard blocker: MLX (chosen by measurement) has no Metal passthrough
   in Docker on macOS. Module boundaries now; the first physical seam, if a
   hosted multi-writer deployment ever exists, is the HTTP API.
5. **Governance-as-epistemics.** Visibility levels and a human review panel
   answer "who may see this claim," never "is this claim true, current,
   consistent." A wrong memory with team visibility is wrong for everyone
   authorized to see it. Alexandria's ordering stays: epistemic mechanisms
   (faithfulness grading, citation lint, contradiction scan, eval gates) are
   the core; governance is a future add-on when there are actors to govern.

### Rejected by measurement

6. **Type-aware retrieval policy** (recency for chat-shaped, etc.). Tested
   against the stratified golden set: zero-overlap-band failures do NOT cluster
   by entry type (zero-band: memory 3/6, observation 4/11, task 0/1; the
   type gap flips direction in the partial band). The zero-band weakness is
   band-wide — a paraphrase/semantic-matching problem — so per-type search
   strategies attack the wrong axis. Re-test if a larger golden set shows
   type clustering.

### Noted, conditional

7. **KV-cache-aware injection placement** (recalled context before the user
   turn, not in the system prompt, to preserve upstream KV-cache). Genuinely
   clever; relevant only if Alexandria ever builds an auto-context surface,
   which is itself undecided.

### YAGNI

8. **ACL model.** Their permission-checker (owner → membership → visibility →
   role → explicit ACL, `agent` as first-class subject) is a good reference
   implementation — and worth exactly nothing until multi-user is a committed
   feature. Not built, not stubbed, not "cribbed for later."

## Posture

Alexandria's multi-actor story is **design credibility, not deployed product**:
the data model carries attribution seams so the architecture visibly extends to
multiple writers, while enforcement waits for a real second writer. The
differentiator against team-memory systems is the epistemic layer they lack —
measured retrieval, calibrated faithfulness, enforced citations — which is not
bolt-on-able later, whereas plumbing demonstrably is.

## Open

Sequencing of the seams, dedup, zero-band retrieval work, and any consumer API
relative to phase 2 is deliberately undecided as of this record.
