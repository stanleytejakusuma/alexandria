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
    '{"summary": string under 40 words capturing the document\\'s core '
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
    """Cheap content fingerprint of a doc's chunk texts (for skip-if-unchanged)."""
    return hashlib.sha256(
        "\x1f".join(r["text"] for r in doc_records).encode()).hexdigest()[:16]


class EnrichmentStore:
    """doc_id -> {sha, payload} so re-indexes skip already-enriched docs."""

    def __init__(self, index_dir: str | Path) -> None:
        path = Path(index_dir) / "enrichment.sqlite"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(path)
        self.con.execute(
            "CREATE TABLE IF NOT EXISTS enriched_docs ("
            "doc_id TEXT PRIMARY KEY, sha TEXT NOT NULL, payload TEXT NOT NULL)")
        self.con.commit()

    def is_enriched(self, doc_id: str, sha: str) -> bool:
        row = self.con.execute(
            "SELECT 1 FROM enriched_docs WHERE doc_id = ? AND sha = ?",
            (doc_id, sha)).fetchone()
        return row is not None

    def put(self, doc_id: str, sha: str, payload: dict) -> None:
        self.con.execute(
            "INSERT INTO enriched_docs (doc_id, sha, payload) VALUES (?, ?, ?) "
            "ON CONFLICT(doc_id) DO UPDATE SET sha=excluded.sha, payload=excluded.payload",
            (doc_id, sha, json.dumps(payload, sort_keys=True)))
        self.con.commit()

    def count(self) -> int:
        return int(self.con.execute("SELECT COUNT(*) FROM enriched_docs").fetchone()[0])
