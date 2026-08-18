**Repo:** `~/codebase/alexandria` · **Branch:** `docs/rebuild-atomic-work-order`, not `main`
**Venv:** `.venv` (`.venv/bin/python`, never system python) · Python 3.12
**Baseline:** `main` at 755 passing tests. Do not regress it.

# Work order: atomic rebuilds and cross-host release integrity

## 0. Why this exists / why it is deliberately phased

This is a correctness and operational-integrity work order, not a request to
rewrite the indexing pipeline in one patch.  It responds to a red audit of the
current rebuild and deployment boundary.

There are **two distinct problems**, and they must not be conflated:

1. **The already-fixed local writer race.** `cmd_index` now takes the same
   host-local `WriteLock` as `promote_pending`; the serve drain honors that
   lock. `tests/test_index_write_lock.py` deterministically proves the former
   index-versus-promote loss interleaving cannot run.  Do not remove, weaken,
   duplicate, or re-describe this as a currently-open in-engine writer race.
   This work order builds on that fix.
2. **Rebuild reader exposure and file-transfer integrity.** Today
   `index --rebuild` writes a marker, drops the live dense and FTS artifacts,
   refills those physical legs independently, then writes the generation and
   manifest only after success.  A normal serve read does not honor the marker,
   so it can read an empty, partial, or cross-leg-incoherent live index. A
   crash leaves that partial index plus its marker; only `eval` presently
   refuses the marked state. Separately, a host-local `flock` cannot protect an
   external copier. It says nothing about a second machine copying files while
   they change.

The audit also found two independently materialized, live index roots. Their
provider, model, and dimension agree, but one manifest reports
`normalized: false` and the other `normalized: true`. These are different
vector-space identities under the existing six-field manifest contract; the
common provider/model/dimension triple is not compatibility evidence. A server
and a scheduled writer are both active. There is no evidence of an active
cross-host copy at the time of the audit, but the public migration work order
currently directs a raw transfer of corpus, index, and embedding cache and has
no sealed-release or integrity protocol.

A successful rebuild already retains durable soft-delete tombstones because
`deleted: true` comes from source frontmatter and is reprojected into both
legs. The replacement design must preserve, validate, and test that property;
it must never turn a tombstone into a live chunk while moving releases.

The safe delivery order is intentionally:

- **P0 — operational fence:** establish one authoritative deployment and stop
  documenting raw live-index copies.
- **P1 — admission safety:** a marked rebuild is unavailable to every normal
  reader, including an already-running server.
- **P2 — availability and atomicity:** construct a sealed, validated generation
  beside the serving one and atomically move a reader pointer. This removes the
  P1 rebuild outage for compatible releases.
- **P3 — transfer:** move only an explicitly sealed release and its declared
  source snapshot through a verify-before-activate import protocol.

P0 and P1 are prerequisites for production rebuilds. P2 and P3 are separate,
reviewable implementation phases. This document is authored on its docs branch;
dispatch P0, P1, P2, and P3 as separate implementation branches/reviews from a
then-current `main`, each with a phase-scoped commit series and green suite. Do
not promise P0–P3 in one patch, and do not enable a remote/cross-host index
transfer until P3 is complete.

## 1. Where things live and the source-of-truth contract

- Engine code and public, non-sensitive operational policy live in this repo.
- The corpus is a separate private repository. Its source documents,
  frontmatter, inbox, and durable tombstones are the source of truth. Dense,
  FTS, query/response, and embedding artifacts are derived state; an index is
  never the canonical store.
- Private deployment facts — hostnames, codenames, addresses, usernames,
  tunnel details, key paths, key identifiers, tokens, service names, and
  exact production commands — belong only in the private companion runbook,
  never in this engine repository, test fixture, commit message, or example.
- There may be exactly one **authoritative deployment** for a corpus authority
  epoch. It alone owns the writable corpus, active-release pointer, serve
  process, scheduled sync/index/promote jobs, and release signing authority.
  Other installations are read-only clients or explicitly imported replicas;
  they must not run a weekly writer or mutate an active release. The private
  deployment configuration must enforce that distinction: only the authority
  service account may own the writable corpus/release tree and scheduled
  writer units; replica/client credentials are read-only and no public engine
  option bypasses those filesystem/service boundaries.

“Authoritative” is an operational provenance designation, not a distributed
lock and not an authentication mechanism. The authority epoch and opaque
source-snapshot/release identifiers make a transfer auditable without putting
an identifying machine label in the engine repo. The private runbook records
which real deployment holds that role and the private permission evidence that
makes the policy mechanically effective.

## 2. What already exists — call it; do not rebuild it

Read these modules and tests before changing code:

- `src/alexandria/writelock.py`: local-filesystem check and shared
  host-local `flock`. `cmd_index` takes its bounded exclusive lock; periodic
  promotion/drain takes the same lock non-blockingly.
- `src/alexandria/cli.py`: `_cmd_index_locked()` implements the current
  in-place `--rebuild`; `_rebuild_marker()` is the legacy marker; `cmd_eval()`
  is the only current consumer that refuses it.
- `src/alexandria/promote.py`: ordered, idempotent promotion with pending
  markers as the redo log. It is not to be bypassed during a rebuild.
