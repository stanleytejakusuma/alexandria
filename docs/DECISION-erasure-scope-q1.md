# Decision brief: erasure scope (Q1) — core/tail decomposition

**Date:** 2026-08-21
**Status:** DECIDED 2026-08-21. Stanley's answer, verbatim in substance: keep
the retrievable-surface tombstone as the default (matches "the rest is fine"
below), AND also erase the raw document text from git history -- explicitly
NOT the audit trail, NOT backups beyond their existing retention, source body
text specifically. Sequencing: the CORE below ships FIRST; git-history
erasure ships as its own separate, deliberate operation afterward (Stanley's
explicit instruction), not bundled into `alexandria delete`.
**Corrects:** `docs/SPEC-data-model-and-ambient-capture.md` section 7's claim
that Q1 was "ratified 2026-08-13: tombstone-first." That claim was written by
a prior session's own inference from conversation, not an actual instruction
from Stanley — `docs/RESUME-PROMPT.md` (written earlier the same day, and
its own header says it supersedes all earlier versions) explicitly flags this:
*"THE USER NEVER RATIFIED THIS... treat Q1 as open."* Verified 2026-08-20/21
(this session) that no later document walks that warning back. Q1 is treated
as genuinely open until Stanley answers it here, not assumed either way.

## The question, restated precisely (now answered — see "What was decided" below)

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

## What was decided (formerly "the tail") — RATIFIED 2026-08-21

Stanley's answer, recorded here so no future session has to re-derive it or
risk a second false-ratification incident like the one this document opened
by correcting:

- **Audit trail** (`queries.sqlite`, `answers.jsonl`): **stays**. A citation
  tuple naming a since-deleted document's `doc_id` remains in the durable,
  no-TTL audit log. Not erased.
- **Backups**: **stays**, no change to existing retention behavior beyond
  what the CORE items below already require (restore-replay safety).
- **Git history**: **erase**. The raw document body text must actually be
  removed from every commit that ever carried it, not just the current
  working-tree state. This is the one item that branches from "tombstone
  only" and needs its own deliberate operation (see "Sequencing" below).
- **Source body on disk (working tree)**: covered by "git history" above --
  once history is rewritten, the working tree naturally reflects that too.
  No separate handling needed.

## The real cost of the git-history-erasure decision, named honestly (accepted, not a warning against it)

Stanley has accepted this cost as part of the decision. Recorded here for
the erasure implementation's own awareness, not as an open objection: this
repo's own verification discipline is built on SHA-anchored references —
every commit message in this session's arc, every backlog row citing a
specific commit, `docs/THREAT-MODEL.md`'s own "verified against live code at
commit X" claims, and the leak-scanner's full-history scan just run this
session (`gitleaks`, all 265 commits, zero leaks, cited by SHA-adjacent
methodology) — all of this becomes stale or invalid the moment history is
rewritten. That is not a reason to rule out the tail option; it is a real,
concrete cost that should be weighed explicitly rather than discovered after
the fact.

## Sequencing — RATIFIED, execution order

1. **The CORE (items 1-4 above -- item 5 needed no action, verified clean)
   ships first.** Test-first, with an explicit pre-code failure-frame note
   per item (what breaks under `serve`'s shared engine instance, what
   breaks at a second call site, the cheapest attacker move, what a
   mid-operation crash leaves behind) handed to independent review
   alongside the diff -- this session's own pattern of first-pass gaps
   (three separate items this session broke under `serve`'s shared-instance
   shape specifically) is treated as a known, preventable failure class
   going into this work, not repeated by default.
2. **Git-history erasure ships as its OWN separate, deliberate operation
   afterward** -- Stanley's explicit instruction. Not bundled into
   `alexandria delete`, not automatic. Proposed shape (not yet built,
   scoped as its own follow-up item once the core lands): a new
   `alexandria erase <doc_id>` verb, distinct from `delete` -- `delete`
   stays instant/safe/reversible (undelete exists today); `erase` is the
   rare, deliberate, genuinely-irreversible action. It reuses the tombstone
   `delete` already does, then rewrites the corpus git repo's history for
   that file's path only (via `git-filter-repo`, already installed on this
   machine, the modern maintained tool -- not BFG, which is unmaintained),
   then purges whatever the CORE's erasure-surfaces enumeration says needs
   handling beyond the tombstone. Must refuse to run silently: print the
   exact file path and the number of commits it will rewrite, require
   explicit confirmation, matching the confirmation-gate pattern already
   used elsewhere in this codebase (`--enrich-invalidate`, the real-corpus
   `remember` write guard).

   **Sequencing invariant within `erase` itself** (Red review 2026-08-21,
   pinned now for a deliverable not yet built): the CachedEmbedder
   embedding cache is content-addressed (`sha256(model_name + text)`), so
   it can only be purged of a document's rows while the document's TEXT
   still exists to recompute the key from. `erase` must therefore clean
   caches (embedding cache, and anything else content-addressed the
   erasure-surfaces enumeration later finds) BEFORE scrubbing git history
   -- destroying the last copy of the text first would make those cache
   rows permanently unaddressable (findable only by full-cache scan, not
   by key), defeating the purpose of a deliberate erasure operation.


## `alexandria erase` -- SHIPPED (2026-08-21)

