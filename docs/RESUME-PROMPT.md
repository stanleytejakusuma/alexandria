# Resume prompt (post-compaction)

Paste the block below into a fresh session to continue the Alexandria work.

---

```
We're continuing work on Alexandria (~/codebase/alexandria). Context was
compacted, so start by reading docs/STATE-OF-PLAY-2026-08-13.md — it is the
handoff and it is authoritative over anything you think you remember.

WHERE WE ARE
HEAD a968242, 654 tests passing, working tree clean, everything pushed.

The write-path package is BUILT and shipped: pending markers, flock write lock,
ordered promote, reconcile, liveness, `alexandria serve`, backup/restore, index
manifest, injection guard, negative eval set + precision gate. Every gate in
docs/SPEC-write-path-and-serve.md maps to a mutation-verified test.

The data model is PAPER: docs/SPEC-data-model-and-ambient-capture.md, 1,019
lines, two adversarial review rounds spent (the cap). Typed observations,
entity_id/entity_rev/supersedes, tombstones, ambient capture, erasure — none of
it built. Do not describe any of it as working.

OPEN WORK
  #13 H‍ermes real-path canary — proven by curl from the second host, never
      wired into its actual skill. NEEDS USER APPROVAL: capital-bearing host,
      and this session already caused a reboot incident there on 2026-08-11.
  #18 Opus review pass on the write-path CODE (rounds 1-2 covered the spec
      only). Standing user preference — offer it, don't skip it.
  #20 Golden set n=49 has no significance bar; small recall moves are
      indistinguishable from noise. Blocks BACKLOG #29.
  #21 Negative cases decay as the corpus grows — re-verify against
      verified_against (46,021 chunks at verification time).
  #22 Add >=10 IN-DOMAIN negatives; 21 of 22 current ones are out-of-domain
      brand queries, so the negative set is easier than reality. Keeps gate R3
      PROVISIONAL.

If Phase 1 implementation starts, it must not ship without the three
requirements in state-of-play §2: `deleted`/`entity_id` as INDEXED columns (a
frontmatter-only tombstone stays fully retrievable and a projection join fails
OPEN), path-disjoint revision files (Doc.write is an unconditional overwrite, so
rev 2 destroys rev 1), and a stable burst_id (currently hashes every message, so
an open session is redistilled every sweep — demonstrated live).

DECIDED, DO NOT REOPEN
  Q1 erasure scope: tombstone-first. Making something invisible is a
  prerequisite for destroying it, so it forecloses nothing. Only escalate if
  someone other than the operator gains access to a corpus — that makes
  right-to-erasure legal rather than optional.
  Storage: LanceDB + FTS5, no vector-DB server, no ANN index yet (re-entry at
  ~150-200k chunks or >250ms flat scan at working k).
  Review budget: two rounds per artifact, both spent on the data-model spec.

HARD CONSTRAINTS
  - There is NO deletion path. Anything written to the corpus is permanent.
  - Never run a corpus index build on the second host — it carries live capital
    services and a 45k-chunk CPU embed took it down on 2026-08-11.
  - Never pass --enrich (measured -6.1 pts recall).
  - `sync` alone makes nothing retrievable; you must then `index`.
  - Tests: `unset ALEXANDRIA_EMBED_PROVIDER && .venv/bin/python3 -m pytest
    tests/ -q`. The bare .venv/bin/pytest binary fails.
  - Commits: always `git commit -F <tmpfile>`; pre-commit runs a leak scan then
    a 60-90s eval gate, so allow timeout >= 120s. The leak scanner sees only
    STAGED files, and a zero-width joiner defeats it while still leaking the
    name to a human reader.
  - No private host or agent names in this repo — it is public and scanned.

THE RULE THAT KEPT MATTERING
Trust outcomes, not exit codes. Every significant bug this session shared one
shape: a step reported success while doing nothing — the weekly loop that never
ran once, the enrichment scorer that ranked its own hits last, a job that
reported "done" without writing its log, and three tests that passed against
broken code including one I wrote myself. When a step claims success, confirm
the observable result actually changed.

Tell me what you'd like to pick up. If you have no preference, my
recommendation is #20 — a significance bar is the thing that would have caught
the separation-metric bug before it shipped, and it unblocks offline tuning.
```

---

## Notes for whoever pastes this

- The three case studies are the fastest way back into the design:
  `CASE-STUDY-the-write-path.md` (what runs today, runnable),
  `CASE-STUDY-ambient-capture.md` (what capture would cost, measured on a real
  transcript), `CASE-STUDY-a-day-with-alexandria.md` (operator perspective,
  every claim tagged measured or expected).
- Six load-bearing findings from this session were flushed into the corpus and
  verified retrievable, so `alexandria search` will find them — including the
  burst_id defect and the zero-query audit.
- `docs/BACKLOG.md` carries a verified status table for the Top 10.