- `src/alexandria/serve.py`: one startup `SearchEngine`, live `/search`,
  `/answer`, `/remember`, and `/health` paths, plus the drain.
- `src/alexandria/index/manifest.py`: the authoritative six-field embedding
  identity (`provider`, `model`, `revision`, `dim`, `normalized`, `dtype`) and
  loud mismatch behavior.
- `src/alexandria/cache.py`: generation-keyed query/response caches. Cache
  invalidation must follow an activated release, never a partially staged one.
- `src/alexandria/index/store.py`, `index/bm25.py`, and `index/chunker.py`:
  the dense/FTS physical legs and `deleted` projection semantics.
- `tests/test_index_write_lock.py`, `tests/test_serve.py`,
  `tests/test_manifest.py`, `tests/test_promote.py`, `tests/test_soft_delete.py`,
  and `tests/test_cli.py`: extend these established test styles instead of
  creating a parallel fake index stack.
- `docs/WORK-ORDER-migrate-serve-to-nas.md` and
  `docs/DECISION-serve-host-remote-2026-08-15.md`: deployment intent. The
  raw-copy language in the former is superseded by P0/P3 below; do not preserve
  it as an alternative “quick” path.

The existing write lock protects cooperating processes on **one local
filesystem**. It is not a reader gate today and it cannot make a file copier
safe. The current marker is evidence of an unsafe/partial rebuild; it is not a
lock, a health protocol, or an atomic-swap mechanism.

## 3. Target shape and non-negotiable invariants

### 3.1 Invariants after all phases

1. A normal reader either uses one complete, validated release or receives an
   explicit unavailable response. It never searches an in-place rebuild. In P1
   a physical read owns a shared lease for its complete index-access lifetime;
   in P2 it owns an immutable release lease for that request. A writer cannot
   mutate physical artifacts until P1 leases have drained.
2. A release is immutable after sealing. Its vector artifact, FTS artifact,
   manifest, validation record, source-snapshot declaration, and artifact
   checksums describe the same build.
3. Pointer activation is a local atomic replace on one filesystem. It occurs
   only after candidate validation; a failure before activation preserves the
   prior reader-visible release byte-for-byte.
4. Every writer and in-place mutator (including `promote_pending`, import,
   layout migration, recovery, and GC) continues to participate in the common
   lock protocol on stable, never-replaced lock inodes in one local filesystem
   kernel domain. P2 must resolve the active release *after* obtaining that
   lock, so a queued promotion cannot write into a release that has just ceased
   to be current. If any actor runs outside that lock domain, local `flock` is
   insufficient and the operation is prohibited pending shared coordination.
5. Any manifest identity difference — including normalization — is a hard
   compatibility boundary. It never results in mixed vectors or silently
   changed query embeddings.
6. A rollback is another validated pointer activation, not a directory copy.
   It creates a new cache-activation epoch so no response cache entry is
   accidentally treated as fresh merely because an old release id returned.
7. Durable tombstones survive rebuild, export, import, activation, and rollback;
   both legs continue to enforce them fail-closed.
8. No command imports, follows, or serves a release with a missing/corrupt
   control record, an unsealed candidate, a hash mismatch, a source-snapshot
   mismatch, a manifest mismatch, or an untrusted signature. It fails closed
   before changing the active pointer.

### 3.2 P2 on-disk model

Do not make `current` a symlink. Use a small atomically replaced JSON control
file so its schema, checksum, authority epoch, and activation/cache epoch are
explicit and portable.

```text
.alexandria/index/
  active.json                         # atomically replaced reader pointer
  releases/<release-id>/
    vector/                            # VectorStore root (including Lance data)
    fts.sqlite
    manifest.json
    release.json                       # canonical metadata and artifact digests
    SEALED                             # written last, records release.json digest
  staging/<release-id>/                # never reader-visible; same-FS only
  incoming/<transfer-id>/              # P3 extraction/verification only
  .write.lock
  rebuild-state.json                   # P1/P2 admission state, outside releases
```

`release.json` is canonical JSON with a format version and at least:
`release_id`, immutable parent/source release id where applicable,
`authority_epoch`, `created_at`, engine/index schema and validation-tool
versions, all six manifest identity fields, expected dense/FTS modes and
schemas, `source_snapshot` (algorithm, digest, document count, chunk-id-set
digest, and tombstone count), the candidate’s immutable release generation,
and a complete object manifest. The object manifest lists every exported
release-relative object’s safe path, type, byte size, and digest, including the
dense and FTS artifacts; validation results bind those physical legs to one
source/tombstone snapshot. The `SEALED` record is the digest of these canonical
metadata bytes. It contains no hostname, address, key material, corpus content,
or human codename.

`active.json` contains only a schema version, `release_id`, the release digest,
and a monotonically increasing `activation_epoch`. A reader must:

1. read and parse `active.json` once;
2. open that exact sealed release directory;
3. verify the pointer’s recorded release digest against its `release.json`;
4. acquire a stable per-release shared lifecycle pin before releasing the
   lifecycle lock, then pin an `IndexLease` to that immutable release for the
   full request.

`IndexRepository` protects pointer-read-plus-pin and release deletion with a
stable lifecycle lock: a request takes it shared while resolving/pinning;
pruning takes it exclusive and skips, rather than unlinks, a pinned release.
If any step fails, the reader returns a named unavailable/error state; it must
not scan a directory for “the newest” release and must not fall back to an
unverified candidate. `os.replace()` of a fully fsynced temporary `active.json`
(and fsync of its parent directory where supported) is the only activation or
rollback operation.

