# FAILURE-FRAME — `alexandria erase` git-history erasure (Red-remediation)

Status: remediation in progress on `feat/erase-verb` (2026-08-22). This note
is the pre-code failure frame for the Red-review remediation batch, covering
every failure class the original review demanded be examined before edits
resume. It accompanies the Red findings (REJECT/BLOCK on `39d5b39`) and the
fix plan in todos #61-#79.

## 1. Shared-serve behavior

`serve` never exposes an erase route, so there is no HTTP path to race.
What IS shared is the live corpus working tree and the deployed index:

- A running `serve` daemon holds the index open and answers `/search` and
  `/answer` from `.alexandria/releases/<active>/`. `erase` rewrites Git
  history and then synchronizes the working tree; it must never delete or
  corrupt `.alexandria/` state (index releases, embedding cache, audit
  trail), which is untracked operational state by design.
- The corpus write lock (`writelock.py`) serializes every corpus writer
  (index, promote, remember, delete). `cmd_erase` must acquire it before
  authoritative document resolution and hold it through tombstone, cache
  invalidation, history rewrite, and target synchronization. The lock does
  NOT serialize `serve`'s read path (`/search`), which is expected: a
  tombstoned document is unretrievable because the `deleted` flag is
  enforced at hydration, and the embedding cache is only ever read for
  exact content-addressed keys.
- Failure mode to prevent: `erase` running while a concurrent `index`/
  `promote`/second-`erase` projects or promotes a stale generation, or
  while `serve` mid-flight-answers a query whose evidence includes the
  erased doc. Mitigation: whole-operation lock + tombstone-before-rewrite
  + generation bump on tombstone.

## 2. Second call site

`erase_from_git_history()` and `preflight_git_erase()` are also public
library calls (tested directly in `tests/test_erasure.py`). A direct
caller can pass an inconsistent `preflight`, skip the lock, or call
`recover_interrupted_erase()` concurrently with a live erase. Contract:

- Direct callers without a supplied preflight get a complete clean-state
  preflight that refuses tracked/staged changes.
- `cmd_erase` supplies a lock-captured preflight and explicitly passes
  `allow_target_dirty=True` so the intentional tombstone write is the only
  tolerated worktree change; any other tracked change still fails closed.
- `recover_interrupted_erase()` is idempotent and marker-gated: without a
  durable marker it does nothing; with one it either restores the original
  `.git` or completes only the target-path reconciliation.
- All three functions must keep working on a symbolic unborn branch with
  no refs (never-committed source), returning an honest zero rather than
  failing on `git log` against an empty repository.

## 3. Operator / attacker error

Cheapest operator error: erasing the wrong doc_id. Mitigations: `--yes`
confirmation prints exact path + commit count + citation count first;
preflight resolves the path under the lock before any mutation; a
never-committed path is removed only after confirmation.

Cheapest attacker move: pointing `erase` at a path outside `sources/`/
`wiki/` (path traversal), or at a corpus whose `.git` is a symlink or a
linked worktree. Mitigations: `_normalise_rel_path` rejects absolute/`..`
paths; `_doc_path_for` rejects non-indexable ids; `_check_supported_repo_shape`
requires a standalone top-level `.git` directory, rejects linked worktrees,
remotes, extra refs, custom hooks, active Git operations, and a TRACKED
`.alexandria` state directory; every staging/backup path must be on the
same device (`st_dev`) as the live `.git`.

Attacker move 2: pre-staging a symlink/plant inside `.alexandria/erase-staging`
or `erase-backups` to make the transaction write outside the corpus.
Mitigations: staging uses fresh `mkdtemp` under the validated root; backup
generations are UUID-named and refuse to overwrite an existing destination;
marker recovery validates the backup path is exactly
`.alexandria/erase-backups/<generation>/git`.

## 4. Crash state (mid-operation)

The directory cutover is two renames; they cannot be crash-atomic. A
durable marker (`corpus/.alexandria/erase-txn.json`, fsynced) records the
phase before the first rename:

- `prepared` — original `.git` still active, mirror ready. Recovery:
  remove marker (and empty backup parent), nothing else.
