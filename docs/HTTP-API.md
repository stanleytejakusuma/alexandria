# Alexandria HTTP API

The harness-agnostic consumption surface. Any agent harness, script, or
language can talk to Alexandria over plain HTTP; nothing here assumes a
specific agent framework. The CLI (`alexandria search|answer|remember|...`) is
the equivalent offline surface.

**Base URL:** `http://127.0.0.1:8420` (loopback by default). A unix socket may
be exposed per-identity with `serve --unix-socket IDENTITY=PATH` (identity is
then bound to that socket). Non-loopback serving requires
`ALEXANDRIA_SERVE_ALLOW_REMOTE=1`.

**Body limit:** 65536 bytes. **Text fields:** max 4000 chars. **k:** integer 1..50, default 5.

## Identity (read this first)

- TCP callers are always `local-anonymous`. A `caller`/`user` field in the body
  is deliberately **ignored** -- identity is never forgeable over HTTP.
- A unix socket binds identity at bind time: `--unix-socket prime-agent=...`
  attributes every request on that socket as `prime-agent`.
- Attribution appears in the audit trail; it is not an authorization boundary.

## GET /health

Readiness + liveness probe. **This endpoint is slow on cold start** (walks the
source tree to cross-check counts); use a generous timeout.

```json
{
  "status": "ok",                // "ok" | "degraded" | "rebuilding"
  "generation": 67,
  "chunk_count_lancedb": 46387,
  "chunk_count_fts5": 46387,     // independent cross-check
  "chunk_counts_agree": true,    // false = the two projections disagree
  "source_document_count": 33421,
  "distinct_documents_indexed": 33421,
  "source_documents_agree": true, // false = index stopped seeing new files
  "uptime_seconds": 1234.5,
  "liveness_stale": false,
  "liveness_reason": "",
  "oldest_pending_age_seconds": null,   // null when nothing is pending
  "drain_heartbeat_age_seconds": 13.0,  // null if state file unreadable
  "drain_interval_seconds": 600.0
}
```

Semantics:
- `"rebuilding"` returns ONLY `{status, reason, uptime_seconds}` -- the chunk
  counts are absent because they would be lies mid-rebuild. It means
  **alive but not ready**: do not 503-alarm a supervisor on it.
- `"degraded"` means servable but something needs attention (e.g. a stale
  pending entry). Check `liveness_reason`.
- `drain_heartbeat_age_seconds` is the age of the last completed promotion
  drain cycle -- the signal that `remember` writes actually become searchable.
  **Do not alarm while `uptime_seconds < drain_interval_seconds`**: the first
  tick waits a full interval after restart. Otherwise, age
  `> 3 x drain_interval_seconds` means the drain is likely dead.

## POST /search

Body: `{"query": "...", "k": 5, "filters": {"type": "...", "project": "...", "layer": "..."}}`
(filters optional). Returns 200:

```json
{"results": [{"chunk_id": "...", "doc_id": "...", "text": "...",
              "heading_path": "...", "layer": "...", "score": 0.743, "rank": 1}]}
```

- `503` = index momentarily unreadable (writer active / rebuild in progress).
  **Retryable with backoff** -- not an error.

## POST /answer

Body: `{"question": "...", "k": 5, "llm_model": "...", "grader_a_model": "...",
"grader_b_model": "..."}` (models optional; fall back to server env defaults).
**Cold synthesis takes minutes** (~3.5 min observed); budget your timeout
accordingly (the server itself bounds all LLM stages by
`ALEXANDRIA_ANSWER_TIMEOUT`, default 900s, 0 = unlimited).

Three response shapes:

```json
// 200 -- verified, emitted answer
{"emitted": true, "text": "...", "n_claims": 7, "cached": false, "answer_id": "..."}

// 422 -- synthesis FAILED its own judges. NOT retryable, NOT a transport error.
{"emitted": false, "error": "synthesis failed its native checks",
 "failed_claims": ["..."], "answer_id": "..."}

// 503 -- partial-result SALVAGE: budget exhausted mid-judge. The draft is
//        included but was NEVER approved. Do not treat as verified.
{"emitted": false, "salvaged": true, "text": "...", "n_claims": 7,
 "error": "budget exhausted: unaudited draft", "answer_id": "..."}
```

Rule for consumers: branch on `emitted`/`salvaged` fields, never on the HTTP
status alone; never cache a `salvaged` response.

## POST /remember

Body: `{"text": "...", "session": "...", "corrects": "..."}`. `session` and
`corrects` are optional in-band metadata. **Write guard:** the corpus has no
hard delete; consumers should keep remember user-confirmed.

**Five response shapes -- branch on the `status` field, never the HTTP code:**

```json
200 {"status": "promoted", "entry_id": "...", "chunks_written": 2}  // written AND searchable
200 {"status": "duplicate"}                                          // NOTHING was written
202 {"status": "queued", "entry_id": "..."}                          // written, promotes on next drain (NOT yet searchable)
400 {"error": "..."}                                                 // caller's fault (invalid text/metadata)
500 {"error": "failed to write the inbox entry: ..."}                // nothing written
500 {"error": "wrote inbox entry but failed to mark it pending: ..."} // text IS on disk but will NOT auto-promote; run `alexandria promote`/reconcile or it is orphaned
```

After a 202-queued or pending write, searchability is guaranteed by the drain
cycle (default 600s) -- verify via `/health` `drain_heartbeat_age_seconds`.

## Errors

All error responses are `{"error": <string-or-object>}` with a 4xx/5xx status.
Unhandled exceptions never escape the handler (500 "internal error").