### 3.3 Reader and writer APIs to introduce

Add `src/alexandria/index/releases.py` with small, testable domain APIs rather
than scattering path construction through CLI and serve:

- `read_rebuild_state(corpus) -> RebuildState | None`
- `write_rebuild_state(...)` / `clear_rebuild_state(...)`, using atomic JSON
  writes; these are called only while the exclusive writer lease is held.
- `IndexRepository(corpus)`: `open_active() -> IndexLease`,
  `stage_rebuild(...) -> StagedRelease`, `validate(staged) -> ValidationResult`,
  `activate(release_id, expected_identity, expected_active_digest) -> ActiveRelease`,
  `rollback(release_id, expected_identity, expected_active_digest) -> ActiveRelease`,
  and `prune(...)`. Activation/rollback compare the observed pointer digest
  with `expected_active_digest` under the writer lock; a changed pointer is a
  conflict, never an implicit last-writer-wins replacement.
- `IndexLease`: owns the release id, cache activation epoch, VectorStore, BM25
  index, and a release/pin lifetime. Its public search-engine adapter is
  request-scoped; it must not expose a mutable “current path.”
- `IndexReaderManager`: serves concurrent request leases, notices a changed
  `active.json` between requests, and retains old handles/directories until
  their leases drain. Its lifecycle lock makes pointer-read-plus-per-release
  shared-pin atomic with respect to GC; pruning takes the exclusive lifecycle
  pin and skips (never unlinks) a pinned release. It must bound cache/handle
  growth and never delete the active, previous, pinned, or unverified release
  automatically.
- `ActiveIndexWriter`: resolves `active.json` only after `WriteLock` is held,
  opens that exact immutable active release for `promote_pending`/other
  incremental writers, and refuses if it changed before commit. In P2, a
  promotion that succeeds must seal/activate a unique successor release
  through copy-on-write or an equivalent immutable transaction; it must never
  mutate the release currently pinned by a reader or hard-link writable index
  files from a sealed release. It records the exact pending-marker token and
  source generation in successor metadata, CAS-activates the successor, then
  compare-and-consumes only that token. On crash/retry, an active successor
  bearing the token completes consumption; a failed pointer CAS leaves both
  token and successor inactive. Design and benchmark that idempotent successor
  path as a dedicated P2 subphase before declaring P2 complete.
- `validate_release(path, expected_records, expected_identity)` which opens
  *fresh* dense and FTS handles from the candidate and checks sealed metadata,
  manifest identity, expected chunk-id set/count, dense/FTS agreement,
  tombstone counts/flags, FTS SQLite integrity, artifact tree digests, and a
  deterministic hybrid retrieval smoke set derived from the staged records.

Keep `VectorStore` and `BM25Index` as the storage implementations. Adapt their
constructors to receive a release root from `IndexLease`; do not duplicate
LanceDB or SQLite schema code in a “release store.”

Add one narrow public exception/value type for admission,
`IndexUnavailable(reason, retryable=True)`. CLI converts it to a clear
non-zero diagnostic; HTTP converts it to structured `503` without a stack
trace. Corrupt control data is non-retryable until repaired but is still
unavailable, never a fallback to live files.

### 3.4 Compatibility and migration rules

- P1 retains the existing `alexandria index --rebuild` spelling. It changes
  admission behavior, not the source-of-truth or the index schema.
- P2 keeps that spelling as the compatibility entry point, but it builds into a
  staging release. For an identity-compatible rebuild it validates and atomically
  activates on success. `--stage-only` retains a validated candidate without
  activation; `--release-status`, `--activate-release <id>`, and
  `--rollback-release <id>` are the explicit operator interfaces. Exact parser
  placement may stay under `index`; do not add a second overlapping rebuild
  command.
- A candidate with a different six-field embedding identity is staged and
  reported, but **not auto-activated**. Its command exits with a distinct
  “validated but withheld” status and a maintenance-cutover instruction. A
  planned provider/model/normalization migration validates the target serving
  configuration against the candidate, drains/marks the old service, activates
  explicitly, and starts a process with the matching configuration. If a live
  process observes a new incompatible pointer, it returns `503
  index_identity_mismatch`; it never queries it with its old embedder.
- Initial P2 adoption provides `alexandria index --migrate-release-layout`.
  Under the write lock and a maintenance gate, it **copies, never moves**, the
  legacy root artifacts into a staging release, validates them, derives a new
  activation epoch from the legacy generation, and atomically creates
  `active.json`. The old root remains a read-only rollback forensic artifact
  until a separately approved retention window expires. No old binary may run
  against that corpus after pointer activation.
- Query and answer cache keys use `activation_epoch` (not merely the old
  integer generation). Existing caches may remain on disk but are cold after
  migration/activation. The embedding cache remains outside a sealed release:
  it is keyed by model identity, mutable, and affects performance only. It is
  not copied into an index release or used as integrity evidence.
- The layout migrator and P2 rebuild must carry soft-deleted documents exactly
  as projected in both legs. A row count alone is insufficient validation.

## 4. Phased deliverables

