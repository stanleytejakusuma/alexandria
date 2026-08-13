"""§4 / §4.2.1: promote pending `remember` entries end to end.

`remember` only appends to the inbox and marks the entry pending (§4.1) --
fast, no model load (gate W1). This module does the rest: inbox entry ->
promoted document -> chunks -> embed -> LanceDB -> FTS5 -> generation bump
-> marker unlink, in the crash-safe order specified by §4.2.1:

1. **Embedding cache** -- ON CONFLICT DO UPDATE, own transaction. Idempotent.
2. **LanceDB** -- `merge_insert("chunk_id")`. Idempotent because chunk_id is
   fully content-deterministic (`index/chunker.py`'s `_make`).
3. **FTS5** -- DELETE-batch + INSERT in one transaction under WAL. Atomic
   and idempotent on rerun.
4. **Generation bump** -- once per cycle (not once per fact, gate W4), and
   strictly after 2 and 3: bumped before, a concurrent reader could cache a
   pre-promote answer under the *new* generation with no expiry.
5. **Unlink the pending marker(s)** -- strictly last. The marker *is* the
   redo log: a crash anywhere before this leaves it in place, and a rerun
   converges by the idempotency of steps 1-3, then bumps again (harmless).

The whole sequence runs under `WriteLock` (§4.2): the drain skips its run
rather than blocking when another process holds it (gate W5).

**The inverse case.** Nothing guarantees the inbox append happened before a
marker exists (or that the file promote can see is even the right one) --
a pending id with no matching inbox entry is reported as an error and its
marker is deliberately left in place, never unlinked silently, per §7.1: "a
marker that vanishes without a promotion is indistinguishable from work
completed."
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .cache import write_index_generation
from .connectors.inbox import InboxConnector
from .index.chunker import chunk_doc_records
from .index.manifest import read_manifest, verify_manifest_for_write, write_manifest
from .pending import list_pending, unlink_pending
from .writelock import write_lock

__all__ = ["PromoteResult", "promote_pending"]

# Names of the five ordered steps, in order -- used by the test_hook callback
# so gate W3a can inject a crash after any one of them.
STEPS = ("embed", "upsert", "fts", "bump", "unlink")


@dataclass
class PromoteResult:
    promoted: list[str] = field(default_factory=list)
    skipped_locked: bool = False
    errors: list[str] = field(default_factory=list)
    chunks_written: int = 0

    @property
    def did_work(self) -> bool:
        return bool(self.promoted)


def promote_pending(corpus, config, embedder, store, lexical, *,
                     entry_ids: list[str] | None = None,
                     test_hook: Callable[[str], None] | None = None) -> PromoteResult:
    """Promote some or all currently-pending entries.

    entry_ids: which pending ids to process; defaults to everything in
    .alexandria/pending/.
    test_hook: called with each step name in STEPS immediately after that
    step completes -- gate W3a's crash-injection lever. A hook that raises
    simulates a crash at that exact point; promote_pending does not catch
    it, so the lock is released (via `finally`, same as a real process
    death would release flock) and the caller observes the same state a
    real crash would leave.
    """
    corpus = Path(corpus).expanduser()
    ids = list(entry_ids) if entry_ids is not None else list_pending(corpus)
    result = PromoteResult()
    if not ids:
        return result

    lock = write_lock(corpus)
    if not lock.acquire():
        result.skipped_locked = True
        return result
    try:
        # Same F4 write-path guard as cmd_index: a drain running under a
        # different --embed-provider would otherwise pollute the vector space
        # silently. serve verifies at startup (S9); cmd_promote builds its own.
        verify_manifest_for_write(corpus, embedder, config.embed_provider, store)
        if store.count() == 0:
            # promote is a writer, so it must claim the vector space it is about
            # to populate. Written BEFORE any vector lands: a corpus reached
            # only through remember+promote (never `alexandria index`) would
            # otherwise end up non-empty with no manifest, and every subsequent
            # promote would refuse forever.
            #
            # The condition MUST be the same predicate the guard exempts on
            # (count == 0), not `read_manifest() is None`. With the two out of
            # step, a promote that claimed provider A and then failed before
            # store.upsert leaves an empty index labelled A; the next promote
            # under provider B is exempted by count == 0, finds a manifest
            # present so does not rewrite it, and lands B vectors under an A
            # label. promote never rewrites the manifest at the end the way
            # cmd_index does, so nothing repairs it -- permanent mislabelling
            # in a corpus with no deletion path. Re-claiming whenever the index
            # is empty is idempotent and closes that window.
            write_manifest(corpus, embedder, config.embed_provider)
        conn = InboxConnector(inbox_dir=corpus / "inbox")
        wanted = set(ids)
        items_by_id = {}
        for item in conn.discover():
            if item.source_id in wanted:
                items_by_id[item.source_id] = item
                wanted.discard(item.source_id)
        for missing in sorted(wanted):
            result.errors.append(
                f"pending marker {missing!r} has no matching inbox entry -- "
                f"leaving the marker in place (SPEC §7.1 inverse case)")

        records: list[dict] = []
        promotable_ids: list[str] = []
        for entry_id, item in items_by_id.items():
            try:
                docs = conn.normalize(item)
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                result.errors.append(f"{entry_id}: normalize failed ({exc})")
                continue
            entry_records: list[dict] = []
            entry_ok = True
            for doc in docs:
                doc.write(corpus)
                chunk_records, error = chunk_doc_records(corpus / doc.path, corpus, config)
                if error:
                    result.errors.append(f"{entry_id}: {error}")
                    entry_ok = False
                    continue
                entry_records.extend(chunk_records)
            if entry_ok and entry_records:
                records.extend(entry_records)
                promotable_ids.append(entry_id)

        if not records:
            return result

        # Step 1: embedding cache (idempotent, own transaction per key)
        vectors = embedder.embed([r["text"] for r in records])
        for record, vector in zip(records, vectors, strict=True):
            record["vector"] = vector
        if test_hook is not None:
            test_hook("embed")

        # Step 2: LanceDB merge_insert (idempotent, content-deterministic chunk_id)
        store.upsert(records)
        if test_hook is not None:
            test_hook("upsert")

        # Step 3: FTS5 (atomic DELETE+INSERT in one transaction, idempotent)
        lexical.index(records, append_only=False)
        if test_hook is not None:
            test_hook("fts")

        # Step 4: generation bump -- ONCE per cycle (W4), strictly after 2 and 3
        write_index_generation(corpus)
        if test_hook is not None:
            test_hook("bump")

        # Step 5: unlink pending markers -- strictly last; the marker is the redo log
        for entry_id in promotable_ids:
            unlink_pending(corpus, entry_id)
        if test_hook is not None:
            test_hook("unlink")

        result.promoted = promotable_ids
        result.chunks_written = len(records)
        return result
    finally:
        lock.release()
