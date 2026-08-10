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

ENRICH_SYSTEM = (
    "You enrich a private knowledge-base document for retrieval. Read the "
    "document and return ONE JSON object with exactly these keys:\n"
    '{"summary": string under 40 words capturing the document core '
    "subject and its claims, \"keywords\": array of 4-8 topical keywords, "
    "\"hypotheticals\": array of 3-5 plausible USER QUESTIONS this document "
    "would answer, phrased the way a user would actually ask them, not "
    "using the document's own wording.}\n"
    "Return ONLY the JSON object, no prose, no markdown fences."
)

MAX_DOC_CHARS = 8000
MAX_HYPOTHETICALS = 3


def enrich_doc(llm, doc_id: str, doc_text: str) -> dict[str, Any]:
    """One LLM call; returns {"summary", "keywords", "hypotheticals"} (any
    subset on failure -- the caller attaches whatever came back)."""
    user = f"DOCUMENT id: {doc_id}\n\n{doc_text[:MAX_DOC_CHARS]}"
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
        out["hypotheticals"] = [str(h) for h in (out.get("hypotheticals") or [])][:MAX_HYPOTHETICALS]
        out["summary"] = str(out.get("summary") or "")[:200]
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
    """
    by_doc: dict[str, list[dict]] = {}
    order: list[str] = []
    for record in records:
        doc_id = record["doc_id"]
        if doc_id not in by_doc:
            by_doc[doc_id] = []
            order.append(doc_id)
        by_doc[doc_id].append(record)
    stats = {"enriched": 0, "reattached": 0, "failed": 0, "synthetic": 0}
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
        store.put(doc_id, sha, recipe, payload)
        stats["enriched"] += 1
        _apply_payload(doc_records, payload, embedder, records, stats)
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


def _apply_payload(doc_records, payload, embedder, records, stats) -> None:
    """Attach enrichment to chunk records + extend with synthetic vectors."""
    if payload.get("hypotheticals"):
        hypotheticals = payload["hypotheticals"][:MAX_HYPOTHETICALS]
        if hasattr(embedder, "embed_queries"):
            vectors = embedder.embed_queries(hypotheticals)
        else:
            vectors = embedder.embed(hypotheticals)
        anchor = doc_records[0]["chunk_id"]
        records.extend(synthetic_records(doc_records, payload, anchor, vectors))
        stats["synthetic"] += len(hypotheticals)
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

    def count(self) -> int:
        return int(self.con.execute("SELECT COUNT(*) FROM enriched_docs").fetchone()[0])