### P0 — authoritative deployment, provenance, and no raw live copies

**Purpose:** eliminate the unsafe operating ambiguity now, before a code
redesign lands. This phase is documentation/runbook work plus an operator
cutover; it does not copy a corpus, start or stop a service, or change a
production host from this branch.

1. Add `docs/DECISION-index-authority-and-release-transfer.md`.
   - State the one-authoritative-deployment rule, authority epoch, reader-only
     replica role, source-of-truth distinction, and explicit prohibition on
     raw copies of a live `.alexandria/index` tree.
   - State that host-local `flock` is not cross-host coordination and that
     matching provider/model/dimension does not override the complete manifest
     identity check.
   - Establish the non-identifying provenance fields P2/P3 release metadata
     must carry. Make clear that they are audit evidence, not a substitute for
     cryptographic authentication or a distributed lock.
2. Revise `docs/WORK-ORDER-migrate-serve-to-nas.md` so its current raw
   corpus/index/cache transfer step is fenced: before P3, transfer no live
   derived index/cache via raw file copy. Use the canonical source repository
   and an operator-approved maintenance process for source migration; after
   P3, use only the sealed export/import protocol. Remove any suggestion that
   copying an embedding cache is required for correctness.
3. Add a non-secret companion-runbook checklist to the public doc, with the
   sensitive values explicitly delegated to the private companion runbook:
   select authority and authority epoch; inventory every server, scheduled
   writer, and local corpus copy; disable/demote non-authoritative writers;
   verify filesystem/service-account permissions make clients readers rather
   than filesystem sharers; record source
   revision/snapshot and manifest identity; and retain a rollback path. It
   must require observable evidence (job disabled, pointer/version recorded,
   health/read canary) rather than exit codes.
4. The operator completes the private runbook before P1 activation. The
   public repo contains only a redacted completion record/category, never a
   host label, address, tunnel endpoint, account name, or key reference.
   The private runbook supplies evidence that writable paths and scheduled
   writer interfaces are available only to the selected authority.

**P0 acceptance criteria**

- A cold operator can tell which role is allowed to write and what is forbidden
  without guessing from topology prose.
- The public migration documentation contains no raw `rsync`/copy procedure
  for a live index or cache and no private deployment identifiers.
- The known differently-normalized indexes are treated as incompatible until a
  planned re-embed/cutover, not as a candidate for file synchronization.
- There is one named private provenance record for the authority decision and
  a verified inventory showing only the authority runs scheduled writers.

### P1 — rebuild state, read admission, and honest health

**Purpose:** make the unsafe in-place interval fail closed while P2 is being
built. This is an availability trade-off by design: readers get `503`, not a
plausible answer from a half-built projection.

1. Implement `src/alexandria/index/rebuild_state.py` (or the
   `releases.py` control-plane module if introduced early) with a versioned,
   atomically written `rebuild-state.json`. It records opaque attempt id,
   start time, requested identity, build mode, phase, and source snapshot
   declaration. It has no host/key fields. A state present after process death
   is deliberately reported as **active-or-interrupted**, not guessed “dead”
   from a PID.
2. Treat the legacy `.rebuild-in-progress` file as an admission-blocking legacy
   state on upgrade. Do not auto-delete it. `alexandria index --rebuild-status`
   reports the state. A fresh full rebuild through an explicit
   `alexandria index --rebuild --recover` is the recovery path; P1 does not
   claim to resume arbitrary half-written dense/FTS files. A bare rebuild when
   an old state remains must refuse with the recovery instruction.
3. Add an `IndexReadGate`/reader lease beside `WriteLock` in
   `src/alexandria/writelock.py`, backed by a distinct local
   `.alexandria/index/.read-gate.lock`: normal index-artifact reads use
   `LOCK_SH`; **only an in-place P1 rebuild** takes `LOCK_EX`. Do not reuse
   `.write.lock` for reader admission, or ordinary promotion would unnecessarily
   exclude normal reads. A reader first checks rebuild state as a fast refusal,
   then acquires its shared gate lease and rechecks state authoritatively before
   any physical index access; the lease spans the complete physical
   query/retrieval operation, including lazy reads, iterators, subprocesses,
   and mmap-backed access. `/answer` releases it only after all its retrieval
   work is complete, before unrelated LLM latency. No path may obtain
   `WriteLock` while it holds `IndexReadGate`; the only allowed in-place rebuild
   order is `WriteLock` then the exclusive read gate. The existing exclusive
   index/promote `WriteLock` is acquired
   before the rebuild writer persists state and before it drops either leg. The
   writer persists state, thereby blocking new admissions, then takes the read
   gate exclusively and waits for already-admitted reader leases to drain (with
   an explicit bounded timeout and loud failure) before drop. This ordering
   means: an already admitted physical read finishes; a later reader fails the
   fast or authoritative state check; and only then does destructive drop
   begin. Persist and fsync state (and its parent directory) before destructive
   work; after rebuilding, validate and fsync both artifacts before clearing
   state, and clear/fsync state while the exclusive gate remains held. A timeout
   leaves state present and does not mutate live artifacts.
