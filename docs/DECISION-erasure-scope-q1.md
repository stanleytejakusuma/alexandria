# Decision brief: erasure scope (Q1) — core/tail decomposition

**Date:** 2026-08-21
**Status:** awaiting Stanley's decision on the TAIL only (see below) — the CORE
does not require Q1's answer and is proposed for immediate build.
**Corrects:** `docs/SPEC-data-model-and-ambient-capture.md` section 7's claim
that Q1 was "ratified 2026-08-13: tombstone-first." That claim was written by
a prior session's own inference from conversation, not an actual instruction
from Stanley — `docs/RESUME-PROMPT.md` (written earlier the same day, and
its own header says it supersedes all earlier versions) explicitly flags this:
*"THE USER NEVER RATIFIED THIS... treat Q1 as open."* Verified 2026-08-20/21
(this session) that no later document walks that warning back. Q1 is treated
as genuinely open until Stanley answers it here, not assumed either way.

## The question, restated precisely

`docs/SPEC-data-model-and-ambient-capture.md` section 9, Q1: does erasure
stop at the retrievable surface (tombstone — gone from search and synthesis,
still present in git history and the audit trail), or does it need to reach
source files, git history, and the audit trail too (crypto-shred / history
rewrite)?

## Why this decomposes into a core and a tail

Every real gap this decision needs to close is required **regardless of
which answer Q1 gets** — tombstone-first still needs a working tombstone
that actually stops serving, actually purges derived vector/lexical state,
and doesn't get silently un-done by a restore or a poisoned re-enrichment.
None of that depends on whether erasure eventually reaches git history too.
Splitting the work this way means the useful, unambiguous parts can start
now, and Stanley's actual decision is scoped down to the one place it
genuinely branches.

## What ALREADY EXISTS (verified live against main@116b331, do not rebuild)

Soft-delete/tombstoning is further along than the spec's "Untouched" backlog
line suggests — `alexandria delete <doc_id>` (`cli.py:cmd_delete`) already:

1. Writes `deleted: true` into the document's own frontmatter (durable;
   survives any future reindex, since `deleted` is re-derived from disk on
   every `alexandria index`, never resurrected by accident).
2. Reprojects the flag into BOTH physical stores immediately (`mark_deleted`
   on the dense store AND the lexical/BM25 store), keyed by stable `doc_id`
   — not by chunk ids re-derived from possibly-edited current content, so an
   edited-then-deleted document's OLD indexed chunks are correctly tombstoned
   too (a documented prior bug class, already fixed).
3. Enforces the tombstone at READ time unconditionally
   (`not_deleted_clause`, `search.py`) — not an opt-in filter a caller could
   forget to pass. A synthetic (enrichment-hypothetical) row that routes to a
   tombstoned real chunk is also correctly suppressed, not just the direct
   hit.
4. Bumps the corpus generation counter (`write_index_generation`) on every
   delete — which is the SAME mechanism `ResponseCache.key()` already
   includes as a required component (`cache.py:249-262`, verified live). A
   cached answer synthesized before a tombstone is therefore **already**
   invalidated the moment a document is deleted; this genuinely does not
   need to be built.
5. Degrades honestly on partial failure (SOL-03): if the dense flip commits
   but the lexical flip then raises, the generation counter is bumped anyway
   so a stale pre-delete cached result cannot resurrect the document even
   though the two physical stores are momentarily inconsistent.

**Also already built, reusable by an erasure design rather than duplicated:**

