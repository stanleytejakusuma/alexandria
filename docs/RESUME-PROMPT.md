# Resume prompt (cold start)

Paste the block below into a fresh session. It assumes **zero** memory of any
prior conversation.

Supersedes all earlier versions of this file. Last verified against live repo
state on 2026-08-13 (HEAD `55d646b`, suite run to completion: 654 passed).

---

```
You are picking up work on Alexandria, at ~/codebase/alexandria. You have no
memory of prior sessions. Everything you need is on disk; this prompt tells you
where and warns you about what is stale.

READ FIRST, IN THIS ORDER
  1. AGENTS.md (repo root) — HOW work happens here. The WORK ORDER protocol,
     branch discipline, test invocation, commit rules, CLI surface, hard
     constraints. Written cold by a session with no context, verified against
     the repo. Treat it as authoritative on process.
  2. docs/STATE-OF-PLAY-2026-08-13.md — WHAT is built vs. paper, the bugs found
     and their mechanisms, every measurement worth not re-deriving.
  3. docs/LEARNINGS.md — short; traps not yet folded into AGENTS.md.
  4. docs/BACKLOG.md — the open-work index, with a verified status table for the
     Top 10.
These four supersede anything you infer from code alone. Where this prompt and
those files disagree, THIS PROMPT IS NEWER — it records corrections made after
they were written, listed under CORRECTIONS below.

CURRENT STATE (verified, not recalled)
  HEAD 55d646b. Suite: 654 passed in 51s — actually run to completion, which
  resolves LEARNINGS.md's open caveat that only --collect-only had been run.
  Working tree: docs/BACKLOG.md and docs/RESUME-PROMPT.md modified by the
  session that wrote this prompt; commit them if not already committed.
  Everything else pushed.

  The repo is the ENGINE (src/alexandria/, package `alexandria`). The corpus it
  indexes is a SEPARATE repo at ~/alexandria-corpus and is out of scope for
  edits from here.

WHAT IS BUILT AND SHIPPED
  docs/SPEC-write-path-and-serve.md — fully implemented. Pending markers, flock
  write lock, ordered promote, reconcile, liveness, `alexandria serve` (stdlib
  http.server: /health /search /answer /remember), backup/restore, index
  manifest, inbox injection guard, negative eval set + precision gate. Every
  gate in §9 maps to a mutation-verified test.

WHAT IS PAPER — DO NOT DESCRIBE AS WORKING
  docs/SPEC-data-model-and-ambient-capture.md (1,019 lines). Typed observations,
  entity_id/entity_rev/supersedes, tombstones, ambient capture, erasure. None
  built. Two adversarial review rounds spent — that is the cap; do not spend a
  third on this spec, implement or revise it directly.

CORRECTIONS TO THE DOCS ABOVE (this prompt is the newest source)
  - Q1 (erasure scope) is recorded as OPEN in the spec §9 and "Phase 5 cannot
    start without this answer." A PRIOR SESSION CONCLUDED IN CONVERSATION that
    the answer is tombstone-first — making something invisible is a prerequisite
    for destroying it, so it forecloses nothing, and only escalates to real
    erasure if someone other than the operator ever gains access to a corpus
    (which makes right-to-erasure legal rather than optional). THE USER NEVER
    RATIFIED THIS. It is not in the spec. Treat Q1 as open and get an explicit
    answer before starting Phase 5; do not cite it as decided.
  - Q5 (relevance floor): the first published answer was WRONG and the
    correction is the interesting part. `separation()` read scores[0] for every
    positive, but a "hit" only means the target appeared somewhere in top-k, so
    a rank-3 hit was scored by the wrong document. Fixed in a5507a1 with two
    regression tests. Recomputed: positive minimum collapses 0.1190 → 0.0274
    while the median barely moves (23 of 31 hits are at rank 1). A 0.12 floor
    retains 90.3%, not the 100% originally published. Five positives fall below
    any useful floor, ALL in the zero-overlap band. Conclusion: a score floor is
    the WRONG INSTRUMENT, not a mistuned one. Spec §9 Q5 = PARTLY RESOLVED,
    gate R3 = PROVISIONAL. BACKLOG.md was stale on this and has been corrected.

PROGRESS LEDGER — 17 of 22 tracked items closed (restore this as your todo list)
  Closed, all shipped with mutation-verified tests:
   #1  busy_timeout/WAL on every SQLite writer (F1)
   #2  locked + atomic generation counter, fail-loud on corruption (F2, W3b)
   #3  index manifest: provider/model/revision/dim/norm/dtype, refuse on
       mismatch or absence (F4)
   #4  split cache_hit codes: query-cache vs answer-retrieval-cache (F3)
   #5  cost ledger on the /answer path from llm.py last_usage (F5)
   #6  pending marker directory, O_CREAT|O_EXCL create / unlink consume (F6, W1)
   #7  flock write lock + local-fs startup check + ordered promote
       (W3, W3a, W4, W5, W6)
   #8  liveness: oldest-pending-age, reconcile independent of the pending list
       (W7, F6)
   #9  alexandria serve — stdlib http.server, /health /search /answer /remember
       (S0–S10)
   #10 backup/restore of .alexandria state (B1)
   #11 tenancy tripwire test (T1)
   #12 extension routes through serve when reachable, falls back to CLI
   #14 flushed 4 writeups rescued from non-persistent /tmp into the corpus
   #15 corrected a stale memory note claiming 1,444 knowledge-graph memories had
       no connector — falsified; connectors/knowledge_graph.py exists and the
       weekly loop syncs it. That note was actively routing agents AWAY from
       Alexandria.
   #16 negative eval set + precision gate (BACKLOG #21's precision half)
   #17 CLI attribution: --user DELETED rather than validated; identity now
       derived from the OS user (664d896)
   #19 spec review round 2 of 2 — the cap is now spent on this spec

OPEN TODOS — verbatim, restore these five
  #13 Full suite green throughout; final gate-to-test mapping audit; second-
      harness real-path acceptance canary. The canary was proven by curl from
      the other host but never wired into that host's actual skill. NEEDS
      EXPLICIT USER APPROVAL: capital-bearing host, and a prior session already
      caused a reboot incident there on 2026-08-11 by running an index build
      on it.
  #18 Opus review pass on all Sonnet-executed work from the 2026-08-12 session
      (write-path package + spec). Both review rounds so far covered the SPEC
      only, never the shipped CODE. STANDING USER PREFERENCE — do not mark a
      session done without offering it.
  #20 Golden set n=49 is underpowered and has no significance bar — a small
      recall move is indistinguishable from noise. Negative cases closed the
      precision half of BACKLOG #21; this is the remaining half. Also neither
      the golden set nor negative-v1.jsonl lives in this repo (both in the
      corpus), so the gate is not independently reproducible. Blocks BACKLOG
      #29 offline policy tuning.
  #21 Negative cases decay as the corpus grows — distillation will add documents
      containing every term in negative-v1.jsonl (Kafka, MongoDB, Stripe…),
      silently invalidating absence claims. Re-verify against the
      `verified_against` field (46,021 chunks at verification time) whenever the
      golden set is reviewed.
  #22 Add ≥10 IN-DOMAIN negatives — queries about this corpus's own subject
      matter that it happens not to cover. Current set is 21/22 out-of-domain
      brand queries, so the 0.0238 negative median is partly an artefact of easy
      negatives. This is what keeps gate R3 PROVISIONAL. Found by Opus round 2.

  Plus BACKLOG.md's still-open Top 10: #5 enrichment injection framing, #6
  deletion/erasure path (blocked on Q1), #8 real attribution (CLI half closed by
  664d896; --caller remains an unverified hint), #9 citation linkage, #10
  procurement floor.

STANDING OBLIGATIONS (carry these forward; they are not one-off tasks)
  - Offer an Opus review pass after Sonnet-executed work completes. Explicit,
    repeated user preference. Todo #18 is the outstanding instance.
  - Two adversarial review rounds per artifact, maximum. Both are spent on
    SPEC-data-model-and-ambient-capture.md — implement or revise it directly
    rather than commissioning a third.
  - Session hygiene: this agent's latency scales with retained history and
    cannot be compacted back down. Before substantial multi-turn work, offer to
    spawn a fresh child session rather than growing the current one. The user
    will not remember to ask — raise it yourself.
  - Never fire anything against the capital-bearing second host without explicit
    per-instance approval. Read-only probes are fine; index builds are not.

IF YOU START PHASE 1, IT CANNOT SHIP WITHOUT THESE THREE
  All three were found by adversarial review and are recorded in spec §D4a/§3.4:
  1. `deleted` and `entity_id` must be INDEXED columns, not frontmatter-only. A
     frontmatter-only revision produces an identical chunk_id, store.upsert
     overwrites in place, and neither SCALAR_FIELDS nor METADATA_COLUMNS carries
     `deleted` — so a tombstone stays fully retrievable and the projection join
     fails OPEN.
  2. Revision files must be path-disjoint. `source_filename()` is deterministic
     on (source, source_id, title) and `Doc.write` is an unconditional
     write_text, so revision 2 silently destroys revision 1.
  3. `burst_id` must be stable. It currently hashes every message, so an open
     session gaining one turn gets a new id and is re-distilled — demonstrated
     live: 3a862d788848 → 4f7cf01aaf04 after one added turn. At an hourly sweep
     a 13-hour session yields 13 permanent near-identical document sets in a
     corpus with no deletion path. Fix: derive from (session path, first-message
     timestamp, window ordinal).

HARD CONSTRAINTS (AGENTS.md has the full list; these are the ones that bite)
  - NO DELETION PATH. Anything written to a real corpus is permanent. Treat
    every remember/promote/sync against ~/alexandria-corpus as one-way.
  - Never run a corpus index build on the second host — live capital services,
    and a 45k-chunk CPU embed took it down on 2026-08-11.
  - Never pass --enrich. Measured -6.1 pts recall (0.673 → 0.612) against a
    same-corpus control; see docs/DECISION-enrichment-2026-08-11.md before
    proposing to revive it.
  - `sync` alone makes nothing retrievable — you must then run `index`.
  - Tests: `.venv/bin/python -m pytest tests/ -q`. The bare `.venv/bin/pytest`
    binary fails full-suite collection. Don't leave ALEXANDRIA_EMBED_PROVIDER
    exported — it fails test_mlx_is_the_default_embed_provider spuriously.
  - Commits: `git commit -F <tmpfile>`, never a heredoc in `-m "$(...)"`.
    Pre-commit runs a leak scan then a 60–90s eval gate, so give Bash calls
    timeout 120+. The scanner only sees STAGED files. A zero-width joiner
    (U+200D) defeats it while still leaking the name to a human reader.
  - No private host or agent names in this repo — it is public and scanned.
    Private detail goes in a companion doc OUTSIDE the repo; that pattern is
    already in use.
  - Cold queries against the real corpus take 25–50s (one ETIMEDOUT recorded).
    Warm through `serve`: 0.427s measured. Budget accordingly.
  - The golden set's must_retrieve doc ids are brittle — a red eval may be
    golden-set staleness rather than a real regression. Investigate before
    reverting real knowledge to protect a metric.

THE FINDING THAT SHOULD SHAPE YOUR PRIORITIES
  During the 13-hour session that BUILT this system, its author issued zero
  retrieval queries against it. Of 442 queries logged that day, ~430 were the
  eval gate and 5 were smoke tests. Not carelessness — at every decision point a
  200ms `rg` beat a 30s tool that might time out. The aggregate of locally
  correct decisions was a system nobody used. This is the empirical argument
  that ambient capture is the product, not a convenience feature, and it is why
  cold-query latency is on the critical path rather than in the polish pile.

THE RULE THAT KEPT MATTERING
  Trust outcomes, not exit codes. Every significant bug found shared one shape:
  a step reported success while doing nothing — the weekly loop that never once
  ran (its log dir was never mkdir'd, so every >> aborted the sync while
  `git commit --allow-empty` still succeeded, for three days), the enrichment
  scorer that ranked its own hits last, a job that reported "done" without
  writing its log, and three tests that passed against broken code including one
  written by the session that was auditing for exactly that. When a step claims
  success, confirm the observable result changed — row counts, generation
  numbers, file mtimes.

START BY
  Running the suite to confirm 654 still passes, and `git status` to see whether
  the two modified docs above were committed. Then tell me what you'd like to
  pick up. If you have no preference, my recommendation is #20 — a significance
  bar is what would have caught the Q5 scoring bug before it was published, and
  it unblocks offline policy tuning.
```

---

## Notes for whoever pastes this

- The three case studies are the fastest way back into the design:
  `CASE-STUDY-the-write-path.md` (what runs today — every command runnable),
  `CASE-STUDY-ambient-capture.md` (what capture would cost, measured on a real
  55MB transcript: 16 bursts, 82 calls, ~2.4M input tokens), and
  `CASE-STUDY-a-day-with-alexandria.md` (operator's chair; every claim tagged
  ✅ measured or ⏳ expected).
- Six load-bearing findings from the 2026-08-12/13 session were flushed into the
  real corpus and verified retrievable *by query*, so `alexandria search` will
  find them — including the burst_id defect and the zero-query audit.
- If a work order is what you were handed, AGENTS.md §"How work happens here"
  is the contract; that protocol predates all of today's documents (six
  `docs/WORK-ORDER-*.md` files already existed) and should not be reinvented.