4. Route CLI `search`, `answer`, and `eval`, plus serve `/search`, `/answer`,
   startup construction, and `/health` artifact counting through that common
   admission API. `/health` must return structured `503` with `ready: false`,
   `status: "rebuilding"` or `"rebuild_interrupted"`, attempt/phase when
   readable, and no fabricated live counts. A running server must check it on
   every read request, not only when it builds its context. `ready`,
   rebuild/recovery admission, liveness/freshness, and degraded data-quality
   signals are separate fields: a healthy drain cannot make an unsafe index
   ready, and an unavailable index is not reported merely as stale. `/remember` may
   durably append and mark a new entry pending while rebuild state is present;
   its inline promotion will use the existing non-blocking writer lock and
   therefore must report accepted/pending (not immediate searchability) when
   the rebuild holds it.
5. Remove `eval --allow-partial-index`: a partial projection cannot become a
   durable evaluation baseline under an escape hatch. If forensic inspection
   is needed, expose a separately named offline diagnostic that never runs
   normal retrieval, never writes `eval_runs`, and prints an unmistakable
   unsafe banner. It is not an HTTP or standard CLI read override.
6. Clear rebuild state only after a successful P1 in-place build has written
   both legs, generation, and manifest; on every exception it remains. Error
   reporting must preserve the original failure and the recovery instruction.
   Do not make a health probe clear state.

**P1 acceptance criteria**

- A failed or killed in-place rebuild leaves an explicit admission state and
  every ordinary reader returns a named unavailable response; it never reports
  stale-looking `200` health or partial search results.
- A reader admitted just before rebuild owns a query-lifetime shared
  `IndexReadGate` lease, so drop cannot start until that physical retrieval
  exits. A reader arriving after state is persisted fails its state admission
  check and cannot enter artifacts. A bounded drain timeout leaves state
  present and artifacts untouched.
- `promote_pending` and the drain still honor the same host-local writer lock.
  The P1 change does not assert or attempt to “fix” an index/promote race that
  `tests/test_index_write_lock.py` already proves fixed.
- Recovery is always a full source-derived rebuild; no command blesses the
  old partial index by deleting its marker.

### P2 — staged releases, atomic pointer activation, and rollback

**Purpose:** replace the P1 availability outage with an immutable old-release
read path while a new release is built and validated.

1. Implement `IndexRepository`/release layout from §3.2–§3.3 in
   `src/alexandria/index/releases.py`; add an atomic-JSON/fsync helper in
   `src/alexandria/index/atomic.py` if it is not shared elsewhere. Keep
   control-plane paths outside release directories so a release remains
   immutable.
2. Refactor `cli.py` index wiring to stage a complete rebuild in a unique
   same-filesystem `staging/<release-id>`. It must write neither the active
   vector root nor active FTS database, must never reuse a release identity,
   and must not hard-link any writable candidate object from a sealed release.
   Hold the existing bounded exclusive
   `WriteLock` for the source-record snapshot through validation/activation so
   promotion cannot write into a source snapshot or old active release that is
   about to be superseded. The drain may skip as it does today; a bounded
   scheduled index wait/failure remains loud. This applies to rebuild staging;
   before P2 is declared complete, route `promote_pending` and every other
   incremental index writer through `ActiveIndexWriter` so no active sealed
   release can be modified in place.
3. Build the candidate’s dense and FTS legs, then validate it with freshly
   opened handles. Seal only after validation, then atomically activate by
   replacing `active.json`. A crash/failure before that replacement leaves the
   prior active pointer serving. A sealed-but-unactivated candidate is retained
   for inspection; it is never discovered by readers automatically.
4. Refactor `serve.py` so `ServeContext` owns an `IndexReaderManager`, not a
   permanently open root `SearchEngine`. `/search` pins one `IndexLease` for
   the request. `/answer` pins one compatible lease for all retrieval performed
   for that answer, while still releasing it before unrelated LLM latency when
   safe. A pointer change affects only subsequently admitted requests;
   in-flight requests finish against their old immutable release. CLI reads use
   the same repository API. Keep the existing in-process search serialization
   semantics where necessary; do not hold a global filesystem lock across LLM
   calls.
5. Convert P1 read leases to pin immutable generations. P2 readers do not take
   the global writer `flock` merely because a candidate is being built: their
   active files are immutable. Writer serialization remains mandatory for
   promotion/rebuild/source snapshot coherence. `promote_pending` is not an
   exception: it creates a sealed successor through `ActiveIndexWriter`, then
   activates it by compare-and-swap after validation (or leaves the old release
   active on failure). Do not remove P1 admission state until all paths use the
   manager and a state still blocks activation of an unsafe legacy/incomplete
   layout.
6. Implement explicit activation and rollback. Both revalidate sealing,
   complete object manifest, release digest, source snapshot declaration, and
   the configured runtime manifest identity before a compare-and-swap pointer
   replacement. Fsync sealed release files/directories before activation, fsync
   the temporary pointer and its parent directory after `os.replace()` where
   supported. A compatible rollback points to a prior sealed release and
   increments `activation_epoch`; it does not copy files back. Provider/model/
   normalization changes require the planned maintenance cutover in §3.4.
7. Add conservative retention/recovery behavior: retain active + previous
   releases and all pinned releases; never garbage-collect on failure; make
   pruning an explicit operator command with a dry run and a provenance log.
   Document restart recovery: on startup, open only `active.json`; ignore
   staging/incoming/orphan directories and report corrupt pointers loudly.