- `EnrichmentStore.invalidate(doc_id)` (#5) — force-drops a cached
  enrichment payload independent of content/recipe change. **Not currently
  wired to `cmd_delete`** — a tombstoned document's stale enrichment payload
  is not automatically invalidated today (low severity, since the document
  is already unretrievable, but a future re-enrichment run could reattach a
  payload for a document the operator explicitly marked deleted; cheap to
  wire).
- `citation.doc_id` is present in every durable citation tuple (#9,
  `answers.jsonl`) — a "this document was cited in N past answers" impact
  report is a free join against the existing audit trail, before Stanley
  even answers Q1.
- Staged-release `--gc` (#30 P2a) already retains only active+previous and
  is explicit/dry-run-by-default — the primitive an erasure-driven rebuild
  needs to actually remove a tombstoned document's vectors from disk, not
  just suppress them at query time.

## What is GENUINELY MISSING (the core — proposed for immediate build,
independent of Q1's answer)

1. **A query-time exclusion set, checked before a full rebuild completes.**
   This is the one real gap the existing tombstone does not close: between
   `alexandria delete` (instant, correct) and the NEXT `--rebuild` (which
   actually removes the vectors from disk), the tombstoned document's
   vectors still physically exist in the active release and are only kept
   out of results by the `not_deleted_clause` filter at query time — which
   already works correctly today. The exposure is narrower than an initial
   read might suggest: it is not "erasure doesn't work for 33 minutes," it
   is "physical purge from disk takes until the next rebuild," and
   suppression at the query boundary is already unconditional. Recommend:
   keep this framing precise in any follow-up work — do not over-scope a
   fix for a gap that is smaller than it first looks.
2. **Wire `EnrichmentStore.invalidate()` into `cmd_delete`.** One-line
   addition given the primitive already exists; closes the "stale poisoned
   enrichment survives a tombstone" edge case named above.
3. **A documented erasure-surfaces enumeration**, in the same spirit as
   #8's `KNOWN_CALLERS <= GENUINE_CALLERS` contract test: every module that
   persists doc-derived content (dense store, lexical store, enrichment
   store, response cache, query log, citation tuples) should be explicitly
   listed as either "covered by tombstone today" or "requires its own
   handling" — so a future new persistence surface cannot silently escape
   scope the way #9's citation tuples almost did (caught only because this
   session cross-referenced it manually).
4. **Restore-replay safety** (`backup.py`/`restore_state`): a restore from a
   backup taken BEFORE a tombstone was written would currently resurrect the
   deleted document's frontmatter flag along with everything else in that
   backup — restore replaces state, it does not merge against a
   newer-than-the-backup tombstone. Needs either an explicit warning when
   restoring a backup older than the most recent delete, or a documented
   operator responsibility ("restore, then re-apply any deletes made after
   the backup was taken").
5. **Legacy-manifest special case: verified NOT an issue for delete.**
   `mark_deleted` (`store.py`) is a raw table `.update()` call with its OWN
   independent schema check (fails loudly if the `deleted` column predates
   the table) — it never routes through `verify_manifest_for_write` or the
   `allow_unverified_legacy` gate at all (confirmed live: that guard is only
   called from `_guarded_write_embedder`, reached from `cmd_index`, not
   `cmd_delete`). A tombstone write on a legacy-manifest corpus works today
   without forcing a rebuild first. No action needed here — listed to show
   the question was checked, not assumed.

## What is GENUINELY THE TAIL — this is Stanley's actual decision

Everything above is proposed to ship regardless of the answer below. What
remains open, and does branch on Stanley's answer:

- **Does erasure need to reach the audit trail** (`queries.sqlite`,
  `answers.jsonl`) — i.e., can a citation tuple naming a since-deleted
  document's `doc_id` remain in the durable, no-TTL audit log forever, or
  must it eventually be purged/redacted too?
- **Does erasure need to reach git history** — i.e., can a deleted
  document's full prior content remain recoverable via `git log`/`git show`
  on old commits forever, or must history be rewritten?
- **Does erasure need to reach `sources/*.md` itself** beyond the
  frontmatter flag — i.e., is `deleted: true` with the body still physically
  present on disk (current behavior) acceptable, or must the body content
  itself be scrubbed/overwritten?
- **Does erasure need to reach backup archives** (`.alexandria/`, per
  `backup.py`) — old backups taken before a deletion currently retain the
  pre-deletion state indefinitely.

## The real cost of the "reaches everything" tail option, named honestly

If Stanley's answer requires history rewriting or crypto-shredding: this
repo's own verification discipline is built on SHA-anchored references —
every commit message in this session's arc, every backlog row citing a
specific commit, `docs/THREAT-MODEL.md`'s own "verified against live code at
commit X" claims, and the leak-scanner's full-history scan just run this
session (`gitleaks`, all 265 commits, zero leaks, cited by SHA-adjacent
methodology) — all of this becomes stale or invalid the moment history is
rewritten. That is not a reason to rule out the tail option; it is a real,
concrete cost that should be weighed explicitly rather than discovered after
the fact.

## Recommended sequencing

1. Stanley answers the TAIL question above (audit trail / git history /
   source body / backups — any subset, does not need to be all-or-nothing).
2. The CORE (items 1-4 above -- item 5 needed no action, verified clean)
   proceeds now, independent of that answer,
   test-first, with an explicit pre-code failure-frame note per item (what
   breaks under `serve`'s shared engine instance, what breaks at a second
   call site, the cheapest attacker move, what a mid-operation crash leaves
   behind) handed to the independent review alongside the diff — this
   session's own pattern of first-pass gaps (three separate items this
   session broke under `serve`'s shared-instance shape specifically) is
   treated as a known, preventable failure class going into this work, not
   repeated by default.
3. Once Stanley's tail answer is known, the remaining scope (if any) is
   built as its own follow-up, sized by what was actually decided.
