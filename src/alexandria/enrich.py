"""Per-document enrichment: summary, keywords, hypothetical questions.

One LLM call per document (not per chunk): the metadata is attached to every
chunk of the doc, so the cost stays linear in documents. Hypothetical
questions are embedded as query-space synthetic vectors so retrieval can
match question-to-question (the production-RAG pattern from ByteMonk's
architecture talk -- aimed at our weakest measured band, zero-overlap
recall at 38.9%). Enrichment never fails indexing: bad LLM output degrades
to no enrichment.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from .untrusted import INERT_DATA_FRAMING, escape_for_prompt, looks_like_injected_instruction

# #5/F3b: this builder was the one prompt builder in the codebase missing the
# inert-data framing already carried by synthesis/write.py, gather.py, and
# repair.py (docs/SPEC-multi-tenant-and-learning-loop.md Part F). Shares the
# exact wording via untrusted.INERT_DATA_FRAMING rather than restating it, so
# the four builders cannot silently drift from each other.
ENRICH_SYSTEM = (
    "You enrich a private knowledge-base document for retrieval. Read the "
    "document and return ONE JSON object with exactly these keys:\n"
    '{"summary": string under 40 words capturing the document core '
    "subject and its claims, \"keywords\": array of 4-8 topical keywords, "
    "\"hypotheticals\": array of 3-5 plausible USER QUESTIONS this document "
    "would answer, phrased the way a user would actually ask them, not "
    "using the document's own wording.}\n"
    f"{INERT_DATA_FRAMING}\n"
    "Return ONLY the JSON object, no prose, no markdown fences."
)

MAX_DOC_CHARS = 8000
MAX_HYPOTHETICALS = 3
MAX_HYPOTHETICAL_CHARS = 200  # #5/F3c: only array length was capped before


def enrich_doc(llm, doc_id: str, doc_text: str) -> dict[str, Any]:
    """One LLM call; returns {"summary", "keywords", "hypotheticals"} (any
    subset on failure -- the caller attaches whatever came back)."""
    # #5/F3a: doc_text is retrieved corpus content, potentially third-party
    # and untrusted (the spec calls this the real unframed surface -- a
    # retrieval-POISONING vector, not just an answer-poisoning one, since a
    # hypothetical becomes a first-class synthetic vector that boosts ranking
    # for future queries). Give it an explicit <document> delimiter (the same
    # pattern write.py/gather.py/repair.py already use) so escaping has a
    # real boundary to protect, then escape both the id and the text.
    user = (f'<document id="{escape_for_prompt(doc_id)}">\n'
            f"{escape_for_prompt(doc_text[:MAX_DOC_CHARS])}\n"
            "</document>")
    try:
        raw = llm.complete(ENRICH_SYSTEM, user, temperature=0.1)
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    payload = _parse_enrichment(raw)
    if not payload:
        payload["error"] = "unparseable enrichment response"
    return payload


def _parse_enrichment(raw: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    try:
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end <= start:
            return out
        parsed = json.loads(raw[start:end + 1])
        for key in ("summary", "keywords", "hypotheticals"):
            if key in parsed:
                out[key] = parsed[key]
        out["keywords"] = [str(k) for k in (out.get("keywords") or [])][:8]
        # #5/F3c: per-item length cap (only array length was capped before)
        # plus a plausibility filter that drops instruction-shaped entries
        # BEFORE they can become synthetic retrieval vectors. suspicious_count
        # is surfaced to the caller (enrich_docs_for_index's stats dict) so
        # an operator can see attempts, not only successes (F3e).
        raw_hypotheticals = [str(h)[:MAX_HYPOTHETICAL_CHARS]
                             for h in (out.get("hypotheticals") or [])][:MAX_HYPOTHETICALS]
        clean, suspicious = [], 0
        for h in raw_hypotheticals:
            if looks_like_injected_instruction(h):
                suspicious += 1
            else:
                clean.append(h)
        out["hypotheticals"] = clean
        # Red review 2026-08-20 (finding #8): Gate F's own wording names
        # "reranked text" as a steering surface -- summary is appended to
        # every real chunk's reranker input (search.py:_rerank_text) on
        # every future query, unfiltered, until this fix. Same faithfulness
        # concern as hypotheticals, so the same conservative filter applies:
        # an instruction-shaped summary is dropped (falls back to the
        # chunk's own body text for reranking) rather than trusted verbatim.
        summary = str(out.get("summary") or "")[:200]
        if summary and looks_like_injected_instruction(summary):
            summary = ""
            suspicious += 1
        out["summary"] = summary
        if suspicious:
            out["suspicious_count"] = suspicious
    except (json.JSONDecodeError, TypeError, AttributeError):
        return {}
    return out


def doc_fingerprint(doc_records) -> str:
    """Unambiguous canonical fingerprint of a doc's chunk texts.

    Length-framed canonical framing (json list) + FULL sha256 -- Red
    2026-08-09: joins need framing, 16 hex chars needlessly halves the
    digest."""
    texts = [r["text"] for r in doc_records]
    return hashlib.sha256(json.dumps(texts, sort_keys=True).encode()).hexdigest()


def synthetic_records(doc_records, payload: dict, anchor_chunk_id: str,
                      vectors: list[list[float]]) -> list[dict]:
    """Synthetic chunk records from enrichment hypotheticals.

    Red 2026-08-09: synthetic records carry EXPLICIT metadata -- kind,
    parent_doc, target_chunk -- never a naming-convention parse. They are
    document-routing signals: at search time a hypothetical hit boosts its
    target real chunk; synthetic records are never surfaced to rerank,
    synthesis, or citations."""
    base = dict(doc_records[0])
    out: list[dict] = []
    for i, (hq, vector) in enumerate(
            zip(payload.get("hypotheticals", []), vectors, strict=True)):
        out.append({
            **base,
            "chunk_id": f"{anchor_chunk_id}::hq{i + 1}",
            "text": str(hq),
            "heading_path": base.get("heading_path") or "",
            "layer": "synthetic",
            "vector": vector,
            "enrichment": None,
            "kind": "synthetic",
            "parent_doc": base["doc_id"],
            "target_chunk": anchor_chunk_id,
        })
    return out


def enrich_docs_for_index(records: list[dict], *, llm, embedder, store: EnrichmentStore,
                          recipe: str, limit: int = 0, workers: int = 1,
                          max_hypotheticals: int = MAX_HYPOTHETICALS,
                          progress_every: int = 0) -> dict:
    """Enrich documents and attach payloads + synthetic records.

    - one LLM call per document, persisted to the store immediately (the
      store IS the checkpoint: a crash resumes without re-calling)
    - stored payloads are REATTACHED (no LLM call) -- replay semantics
    - failures are never stored, so they stay retryable on the next run
    - limit counts documents that NEED work, not already-enriched ones
      (Red: --enrich-limit applies to pending docs)
    - limit == 0 means the whole corpus in one bounded, resumable run
    - workers > 1 fans the LLM calls out across threads (the gateway is
      the bottleneck, not the local machine); store writes stay on the
      main thread -- sqlite is a single writer
    - #6 erasure-core, Red review 2026-08-21 (finding #3): a tombstoned
      document (record["deleted"] is True) is never enriched. This is the
      POINT-OF-USE guard the invariant actually needs -- cmd_delete's
      EnrichmentStore.invalidate() call is cleanup for a payload that
      already existed, but does nothing to stop THIS run from creating a
      fresh one for a document tombstoned moments before (or concurrently
      with) this call. Skipped documents are counted, not silently dropped,
      so `alexandria index --enrich`'s own report is honest about why a doc
      it walked past never got a payload.
    """
    stats = {"enriched": 0, "reattached": 0, "failed": 0, "synthetic": 0,
             "suspicious": 0, "skipped_deleted": 0}  # #5/F3e: count of
             # hypotheticals dropped as instruction-shaped, so a monitoring
             # loop observes injection ATTEMPTS, not only whatever survived
             # filtering. skipped_deleted (#6): tombstoned docs never enriched.
    by_doc: dict[str, list[dict]] = {}
    order: list[str] = []
    for record in records:
        if record.get("deleted") is True:
            stats["skipped_deleted"] += 1
            continue
        doc_id = record["doc_id"]
        if doc_id not in by_doc:
            by_doc[doc_id] = []
            order.append(doc_id)
        by_doc[doc_id].append(record)
    next_progress = progress_every
    def _progress() -> None:
        nonlocal next_progress
        done = stats["enriched"] + stats["reattached"] + stats["failed"]
        if progress_every and done >= next_progress:
            print(f"enrich progress: {done} docs "
                  f"({stats['enriched']} new, {stats['reattached']} reattached, "
                  f"{stats['failed']} failed)", flush=True)
            next_progress = done + progress_every
    pending = 0
    _pending: list[str] = []

    def _pending_items():
        for doc_id in _pending:
            yield doc_id, "\n\n".join(r["text"] for r in by_doc[doc_id])

    def _persist(doc_id: str, payload: dict) -> None:
        doc_records = by_doc[doc_id]
        sha = doc_fingerprint(doc_records)
        if not payload or "error" in payload:
            stats["failed"] += 1
            _progress()
            return
        # #5/F3e: surface suspicion BEFORE persisting -- the stored payload
        # already has the filtered (clean) hypotheticals; the count is the
        # observable signal a monitoring loop reads, the filtering already
        # happened in _parse_enrichment.
        stats["suspicious"] += payload.pop("suspicious_count", 0)
        # _apply_payload mutates payload["hypotheticals"] in place (the
        # faithfulness gate, finding #1, may drop entries the pattern filter
        # missed) -- it must run BEFORE store.put(), or a replay from the
        # store would reattach the UNFILTERED list even though the live
        # index only ever saw the filtered one. Persisting the corrected
        # payload keeps get()'s replay semantics honest.
        _apply_payload(doc_records, payload, embedder, records, stats)
        store.put(doc_id, sha, recipe, payload)
        stats["enriched"] += 1
        _progress()

    # Reattached docs first (no LLM, no store writes): attach enrichment to
    # their chunk records immediately so progress lines flow fast.
    for doc_id in order:
        doc_records = by_doc[doc_id]
        sha = doc_fingerprint(doc_records)
        payload = store.get(doc_id, sha, recipe)
        if payload is not None:
            stats["reattached"] += 1
            _apply_payload(doc_records, payload, embedder, records, stats)
            _progress()
        elif limit and pending >= limit:
            break
        else:
            pending += 1
            _pending.append(doc_id)
    # Pending docs stream through the pool; each completed doc is persisted
    # IMMEDIATELY (store is the checkpoint -- a crash mid-pool keeps every
    # completed doc; the write phase is not a barrier).
    def _call(item):
        doc_id, text = item
        return doc_id, enrich_doc(llm, doc_id, text)

    if workers > 1:
        import concurrent.futures as _futures
        with _futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(_call, item): item for item in _pending_items()}
            for fut in _futures.as_completed(futs):
                doc_id, payload = fut.result()  # enrich_doc never raises
                _persist(doc_id, payload)
    else:
        for item in _pending_items():
            doc_id, payload = _call(item)
            _persist(doc_id, payload)
    return stats


# #5/F3c, Red review 2026-08-20 finding #1: the instruction-pattern filter
# in _parse_enrichment only catches hypotheticals that READ like an attack.
# The stronger attack is an ordinary-phrased question aimed at a topic the
# document does not legitimately answer -- it evades framing, escaping, AND
# the pattern filter, because it is not instruction-shaped at all. This
# threshold gates on FAITHFULNESS instead: a hypothetical must be
# semantically close to its own document's content, or it is rejected
# regardless of how plausible it reads. Conservative default -- reject only
# clearly off-topic entries, not merely-loosely-related ones (a document's
# genuinely poor phrasing of its own hypothetical should not be punished
# alongside genuine poisoning).
FAITHFULNESS_MIN_SIMILARITY = 0.10


def _faithful_hypotheticals(hypotheticals: list[str], doc_records: list[dict],
                            embedder) -> tuple[list[str], int]:
    """Drop hypotheticals whose embedding is not close to their own
    document's embedding (Red review 2026-08-20, finding #1). Both sides
    embedded with the SAME method (embed(), document-space) for a fair,
    symmetric comparison -- this is separate from the query-space vectors
    synthetic_records() computes for actual retrieval matching."""
    if not hypotheticals:
        return [], 0
    from .untrusted import cosine_similarity
    doc_text = "\n\n".join(r["text"] for r in doc_records)[:MAX_DOC_CHARS]
    if not doc_text.strip():
        return hypotheticals, 0  # nothing to compare against; do not punish
    doc_vector = embedder.embed([doc_text])[0]
    hyp_vectors = embedder.embed(hypotheticals)
    clean, rejected = [], 0
    for text, vector in zip(hypotheticals, hyp_vectors, strict=True):
        if cosine_similarity(vector, doc_vector) >= FAITHFULNESS_MIN_SIMILARITY:
            clean.append(text)
        else:
            rejected += 1
    return clean, rejected


def _apply_payload(doc_records, payload, embedder, records, stats) -> None:
    """Attach enrichment to chunk records + extend with synthetic vectors."""
    if payload.get("hypotheticals"):
        hypotheticals = payload["hypotheticals"][:MAX_HYPOTHETICALS]
        hypotheticals, unfaithful = _faithful_hypotheticals(hypotheticals, doc_records, embedder)
        if unfaithful:
            stats["suspicious"] = stats.get("suspicious", 0) + unfaithful
        if hypotheticals:
            if hasattr(embedder, "embed_queries"):
                vectors = embedder.embed_queries(hypotheticals)
            else:
                vectors = embedder.embed(hypotheticals)
            anchor = doc_records[0]["chunk_id"]
            records.extend(synthetic_records(doc_records, payload, anchor, vectors))
            stats["synthetic"] += len(hypotheticals)
        payload["hypotheticals"] = hypotheticals  # keep persisted payload consistent
    for record in doc_records:
        record["enrichment"] = json.dumps(payload, sort_keys=True)
    return stats


def recipe_signature(model: str, prompt_version: str = "v1") -> str:
    """Enrichment recipe: model + prompt version. A change in either must
    force re-enrichment (Red: version by recipe, not only doc contents)."""
    return f"{model}@{prompt_version}"


class EnrichmentStore:
    """doc_id -> payload, keyed by fingerprint + recipe.

    Stores the SUCCESSFUL payload, not a boolean (Red: replay semantics --
    a rebuild reattaches the stored enrichment without new LLM calls;
    failures are never stored, so they stay retryable)."""

    def __init__(self, index_dir: str | Path) -> None:
        path = Path(index_dir) / "enrichment.sqlite"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(path)
        self.con.execute(
            "CREATE TABLE IF NOT EXISTS enriched_docs ("
            "doc_id TEXT PRIMARY KEY, sha TEXT NOT NULL, recipe TEXT NOT NULL,"
            " payload TEXT NOT NULL)")
        self.con.commit()

    def get(self, doc_id: str, sha: str, recipe: str) -> dict | None:
        """The stored payload only when BOTH the content fingerprint and the
        enrichment recipe match; otherwise None (doc needs re-enrichment)."""
        row = self.con.execute(
            "SELECT payload FROM enriched_docs WHERE doc_id = ? AND sha = ? "
            "AND recipe = ?", (doc_id, sha, recipe)).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            return None
        return payload if isinstance(payload, dict) else None

    def put(self, doc_id: str, sha: str, recipe: str, payload: dict) -> None:
        self.con.execute(
            "INSERT INTO enriched_docs (doc_id, sha, recipe, payload) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(doc_id) DO UPDATE SET "
            "sha=excluded.sha, recipe=excluded.recipe, payload=excluded.payload",
            (doc_id, sha, recipe, json.dumps(payload, sort_keys=True)))
        self.con.commit()

    def invalidate(self, doc_id: str) -> bool:
        """#5/F3d: force-drop a stored payload independent of content-hash or
        recipe change. Without this, an accepted poisoned payload is sticky
        forever -- get() only refuses a STALE fingerprint/recipe, never a
        payload an operator has since judged bad on an unchanged document.
        Returns True if a row existed and was removed, False otherwise (so a
        caller invalidating a doc that was never enriched gets an honest
        signal rather than a silent no-op)."""
        cur = self.con.execute(
            "DELETE FROM enriched_docs WHERE doc_id = ?", (doc_id,))
        self.con.commit()
        return cur.rowcount > 0

    def count(self) -> int:
        return int(self.con.execute("SELECT COUNT(*) FROM enriched_docs").fetchone()[0])