8. Implement `--migrate-release-layout` as described in §3.4. It is a
   one-time, maintenance-gated copy/validate/activate transition. It must
   preserve legacy artifacts and a recovery record until verified rollback and
   retention conditions are met.

**P2 acceptance criteria**

- During a long successful staged rebuild, repeated serve reads return results
  wholly from the prior sealed release until pointer activation. They do not
  observe a partially populated new leg. A promotion concurrently arriving
  after rebuild lock release produces a validated successor and does not mutate
  a reader-pinned active release.
- Injecting an error after either candidate leg writes, during validation, or
  after sealing but before pointer replacement leaves `active.json` unchanged
  and existing serve reads continue to return the known old result.
- After activation, a new request observes exactly the new release and cache
  activation epoch; an already pinned request observes exactly the old one.
  Pointer-read-plus-pin is atomic with release GC, and a GC job skips instead
  of deleting any active, previous, or reader-pinned release.
- Dense/FTS count and id-set checks, manifest identity, artifact hashes, FTS
  integrity, and tombstone projection all pass before activation. A deleted
  document remains unretrievable before and after rebuild, migration, and
  rollback.
- A release with only `normalized` changed is withheld or returns a named
  incompatibility response; no code path queries it using the old embedder.
- Crash recovery never treats a staging/incoming directory as active and never
  destroys a known-good old release.

### P3 — signed sealed transfer and verify-before-activate import

**Purpose:** provide the only supported cross-host route. P3 moves artifacts;
it does not introduce automatic replication, a distributed writer lock, or a
network daemon.

1. Add `src/alexandria/release_transfer.py` and CLI surfaces:

   ```text
   alexandria release export --release <id> --out <archive> \
       --signing-key <private-path-outside-repo> --key-id <non-secret-id>
   alexandria release verify --archive <archive> --trust-store <private-path>
   alexandria release import --archive <archive> --trust-store <private-path>
   alexandria release activate-import <release-id>
   ```

   Final flag spelling may follow existing argparse conventions, but preserve
   these four verbs and their strict separation: export, verify, import
   (inactive), explicit activation. Do not add a command that transfers from a
   remote host or shells out to `rsync`.

2. Add `cryptography` with a pinned compatible floor in `pyproject.toml` only
   if no approved in-repo Ed25519 primitive exists. Use Ed25519 signatures over
   canonical release/source-bundle manifest bytes. That bundle manifest binds
   every object's safe relative path, type, byte size, digest, source-snapshot
   identity/watermark, tombstone boundary, format/schema/tool versions,
   compatibility identity, authority epoch, and release/activation lineage.
   Private signing material and trust-store contents remain external
   configuration; tests generate ephemeral fixture keys. Verification applies
   an external allowlisted, revocable signer/key-policy for the declared trust
   domain, not merely a matching embedded key id. A checksum alone detects
   accidental damage but cannot prove origin, so it is insufficient for P3.
3. Export a deterministic, path-safe bundle containing:
   - one sealed immutable index release;
   - a canonical source snapshot for the release (indexable source roots,
     durable inbox/pending redo state needed for an authority cutover, and a
     signed source manifest); and
   - only explicitly classified durable state required for the chosen cutover.

   Query/response and embedding caches are excluded. They are mutable,
   non-authoritative, and can be regenerated. The exporter must take the local
   writer lock, require the P0 private-runbook source-quiescence gate, compute a
   deterministic source tree digest before and after bundle construction, and
   abort if it changed. The protocol assumes an authenticated encrypted
   transport chosen by operations; signing supplies origin/integrity, not
   confidentiality.
4. Import extracts into a fresh same-filesystem `incoming/<transfer-id>`
   directory. Before any release directory rename or pointer write, it rejects
   absolute/traversal paths, symlinks/hardlinks/device entries, duplicate or
   unexpected paths, unknown/downgraded schema versions, replayed release
   lineage, resource-limit violations (member count, declared/expanded bytes),
   bad signature/key id, untrusted signer, bad digest/size, failed
   SQLite/release validation, mismatched source snapshot, or a mismatch with
   the target authority/cutover declaration. Only then atomically renames
   the immutable release into `releases/<release-id>`. Import is inactive by
   default. Explicit activation re-runs identity and release validation under
   the writer/lifecycle locks, follows P2 pointer CAS, and requires explicit
   authorization for a replay or lineage rollback; import never creates an
   active pointer or consumes a pending marker.
5. Document the data-migration/cutover sequence in the private companion
   runbook: quiesce writers; export a declared source/release snapshot; verify
   out-of-band digest/signature; import without activation; verify locally;
   perform planned compatible or provider-change activation; run read/health
   canaries; only then designate the destination authoritative and retire the
   old writer. There is no overlap in which both deployments are writers.

**P3 acceptance criteria**

- A destination never changes `active.json` while importing or verifying. A
  torn/truncated archive, interrupted extraction, or process death leaves only
  an ignored `incoming` directory and the previous active release usable.
- One flipped byte in vector, FTS, source, `release.json`, or signature fails
  verification before import/activation. Wrong/untrusted key, path traversal,
  symlink, duplicate path, and source/release digest mismatch likewise fail
  closed.
