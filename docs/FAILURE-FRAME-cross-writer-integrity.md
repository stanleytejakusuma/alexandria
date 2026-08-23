# FAILURE-FRAME — cross-writer integrity audit + rebuild-race fencing

Status: pre-code note for `feat/cross-writer-integrity` (2026-08-23), written
before edits resume, per this repo's work-order discipline. Scope: session
todo #30 (Red audit residue) -- close the rebuild/cross-writer integrity gaps
found in the audit, prove every writer honors the corpus write lock, and pin
rebuild-race behavior with tests.

## Audit findings (verified against main 6075e7e, 2026-08-23)

Every corpus MUTATION must hold WriteLock exclusively -- that is the invariant
`IndexReadLock` (used by lint and every reader) depends on to classify
"writer active" vs "at rest" (Red review, 2026-08-20). Locked today: `index`,
`backfill-manifest`, `promote` (promote.py:83), `delete`, `erase`, `ingest`,
`migrate`, and serve's `/remember` (via promote_pending). NOT locked:

1. `restore` (`backup.restore_state`) -- overwrites `.alexandria` state
   (queries.sqlite, audit, eval_runs, pending, liveness, generation.json) in
   place with NO write lock. A restore racing an index/promote/erase can
   clobber generation.json or pending state mid-write.
2. `sync` (`cmd_sync`) -- connector distillation calls `doc.write(corpus)`
   (writes corpus SOURCE documents) plus state writes, with NO write lock.
   A sync racing an index can mutate a source file mid-read; a sync racing an
   erase violates the clean-tree preflight premise.
3. `reconcile` (`reconcile.reconcile_inbox`) -- rewrites pending state with
   NO write lock; races promote's pending read/discover under its lock.
4. `cache --clear` (`cmd_cache`) -- `QueryCache.clear() + ResponseCache
   .clear()` DELETE rows with NO write lock; races serve's per-request cache
   reads/writes (semantic race, even though SQLite WAL+busy_timeout makes the
   DELETE itself DB-safe).

## 1. Shared-serve behavior

`serve` reads with `IndexReadLock` and writes audit/caches per request.
`cache --clear` mutates the same query/response cache tables serve reads and
writes every request; `restore` overwrites state files `serve` reads
(generation.json is re-read per query); `sync`/`reconcile` mutate content and
pending state that the drain (`promote_pending`) consumes. All four must take
the corpus write lock (bounded, fail loudly) so a running daemon can never
observe a mid-mutation state or a cache clear racing an entry write.

## 2. Second call site

`restore_state` and `reconcile_inbox` are also library functions called
directly by tests and (potentially) other harnesses. The lock must be taken
inside the DOMAIN function (like promote.py:83), not in the CLI wrapper, so
every call site is fenced. `cmd_sync`'s write phase is inline in cli.py --
the lock goes around the connector distillation loop. `cmd_cache --clear`
takes the lock in the CLI (the cache classes are thin, single-purpose).

## 3. Operator / attacker error

Cheapest operator error: running `restore` or `sync` while the weekly loop /
serve drain is mid-write. Mitigation: bounded lock acquisition with a loud,
actionable refusal naming the holder (the same message pattern index/ingest
use). Cheapest attacker move: none new -- these are local CLI operations;
the lock refusal is the guard, and `restore`'s existing allowlist already
rejects hostile archives.

## 4. Crash state

All four fixes are lock-then-mutate: a crash mid-operation leaves the lock
auto-released by the kernel (flock) and the previous partial-state semantics
unchanged (restore already overwrites in place; sync/reconcile/cache --clear
are idempotent or re-runnable). The generation counter's monotonic restore
guard (already shipped) is untouched. No new durable state is introduced.

## 5. Exact verification (before commit, on feat/cross-writer-integrity)

1. Focused: unset ALEXANDRIA_EMBED_PROVIDER && PYTHONPATH=src .venv/bin/python -m pytest tests/test_backup.py tests/test_sync_commit_semantics.py tests/test_reconcile.py tests/test_caches.py tests/test_writelock.py tests/test_index_write_lock.py tests/test_promote.py tests/test_releases.py -q
2. Dev full: unset ALEXANDRIA_EMBED_PROVIDER && PYTHONPATH=src .venv/bin/python -m pytest tests/ -q
3. CI-like: HF_HUB_OFFLINE=1 HF_HOME=$(mktemp -d) PATH=/usr/bin:/bin PYTHONPATH=src .venv/bin/python -m pytest tests/ -q
4. scripts/precommit-scan.py --all
5. git diff --check
6. Vacuity: each new writer-fence test must fail against the pre-fix
   implementation (lock removed or never acquired).
