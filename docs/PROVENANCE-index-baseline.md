# Vector-space provenance baseline + cross-writer contract

Status: written 2026-08-23 with the #30 cross-writer integrity audit
(`feat/cross-writer-integrity`, main 6075e7e). Purpose: record the current
live index's provenance, the mechanism that detects a silent vector-space
change, and the complete corpus-writer lock contract, so a future session
can verify "which model built this index" and "who may write when" without
re-deriving it.

## Live baseline (real corpus `~/alexandria-corpus`, read 2026-08-23)

- Provider: `local` (torch, non-MLX), model `Qwen/Qwen3-Embedding-0.6B`
- Dimension: 1024, dtype float32, normalization policy `l2`
- Manifest revision field: EMPTY (the live index predates revision capture;
  this is recorded, not a defect -- see "Same-dimension swap" below)
- Generation: 178 (monotonic cache-freshness stamp, re-read per query)
- Active staged release: `releases/20260821T041317-0cef78b4`
  (activation via atomic temp+replace of `active.json`; prior releases are
  retained, never deleted)

## How provenance is enforced (verified against code)

`src/alexandria/index/manifest.py` treats `provider`, `model`, `revision`,
`dim`, `normalization_policy`, and `dtype` as strict IDENTITY_FIELDS:

- WRITE path: `verify_manifest_for_write` refuses to add vectors to a
  non-empty index whose identity differs from what built it (the guard sits
  where the damage would be done -- an index run overwrites the manifest at
  the end, so a read-path-only guard would pass forever after).
- READ path: `verify_manifest` refuses to serve an index whose identity does
  not match the configured embedder.
- A `--rebuild` drops the table first, so an empty index is legitimately
  re-claimable by any provider; the manifest is then written at the end of
  the run.

**Same-dimension swap detection:** the cache key includes the model name, so
switching providers correctly invalidates cached vectors, and `revision` is
in IDENTITY_FIELDS -- a same-dimension model with a DIFFERENT revision is
refused, not silently mixed. The live corpus's empty `revision` means a
same-dim swap cannot currently be distinguished by revision on the READ side;
the documented procedure is therefore to treat any provider/model change as
a global rebuild (`alexandria index --rebuild`, NO `--enrich`) and re-verify
the manifest identity afterward. An MLX-built index cannot be copied to a
Linux host (different vector space, full re-embed required).

## Cross-writer contract (post-audit, 2026-08-23)

Every corpus MUTATION holds `WriteLock` exclusively -- the invariant
`IndexReadLock` depends on to classify "writer active" vs "at rest". Locked,
with a bounded, loud refusal naming the holder when busy:

| Writer | Where it locks | Mutates |
|---|---|---|
| `index` / `index --rebuild` | cli.py cmd_index | releases + manifest + generation |
| `backfill-manifest` | cli.py (same lock as index) | manifest |
| `promote` / serve drain / `/remember` | promote.py promote_pending | pending + vectors + manifest |
| `delete` / `erase` | cli.py cmd_delete/cmd_erase | frontmatter + stores + history |
| `ingest` | cli.py cmd_ingest | assets + companions |
| `migrate` | cli.py cmd_migrate | vault/sources |
| `restore` | backup.py restore_state (**new 2026-08-23**) | .alexandria state |
| `sync` | cli.py cmd_sync (**new 2026-08-23**) | source docs + connector state |
| `reconcile` | reconcile.py reconcile_inbox (**new 2026-08-23**) | pending markers |
| `cache --clear` | cli.py cmd_cache (**new 2026-08-23**) | query/response caches |

Readers (`search`, `answer`, `lint`) use `IndexReadLock` / re-read the
generation per query; serve answers from the ACTIVE staged release, so a
rebuild's projection is never visible mid-activation.

## Rebuild race behavior (what the fences guarantee)

- A second writer is refused (exit nonzero / loud) while a writer holds the
  lock; `index` waits bounded, others fail fast.
- A reader's `IndexReadLock` is refused while a writer holds `WriteLock`.
- A rebuild publishes via a new staged release; activation is an atomic
  temp+replace of `active.json`, so a reader sees the old or the new
  release, never a partial store.
- The generation bump (178 -> 179 -> ...) invalidates cached query/response
  entries, so a pre-rebuild cached answer cannot replay after reindex.
- Erase additionally holds the lock across tombstone + cache invalidation +
  history rewrite + target reconciliation (its own failure-frame doc).

## Verification

`tests/test_writer_fence.py` (new, 5 tests) pins the four audited writers'
refusal-under-lock and lock-release behavior; each was vacuity-checked to
fail against the pre-fix implementation. Full gates: dev 1118 -> (post-fix
count), CI-like restricted PATH, precommit-scan, git diff --check.