- `original-moved` — original `.git` renamed aside. Recovery: if no
  `.git` exists, rename the retained backup back; if `.git` exists, the
  rewrite already installed, so reconcile.
- `swapped` / `new_git_installed_needs_target_reconcile` — rewritten
  `.git` is active. Recovery performs ONLY target-path reconciliation:
  rebuild the index from rewritten HEAD (no broad checkout), verify the
  target is untracked (`ls-files --error-unmatch` exit 1), and unlink the
  erased target. Must also handle the rewritten repo having ZERO commits
  (erasing the only document): `rev-parse HEAD` fails with 128, there is
  no index to rebuild from, and only the target unlink applies.

Crash before the marker is written: no mutation happened (marker write
precedes the first rename). Crash after marker removal: terminal state
already synchronized. Marker removal must happen last, after fsync of the
final state.

The retained backup `.alexandria/erase-backups/<generation>/git` is the
manual-recovery copy of the pre-erase repository and intentionally retains
raw content per ratified policy; it is never overwritten or deleted by the
tool.

## 5. Exact verification (before commit, on `feat/erase-verb`)

1. Focused: `unset ALEXANDRIA_EMBED_PROVIDER && PYTHONPATH=src .venv/bin/python -m pytest tests/test_erasure.py tests/test_soft_delete.py tests/test_embedder.py -q`
2. Dev full suite: `unset ALEXANDRIA_EMBED_PROVIDER && PYTHONPATH=src .venv/bin/python -m pytest tests/ -q`
3. CI-like: `HF_HUB_OFFLINE=1 HF_HOME=$(mktemp -d) PATH=/usr/bin:/bin PYTHONPATH=src .venv/bin/python -m pytest tests/ -q`
4. `scripts/precommit-scan.py --all`
5. `git diff --check`
6. Mutation/vacuity checks: each new acceptance test must fail against a
   counterexample implementation (e.g. no lock, targeted purge instead of
   purge_all, missing marker phase, rollback that broad-checkouts).
7. Second Red review with the evidence package (todo #75), and merge only
   on an acceptable verdict.


## Round-2 review closure (2026-08-22)

The second Red review (round 2 of 2) returned REJECT (escalate) with three
findings, all now fixed and tested in the amended commit `f81fe9f`:

1. **Crash-window tests between a rename and its marker transition.** The
   reviewer asked that recovery branch on observed filesystem state, not on
   completed-phase marker values alone. The implementation already did so
   (the missing-live-`.git` branch restores the retained backup regardless
   of phase; a live `.git` whose history no longer contains the path is
   reconciled regardless of phase), and two new tests pin it:
   `test_recovery_crash_after_first_rename_but_before_marker_advance`
   (marker still `prepared`, `.git` already moved: restore) and
   `test_recovery_crash_after_second_rename_but_before_marker_advance`
   (marker still `original-moved`, rewritten `.git` installed: reconcile
   only the target).
2. **Pre-swap rewrite failure must restore retryability.** A failed rewrite
   with `history_changed=False` now rolls the tombstone back
   (`_rollback_tombstone_after_failed_erase`: file bytes restored from
   `HEAD:<path>`, index rows un-flagged, generation bumped) so the corpus is
   unchanged and a `--yes` retry passes the clean-state preflight.
   `test_cli_erase_pre_swap_failure_rolls_back_the_tombstone_for_retry`
   proves the rollback, re-searchability, and a successful real retry.
3. **`cmd_erase` must fail closed unless the cache purge actually cleared.**
   `CachedEmbedder.cache_row_count()` (public) is read before and after
   `purge_all()`; if durable rows remain, `cmd_erase` rolls the tombstone
   back and aborts BEFORE history is rewritten.
   `test_cli_erase_fails_closed_when_cache_invalidation_does_not_clear`
   proves history is untouched and the corpus is unchanged.

Round-2 verification: dev full suite 1118 passed / 0 skipped; CI-like
restricted PATH 1070 passed / 48 skipped (filter-repo-gated locally, always
installed in CI); `precommit-scan.py --all` clean; `git diff --check` clean.

Per the review doctrine (max two rounds per artifact), both verdicts are
escalated to Stanley with this closure evidence for the merge decision.
