# Atomic weekly publish design

**Status:** proposal for review before implementation.

## Problem

The weekly loop now detects a broken CLI, bounds work, and skips snapshots on
known failures. That is insufficient for a forced timeout: the canonical corpus
contains source documents, connector state, an active index, and Git history.
There is no single pointer that publishes all of them coherently. Copying files
or committing Git after partial work does not create an atomic result.

`Doc.write` is now temp-and-replace, so one interrupted document is not torn.
That does not make a partly completed batch or in-place index safe to publish.

## Required invariant

After a killed, failed, or timed-out run, a reader sees either the complete
previous corpus generation or a fully verified new generation—never a mixture.
No snapshot commit may describe a generation whose active index cannot answer
from the visible sources.

## Proposed model: generation-root pointer

1. Treat the live corpus path as a stable **pointer** to a complete generation
   directory, not as the mutable generation itself.
2. Create a sibling staging generation on the same filesystem. Copy the last
   complete generation using copy-on-write/reflink where available; otherwise
   use an explicitly measured copy. Its internal `.git` is not the publisher.
3. Run bounded connector batches and a rebuild-only index in staging. Never
   run incremental in-place indexing in the loop.
4. Verify staging: connector state, freshness, document/chunk counts, staged
   release checksum, a search smoke test, and clean Git status.
5. Commit the staging generation's source snapshot before publishing it.
6. Fsync every generation payload and directory, then atomically replace the
   root pointer. Fsync the pointer's parent directory afterward.
7. Keep the previous generation until a separate retention/GC policy approves
   deletion. A failed run leaves an unreferenced staging generation for audit
   and can never alter the pointer.

## Explicit non-solutions

- `rsync` into the live corpus.
- Swapping `sources/`, `.alexandria/state/`, and `.alexandria/index/` one at a
  time.
- A best-effort Git commit after failures.
- Reusing an active mutable index.
- Treating a checksum map as an immutable seal while its vector backend can
  mutate on read (tracked separately in backlog #59).

## Design decisions still needed

1. **Path migration:** serving and every consumer must use the stable pointer,
   not an embedded generation path. This requires a controlled service
   cutover, not an overnight restart.
2. **Git authority:** decide whether each generation has its own Git checkout,
   or source history is maintained outside the published root. The source
   snapshot must remain inspectable and reversible.
3. **Storage budget:** measure one generation's data/index footprint and retain
   at least two complete generations plus one failed staging candidate.
4. **Vector seal:** #59 must define a genuine immutable vector-reader contract
   before the staged checksum can be called a production integrity proof.

## Minimum implementation proof

A fixture test must kill a staging run during (a) document write, (b) connector
state update, and (c) index build. In every case the stable pointer still
resolves to the old generation and its search results remain unchanged. A
successful run must switch the pointer exactly once and retain the predecessor.