- A valid archive imports inactive, validates with fresh handles, preserves
  tombstones, and can be explicitly activated/rolled back only when manifest
  identity, signer trust policy, authority provenance, and explicit lineage
  replay/rollback authorization are valid.
- The public docs no longer prescribe a raw live index/cache copy. No host
  endpoint, private key, key material, or codename appears in code, fixtures,
  test output, docs, or commit messages.

## 5. THE TEST THAT MATTERS MOST

`tests/test_rebuild_reader_safety.py::test_failed_rebuild_never_serves_a_mixed_or_partial_index`

Build a small complete synthetic corpus/index and bind a real in-process serve
context. Force a rebuild to pass state persistence and one destructive step,
then fail before completion (use an existing storage seam or a narrowly scoped
test hook; do not fake `SearchEngine`). Assert all of the following:

1. the rebuild command fails and leaves the state/attempt visible;
2. direct physical evidence shows the in-place candidate is no longer a
   complete old index, so the test would catch a permissive marker-only check;
3. `/search` and `/answer` on the **already-running** server return structured
   `503 IndexUnavailable`, never an old-looking success, empty result, or
   cross-leg result;
4. `/health` returns `503`, `ready: false`, and the rebuild reason without
   reading/reporting live artifact counts; and
5. a newly started server refuses the marked index as well.

Mutation proof is required: temporarily bypass the post-lease rebuild-state
check (or make it test-only permissive) and demonstrate that this test fails,
then restore the guard. A separate process/thread coordination assertion must
show a physical reader admitted before the writer’s exclusive lease finishes
before destructive drop begins.

P2 adds the companion test: pause a staged build after writes and force failure
at every seal/validate/activate boundary; assert the pointer and concurrent
reader result stay on the old release. P3 adds a torn-transfer variant that
kills import before atomic rename and proves the destination pointer stayed
unchanged. These are not optional substitutes for the P1 test; all three cover
different failures.

## 6. Test matrix and implementation constraints

### Required tests

| Area | Required proof |
|---|---|
| P0 docs | Documentation scan/review confirms raw live-index transfer is prohibited and no private identifiers appear. |
| P1 state | Legacy marker blocks; malformed state fails closed; state write is atomic; stale state requires explicit fresh recovery; successful completion clears only after both legs + manifest/generation. |
| P1 admission | CLI search/answer/eval, serve startup, `/search`, `/answer`, `/health`, and an already-running server all refuse a marked state. `/remember` still writes/marks durable pending input but reports it as pending when the rebuild-held writer lock makes inline promotion skip; it does not falsely claim immediate indexing. |
| P1 ordering | A real query-lifetime shared `IndexReadGate` lease (including lazy/mmap use) prevents destructive drop until released; a reader arriving after durable state is persisted fails admission; writer order is `WriteLock → read gate`; a bounded drain timeout leaves artifacts untouched. Exercise process-scoped flock behavior, not only a mocked boolean. |
| Existing writer lock | Keep `tests/test_index_write_lock.py` passing unchanged in intent: index waits/fails bounded and promote/drain skips rather than races. Add no test claiming an unfixed writer race. |
| P2 validation | Wrong count/id-set, dense/FTS mismatch, corrupt SQLite, wrong artifact hash, wrong manifest normalization, and bad tombstone projection each block sealing/activation. |
| P2 serving | Simultaneous old lease/new pointer requests are isolated; atomic pointer-read-plus-pin excludes GC races; rebuild and promotion each create unique validated successors without writable hard links rather than mutate a pinned active release; candidate failure/CAS conflict leaves active pointer unchanged; manager restart ignores orphan staging; rollback increments cache epoch. |
| P2 migration | Legacy layout is copied/validated, not destructively moved; legacy data remains recoverable; query/response cache is cold; a `deleted: true` document remains hidden in both legs. |
| P3 bundle | Deterministic complete object manifest/signature and allowlisted/revocable signer-policy check; valid verify/import remains inactive; corruption/truncation/wrong signer/untrusted signer/path traversal/symlink/hardlink/duplicate or unexpected path/schema downgrade/replay/resource-limit breach/source mismatch all fail before rename or activation. |
| P3 torn transfer | Kill/export truncation and kill/import during `incoming` extraction; after restart no unsealed directory is readable and prior active reads still succeed. |

- TDD is mandatory: write the failing test first, make the smallest
  implementation pass, and keep the suite green at every commit.
- Tests must remain fully offline. Use the project’s existing `HashEmbedder`,
  `CachedEmbedder`, synthetic corpus helpers, `FakeEngine`, and
  `ScriptedClient` patterns as applicable. Do not call a real embedder, LLM,
  corpus, remote transfer, signing service, or live server.
- Ed25519 tests use generated ephemeral test keys under `tmp_path`; never add
  a real public/private key fixture. A non-secret test key id is acceptable
  only if it is clearly synthetic and not deployment-derived.
- Exercise the SQLite fallback in the normal suite and add a narrowly scoped
  real-Lance test only where the release tree semantics cannot otherwise be
  established. Do not mistake fallback coverage for a proof that copying a
  live Lance tree is safe.
- The test suite needs deterministic pause/failure hooks at lifecycle
  boundaries. Hooks must be test-only parameters/callbacks at an existing
  orchestration seam, not production environment variables that weaken
  admission or verification.

### Do not modify without stopping and reporting why

