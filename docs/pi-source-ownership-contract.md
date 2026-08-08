# pi-source-ownership-contract.md

# Source-ownership contract (2026-08-08)

**Decided by:** Stanley + Red deliberation (gpt-5.6-sol via the
subscription gateway). Alexandria is a **read-optimized federated overlay**,
not a canonical store. This contract fixes who owns what, and what Alexandria
may and may not do to it.

## Principles

1. **Harness-native files are authoritative and writable by their owners.**
   Every harness source store under `sources/` is ingested read-only.
   Alexandria indexes them without rewriting, renaming, or merging them.
2. **Alexandria's only explicit write surface is the inbox.** User-confirmed
   memories enter via `alexandria remember` (or the inbox file) and are
   promoted by sync. Nothing else auto-writes into `sources/`.
3. **No generated content is self-ingested.** Wiki pages, answers, digests,
   and summaries are never re-ingested as source material (no
   feedback-loop pollution).
4. **Provenance is preserved on every record.** Each indexed doc retains
   source URI/id, content hash, and derivation status (original vs
   distilled). Distillation never edits the source.
5. **Rebuild reproducibility.** Re-running ingestion over the sources must
   reproduce the index (content-derived ids, no hidden state).

## Ownership map

The full map with real source names lives in the private corpus
(`CONTRACT-source-ownership.md`). In public terms: every harness-native
source store is read-only to Alexandria; the Alexandria-owned write areas
are `ops/` (own docs), `journal/` (curated daily-digest), and `inbox/`
(explicit user-confirmed memories); `wiki/` and `notes/` are generated
and never re-ingested; `.alexandria/` holds index/logs/reports.

## Enforced by

- The distiller and connectors never mutate non-owned sources (verifiable
  in `connectors/`).
- `lint` requires schema-valid frontmatter on every doc (source, source_id,
  generated/by+at, actor convention).
- The extension exposes exactly three read tools + one explicit write tool
  (`alexandria-remember`).

## History

Produced in response to Red risk #2/#4 (unified-index-vs-canonical-store
confusion; weak provenance/dedup). Supersedes no prior doc; codifies the
activation decision's write-surface rule at the source level.