Built per the sequencing above, on `feat/erase-verb`, with a full pre-code
failure-frame note written first (what breaks under `serve`'s shared
instance -- N/A, `erase` has no HTTP route; what breaks at a second call
site -- the corpus write lock must be held for the WHOLE operation, not
just the tombstone half; the cheapest operator-error move -- erasing the
wrong doc_id, mitigated by a `--yes` confirmation gate that prints the
exact blast radius first; what a mid-operation crash leaves behind -- the
critical case, since `git filter-repo` deletes the original history
immediately on success with no separate commit step).

**Shape delivered**: `src/alexandria/erasure.py` (`erase_from_git_history`,
`impact_report`, `GitEraseError`) + `cmd_erase` in `cli.py` + `tests/
test_erasure.py` (12 tests) + CLI integration tests in `test_soft_delete.py`
(6 tests, including a dedicated regression test for a real bug found live
during development, see below).

**Safety design, verified live via three deliberate failure-injection
tests, not just asserted in prose**:
1. Never operates on the corpus repo directly -- always clones
   (`git clone --no-local`) to a disposable tmp dir, rewrites the CLONE,
   validates the rewrite (`git log --all -- <path>` returns zero commits
   in the clone), and only then atomically swaps the corpus's `.git`
   directory for the clone's rewritten one.
2. A failure at any point BEFORE the swap leaves the corpus's `.git`
   completely untouched -- proven by monkeypatching `filter-repo` to fail
   and asserting the corpus's HEAD/log are byte-identical before and after.
3. A crash DURING the swap (the narrowest window: renaming the original
   `.git` aside, then renaming the rewritten one into place) is recovered
   from immediately -- proven by monkeypatching the second `rename` call to
   raise and asserting the original `.git` is restored, not left absent.
4. The pre-erase `.git` is never deleted, only renamed aside to
   `.git.pre-erase-<path>` (mirrors #30 P2a's "never delete the previous
   release" retention idiom) -- a manual recovery path, not something the
   command manages long-term.
5. The write lock is held across the WHOLE operation (tombstone + cache
   purge + git rewrite) as a single critical section -- `cmd_delete`
   gained an internal `_held_lock` parameter so `cmd_erase` can pass its
   own already-acquired lock through instead of `cmd_delete` acquiring and
   releasing its own partway through. Proven by a dedicated test that
   spies on the git-rewrite step and asserts a second, independent lock
   acquisition attempt is refused for the full duration.

**A real bug found and fixed during development, not just in review**: the
first working version synced the corpus's working tree to the rewritten
history using `git reset --hard HEAD && git clean -fd` at the corpus root.
`git clean -fd` removes ALL untracked content -- and `.alexandria/` (the
index, embedding cache, and audit trail) is DELIBERATELY untracked corpus
state, exactly like this repo's own "corpus is not this repo" doctrine.
An end-to-end test caught this directly: after erasing one document, the
corpus's entire search index and embedding cache were gone, not just the
erased document. Fixed by never running a repo-root-wide clean at all --
the working-tree sync now targets ONLY the one known erased path directly
(checks whether it is still git-tracked; if not, removes just that file),
plus a `git checkout HEAD -- .` for ordinarily-tracked paths. A dedicated
regression test (`test_cli_erase_never_touches_the_untracked_alexandria_
state_directory`) pins this permanently, vacuity-checked against the
original buggy code (fails on it, passes on the fix).

**A second real edge case found live**: if the erased document was the
ONLY content in the ONLY commit, `filter-repo` correctly prunes that
now-empty commit entirely, leaving the rewritten history with ZERO commits
and no resolvable `HEAD`. `git reset`/`checkout HEAD` then fail (nothing to
reset to), not because anything went wrong. Detected via `git rev-parse
--verify HEAD` first, handled distinctly (skips the checkout/reset step
entirely in that case; a dedicated test, `test_erasing_the_only_document_
leaves_a_valid_empty_but_functional_repo`, covers it and confirms a fresh
commit works fine against the now-empty history afterward).

**External dependency**: `git-filter-repo` (already installed on this
machine per the original decision doc; also available via `pip install
git-filter-repo` or `brew install git-filter-repo`). Treated exactly like
`pdftotext` for PDF ingest -- an optional external binary, absent →
clean, actionable refusal naming both install paths (verified live:
without it, `erase_from_git_history` raises `GitEraseError` with the
install instructions, not an opaque subprocess error). Tests that exercise
the real binary are `pytest.mark.skipif`-gated on `shutil.which
("git-filter-repo")`, matching `test_ingest_refresh.py`'s
`requires_pdftotext` precedent exactly; tests that only need
`subprocess.run` mocked (rollback-safety tests) do not require the real
binary and always run.

**Free citation-linkage integration**: `impact_report()` reuses #9's
durable citation tuples (`answers.jsonl`) to show an operator which past
answers cited the document before they confirm the erase -- read-only,
informational, since the audit trail itself is explicitly NOT touched by
this command per the ratified decision above.

Full suite: 1063 → 1085 (dev), 1005+31 → 1045+40 (CI-like restricted PATH,
the +9 skip delta being the git-filter-repo-gated tests). `precommit-scan.py
--all` clean.
