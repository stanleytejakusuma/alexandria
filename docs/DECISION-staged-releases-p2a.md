# Decision: staged releases (P2a) — a bounded slice of backlog #30

**Date:** 2026-08-19
**Status:** decided (scoped down from the full 4-phase work order after a
second-opinion review; see rationale below)
**Supersedes, for now:** `docs/WORK-ORDER-rebuild-atomic-and-release-integrity.md`
P2's full `IndexRepository`/`ActiveIndexWriter`/`IndexReaderManager` design.
That design is not abandoned — it is P2b, explicitly deferred (see below).

## What changed since the work order was written

The work order's P1 (read admission during rebuild) already shipped as a
simpler, Red-approved substitute: `IndexReadLock` (shared, non-blocking) +
`rebuild_marker`. A normal reader gets a clean 503 during an in-place rebuild
instead of a torn read. That closes the CORRECTNESS gap P1 existed for.

What P1 does NOT close: **a failed rebuild currently destroys or partially
overwrites the only copy of the index.** `--rebuild` calls `store.drop()` +
`lexical.drop()` in place before refilling. A crash at any point after that —
OOM, a bad document, a code bug, a killed process — leaves the corpus without
a working index until a NEW rebuild succeeds, which is unbounded if the
failure is persistent. That is the actual remaining pain, not cross-host
downtime (there is currently one host; the second host is
unreachable and the migration to it was rolled back).

## Decision

Build a staged-release layout NOW (P2a). Defer the in-process, per-request
lease/reader-manager refactor (P2b) and the signed cross-host transfer
protocol (P3) until their triggers fire.

### P2a — staged build + atomic pointer + restart cutover

1. A rebuild builds a COMPLETE new index at `.alexandria/index/releases/<id>/`
   — never touching the live path — using the same `VectorStore`/`BM25Index`
   classes, which already accept an arbitrary path.
2. The candidate is validated (row counts, manifest identity, a checksum
   manifest of every file) BEFORE anything is published.
3. `active.json` (one file, at `.alexandria/index/active.json`) names the
   current release id. Published via write-temp-then-atomic-`os.replace()`,
   fsynced. This is the ONE load-bearing, hard-to-reverse format decision
   this note exists to pin before code commits it.
4. Cutover uses the EXISTING `IndexReadLock` + `rebuild_marker` machinery: a
   brief exclusive window while the running process reopens against the new
   `active.json`. This is NOT the full P2 in-process lease-swap (no
   `IndexReaderManager`, no per-request `IndexLease`) — a single-host,
   single-writer, single-serve-process deployment gets ~99% of the
   availability value (bounded seconds of 503, not hours) from restart-based
   cutover at near-zero blast radius to `serve.py`'s concurrency model.
5. Retention: keep `active` + the immediately previous release; never
   silently garbage-collect. An explicit `--gc` command, dry-run by default.
6. Rollback: repoint `active.json` to the previous release id and reopen —
   no file copy, near-free.
7. Legacy layout: the CURRENT flat layout (`chunks.lance`, `fts.sqlite`,
   `manifest.json` directly under `.alexandria/index/`) is read as an
   implicit "unmanaged" release for one migration cutover, then retired.

### Explicitly deferred: P2b (in-process lease swap)

`IndexReaderManager`, `IndexLease`, refcounted GC, zero-restart cutover in a
long-lived `serve` process. **Trigger to revisit:** a second reader
process/host actually exists, or restart-based cutover proves operationally
annoying in practice (measured, not assumed).

### Explicitly deferred: P3 (signed cross-host transfer)

Export/verify/import/activate-import CLI verbs, Ed25519 signing, a trust
store. Its entire premise is a second host, which is not currently active,
and the eventual topology may not match what would be designed today.
**Trigger to revisit:** a second host is live again and its topology is
settled. **Pulled forward now:** the unsigned checksum manifest per release
(item 2 above) — cheap today, and exactly the artifact P3 would later sign.

## Why not build the full P2 now

`IndexReaderManager`/`IndexLease` exist to let a long-lived process cut over
WITHOUT a restart, and to let GC safely reap files a live reader still holds.
On a single host with one controllable `serve` process, "reopen under the
existing lock" is functionally equivalent in outcome (seconds of 503) at a
fraction of the engineering cost and with no change to `serve.py`'s
concurrency model. Building the full lease refactor now — before a second
reader process exists to need it — would be exactly the "finish it because
it is partially specified" trap: real engineering effort spent on a property
nobody would currently observe.
