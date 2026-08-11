# SPEC — versioning, supersession, and history

Status: **accepted, unimplemented.** Written 2026-08-12.
Supersedes the in-place `status: superseded` proposal, which was wrong (see §2).

---

## 1. The problem, verified against source

Alexandria today cannot express "this knowledge changed."

| fact | evidence |
|---|---|
| An edited entry becomes a **new document**; the old one persists | `connectors/inbox.py:50` — `entry_id = sha256(created + "\n" + text)[:12]` |
| Both versions then compete on similarity alone | nothing distinguishes them at rank time |
| `--corrects` is written to frontmatter and **never read** | `cli.py:209`, `connectors/inbox.py:130` write it; zero references in `retrieval/` or `synthesis/` |
| `corrects` is not even indexed | absent from `SCALAR_FIELDS` and `ALL_FIELDS`, `index/store.py:10-19` |
| The only supersession handling anywhere is a prompt line | `synthesis/gather.py:20` — "EARLIER assertions … may have been superseded". Applies to `answer` only, never `search` |

Consequence: a user can record that B corrects A, and the system will still serve A —
with citations, confidently. Correction is currently **aspirational**.

There is also no deletion path anywhere in the codebase, so nothing can be
withdrawn once indexed.

## 2. Why the obvious fix is wrong

The tempting fix is to mark the old document `status: superseded` in place —
`status` is already in `SCALAR_FIELDS` and already filterable, so it costs nothing.

**Reject it.** In-place mutation destroys the record of what the system believed
at a past moment. The enterprise question is not "what do we know now" — it is
*"what did this system tell the person who made that decision, at the time they
made it."* That is an audit and liability question. A mutating store cannot answer
it, and cannot prove it did not retroactively edit itself.

## 3. Design: identity separate from revision

The content-hash id looked like the defect. It is the asset — it makes documents
immutable by construction. Keep it. Add the missing half.

```
entity_id     stable across all versions of one claim      (new)
entity_rev    monotonic integer, 1..N                      (new)
supersedes    entity_id + rev this revision replaces       (new, indexed)
generated_at  already indexed — gives the time axis        (exists)
```

- A revision is an **append**, never an edit. Document files remain write-once.
- `entity_id` is minted on first write and carried forward by `--corrects`.
- Physical deletion never happens. Withdrawal is a revision carrying a
  `deleted` marker — a tombstone (see §6).

Reference design: a prior local memory tool in this corpus separates
`entity_id` from `entity_rev` with an operation hash and a `deleted` flag, plus
an outbox and dead-letter queue for replay. Its schema is sound and worth
copying. **Its tables are empty** — 0 rows across all three sync tables, and
only 7 of 12,444 records ever reached rev 2 — so treat it as an unexercised
reference, not a validated one. Do not assume the pattern works because it was
written down.

## 4. Read-time resolution

Resolution belongs at query time, not write time — that is what keeps the log
immutable.

1. Retrieve normally (BM25 ∥ dense → RRF → boosts), which may surface several
   revisions of the same `entity_id`.
2. Collapse by `entity_id`, keeping the highest `entity_rev` **at or before the
   query's as-of bound** (default: now).
3. Suppress tombstoned entities unless explicitly requested.
4. Annotate: a surfaced revision that superseded something says so, and names
   what it replaced.

Collapse-by-key is not new machinery — `retrieval/search.py:198-229` already
collapses synthetic chunks onto their target. This is the same operation on a
different key.

## 5. As-of queries

`generated_at` is already indexed, so time travel needs no new field:

```
alexandria search "<q>" --as-of 2026-08-01
```

filters `generated_at <= bound` and resolves chains as of that bound. This is
the audit primitive. It is the reason §2 is rejected — you cannot reconstruct a
past answer from a store that overwrote its own history.

## 6. Tombstones vs erasure — an unresolved tension

Tombstones give **logical** withdrawal: the content stops being served.

They do **not** give erasure. The bytes remain in the corpus, the index, the
embedding cache, and every git object. An immutable log and a right-to-erasure
obligation are in direct conflict, and immutability makes the existing gap worse
rather than better.

The standard resolution is crypto-shredding: encrypt per subject, destroy the
key to erase. That decision constrains the storage layer and is
expensive to retrofit across 33k existing documents — so it must be decided
before the log grows further, but it is **out of scope for this spec**.

Do not claim erasure support until this is resolved.

## 7. Migration

Existing documents have no `entity_id`. Backfill is mechanical:

- `entity_id := doc_id`, `entity_rev := 1` for every existing document.
- Existing `corrects` pointers in frontmatter become `supersedes` edges; entries
  whose target resolves get the target's `entity_id` and `rev = target_rev + 1`.
- Unresolvable pointers are logged, not guessed.

Backfill is additive and requires no rewrite of document bodies.

## 8. Gates

- **V1** A revision never mutates or deletes a prior document file. Verified by
  fault injection: attempt an in-place edit, assert it is rejected.
- **V2** Given A and its correction B, a default query returns B and not A;
  the result names A as superseded.
- **V3** `--as-of <date-before-B>` returns A, proving history survived.
- **V4** A tombstoned entity is absent from default results and present under
  an explicit history query.
- **V5** Backfill assigns exactly one `entity_id` per existing document and
  leaves every body byte-identical.
- **V6** The corpus document count after backfill is unchanged.

Each gate is a test, not a claim. A gate that cannot fail is not a gate — the
enrichment certification failed precisely there.

## 9. Deliberately not built

- **Merge/branch semantics.** Linear revision chains only. Concurrent edits to
  one entity from two harnesses are a real enterprise problem; they are not a
  problem at current scale, and a CRDT is not payable now.
- **Diff/blame UI.** The data supports it. Nothing needs it yet.
- **Automatic supersession detection.** Deciding that B corrects A without being
  told is a model judgment; it will be wrong, and wrong supersession silently
  hides true knowledge. `--corrects` stays explicit.