- The semantics of `WriteLock.acquire()` that currently protect the fixed
  index/promote race: index remains bounded/blocking; drain remains deliberate
  nonblocking skip.
- `promote.py`’s ordered pending-marker redo-log steps and its manifest write
  guard. Refactor it through `ActiveIndexWriter` only with the existing
  crash/idempotency tests preserved; the successor release must be activated
  before its pending marker is consumed.
- `index/manifest.py`’s six-field identity comparison, especially
  normalization; do not relax it to provider/model/dimension.
- Soft-delete’s source-frontmatter authority and fail-closed filters in
  `index/chunker.py`, `index/store.py`, `index/bm25.py`, and retrieval
  hydration.
- `llm.py`’s temperature-zero refusal guard, the synthetic gate’s lexical-only
  configuration, and existing query/answer provenance behavior.

If a phase needs to touch an item above, stop, explain the conflict, and obtain
review rather than routing around the guard.

## 7. Failure recovery and operational safety gates

### P1 recovery table

| Event | Required behavior |
|---|---|
| Writer fails before destructive drop | State remains; all normal reads unavailable; `--recover` starts a new full rebuild, never clears state alone. |
| Writer fails after one/both live legs changed | Same: state remains and reads unavailable. No partial resume or health-count workaround. |
| State/control JSON corrupt | Fail closed with named repair instruction; do not treat it as absent. |
| Writer lock busy | Preserve current bounded index failure and drain skip behavior. Never proceed unlocked. |
| Read lease busy / rebuild under way | Reader returns retryable `503` rather than waiting indefinitely or reading files; writer never obtains `WriteLock` while holding a shared read gate. |

### P2/P3 recovery table

| Event | Required behavior |
|---|---|
| Candidate build/validation/seal fails | Leave `active.json` untouched; retain staging/orphan evidence; explicit inspection/prune only. |
| Crash during atomic pointer write | Atomic replace presents either old or new complete pointer; corrupt/missing pointer fails closed and requires explicit rollback/repair. Orphan successor directories/timestamps never imply activation. |
| New release is runtime-incompatible | Withhold activation or return named `503`; coordinated maintenance changes runtime identity before explicit activation. |
| Release directory accidentally removed/corrupt after activation | Fail closed; activate a verified retained release/rollback through the repository API. Never scan for a substitute. |
| Transfer fails/torn | Ignore `incoming`; keep old active; re-transfer a new signed bundle. |
| Source changes during export | Export aborts on pre/post snapshot mismatch; runbook quiescence remains mandatory because a generic filesystem has no transactional corpus snapshot. |

Operational gates for every real cutover are: P0 authority inventory complete;
no non-authoritative scheduled writer; target source snapshot/release signature
verified; target manifest identity explicitly reviewed (including normalized);
active and rollback release IDs recorded in the private runbook; read + health
canaries pass; and only then writer role changes. “Command exited zero” is not
a gate.

## 8. Out of scope

- Executing the operator runbook, copying any real corpus/index/cache, changing
  a remote machine, starting/stopping a service, or modifying a live schedule.
- Automatic multi-host replication, consensus/distributed locking, leader
  election, peer-to-peer sync, or a remote file-transfer daemon.
- Encryption/key management, tenant authorization, or a general backup
  product. P3 requires external authenticated encrypted transport and external
  key storage; it does not invent either.
- Changing embedding quality, embedding providers, rerankers, chunking, or
  enrichment. A provider identity change is a controlled compatibility cutover,
  not a retrieval experiment in this work order.
- Deleting source documents, dropping tombstones, or treating derived index
  deletion as source deletion.
- Garbage collecting legacy/old releases automatically, rewriting historical
  eval results, or copying query/response/embedding caches as correctness data.
- Relaxing privacy/leak scanning. “Redacted” text that can identify a real host
  or key remains out of bounds.

## 9. Verification before reporting done

Run in the named project environment, from this repository, after each phase:

```bash
.venv/bin/python -m pytest tests/test_index_write_lock.py tests/test_serve.py \
    tests/test_manifest.py tests/test_promote.py tests/test_soft_delete.py -q
.venv/bin/python -m pytest tests/ -q
.venv/bin/python scripts/precommit-scan.py --all
```

For P2/P3 additionally run the new focused release/admission/transfer tests
under repeated execution to expose pointer/lease timing errors. Use a temporary
synthetic corpus only. Do **not** perform a real-corpus rebuild, serve restart,
export, import, `sync`, `promote`, or `--enrich` as part of implementing this
engine work order.

Before a human performs P0/P3 operational steps, provide a separate private
runbook review containing the exact endpoints and credentials. The public
implementation report supplies only non-identifying release ids/digests,
manifest identity fields, test counts, observable canary outcomes, and any
failure/recovery evidence.

## 10. Report back

For each shipped phase, report:

1. exact modules/docs added or changed and the compatibility behavior retained;
2. test counts plus the focused proof for §5 and the relevant P2/P3 failure
   injections;
3. release-layout/control-file schema version and migration behavior;
4. proof that normalization mismatch, tombstones, cache activation, and
   index/promote locking were preserved;
5. any staged-but-withheld candidate and why it was not activated;
6. all operational actions deliberately left for the private runbook/human;
   and
7. any deviation from the phase boundary, with a reason and a stop/go request.
