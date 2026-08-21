"""#6 erasure-core: an explicit enumeration of every module that persists
doc-derived content, so a future new persistence surface cannot silently
escape erasure scope the way #9's citation tuples almost did (caught only
by manual cross-referencing during that session, not by any test).

This is a STATIC ENUMERATION plus a contract test (tests/test_erasure_surfaces.py)
that binds it to reality two ways: (1) every class listed here is imported
from its real source module, so a renamed/removed class fails the import,
not a silently-stale string; (2) a module-discovery scan of src/alexandria/
for classes matching a persistence-shaped name pattern must not find
anything NOT already listed here, so a genuinely new store/cache class
trips the test even if nobody remembered to add it manually.

Per docs/DECISION-erasure-scope-q1.md (ratified 2026-08-21): the tombstone
(`alexandria delete`) is authoritative for what "erased" means at the
retrievable surface. Git-history erasure (the separate, deliberate
`alexandria erase` operation, not yet built) is scoped to the corpus git
repo's commit history for a document's source file -- it does not, by
itself, reach into any of the surfaces enumerated here. Each surface below
states whether the EXISTING tombstone already covers it, or whether it
needs its own explicit handling as erasure work continues.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["ErasureSurface", "ERASURE_SURFACES"]


@dataclass(frozen=True)
class ErasureSurface:
    name: str
    module: str
    persists: str  # what doc-derived content this surface holds
    tombstone_covers: bool  # does `alexandria delete` already suppress this surface?
    note: str


ERASURE_SURFACES = (
    ErasureSurface(
        name="VectorStore (dense/LanceDB)",
        module="alexandria.index.store",
        persists="chunk text + embedding vector, per doc_id",
        tombstone_covers=True,
        note="mark_deleted() flips `deleted` in place; not_deleted_clause "
             "enforces it unconditionally at every read. Vectors persist "
             "physically until the next --rebuild purges them from the "
             "active release (backlog #6 core item 1's framing: suppression "
             "is immediate, physical purge is rebuild-gated).",
    ),
    ErasureSurface(
        name="BM25Index (lexical/FTS5)",
        module="alexandria.index.bm25",
        persists="chunk text, per doc_id",
        tombstone_covers=True,
        note="Same mark_deleted()/not_deleted_clause pattern as VectorStore, "
             "reprojected in the same cmd_delete call.",
    ),
    ErasureSurface(
        name="EnrichmentStore",
        module="alexandria.enrich",
        persists="LLM-generated summary/keywords/hypotheticals, keyed by "
                 "(doc_id, content_sha, recipe)",
        tombstone_covers=True,
        note="invalidate(doc_id) is called by cmd_delete on the delete path "
             "(not undelete) -- #6 erasure-core item 2, this commit.",
    ),
    ErasureSurface(
        name="ResponseCache",
        module="alexandria.cache",
        persists="a full synthesized answer page, keyed by "
                 "(question, model, k, prompt_version, generation, pipeline)",
        tombstone_covers=True,
        note="Keyed by corpus generation (cache.py:249-262), and cmd_delete "
             "bumps the generation counter unconditionally (even on partial "
             "dense/lexical failure, per SOL-03) -- any answer cached before "
             "a delete is structurally unreachable by key after it.",
    ),
    ErasureSurface(
        name="QueryCache",
        module="alexandria.cache",
        persists="a retrieved chunk-id/score list, keyed similarly to "
                 "ResponseCache including generation",
        tombstone_covers=True,
        note="Same generation-keying argument as ResponseCache.",
    ),
    ErasureSurface(
        name="QueryLogger (queries.sqlite)",
        module="alexandria.monitor",
        persists="query text, retrieved chunk ids, scores -- per search, "
                 "not per document",
        tombstone_covers=False,
        note="A tombstoned document's chunk_id can remain in an OLD query "
             "row's retrieved_ids list forever (append-only, no TTL). This "
             "is the AUDIT TRAIL surface -- per the ratified decision, this "
             "stays. Listed here so that decision is traceable to a "
             "specific surface, not just a category.",
    ),
    ErasureSurface(
        name="AuditLogger (answers.jsonl, incl. #9 citation tuples)",
        module="alexandria.auditlog",
        persists="full answer records including per-claim citation tuples "
                 "(query_id, claim_id, doc_id, chunk_id, rank, claim_verdict, "
                 "source_round) -- durable, no TTL, per #9",
        tombstone_covers=False,
        note="Same AUDIT TRAIL classification as QueryLogger -- stays, per "
             "the ratified decision. #9 made this surface durably richer "
             "(doc_id is now explicit in every citation tuple); the "
             "ratified decision was made WITH that fact known.",
    ),
    ErasureSurface(
        name="CachedEmbedder (embedding cache)",
        module="alexandria.index.embedder",
        persists="a raw embedding vector, keyed by sha256(model_name + text) "
                 "-- content-addressed, not doc-id-addressed",
        tombstone_covers=True,
        note="Content-addressed means a tombstoned document's chunk text "
             "no longer resolves to any live chunk_id anywhere -- the cache "
             "row becomes orphaned (unreachable, not actively serving "
             "anything) rather than needing explicit deletion. Self-heals: "
             "a cache entry with no live referent is inert.",
    ),
)
