"""SPEC §5: `alexandria serve` -- a stdlib-only HTTP server that holds the
embedding model and index warm.

The fixed cost this amortizes is the ~16s embedding-model cold load (D3),
not the vector store -- LanceDB is an embedded library, not something that
benefits from a daemon on its own. `serve` exists to (a) kill that 16s on
every CLI invocation, (b) provide the read path a second harness on another
host needs (§12's second-host acceptance canary), and (c) let `/remember` run
promote inline instead of waiting for the drain timer.

Deliberately NOT built (§10): TLS, auth beyond filesystem/socket identity,
a process manager, graceful config reload. The CLI must keep working
identically whether this process is running or not (S6) -- `serve` never
becomes the only way to reach the corpus.
"""

from __future__ import annotations

import json
import os
import socket
import socketserver
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import liveness
from .auditlog import AuditLogger
from .cache import read_index_generation
from .config import AppConfig
from .index.store import SCALAR_FIELDS
from .pending import oldest_pending_age
from .promote import promote_pending

MAX_BODY_BYTES = 65536
MAX_TEXT_CHARS = 4000
MIN_K, MAX_K = 1, 50
DEFAULT_K = 5
REMOTE_ENV = "ALEXANDRIA_SERVE_ALLOW_REMOTE"
LOCAL_ANONYMOUS = "local-anonymous"  # §5.2/§5.3: fixed identity for any TCP caller

__all__ = ["ServeContext", "build_serve_context", "dispatch", "bind", "serve",
           "start_drain", "NonLoopbackRefused"]


class NonLoopbackRefused(Exception):
    """§5.2/S2: a non-loopback bind was requested without explicit opt-in."""


class _LockedEngine:
    """Wraps a SearchEngine so every `.search()` call is serialized through
    the shared engine lock, without holding the lock across the LLM calls
    `run_pipeline` makes between searches (§5.4, gate S8: a slow /answer
    must not block a concurrent /search).

    `gather.py` reaches the engine only through `engine.search()`
    (gather.py:74,83), so the synthesis path itself is fully covered. One
    other access exists on the /answer route: `run_answer` writes the cost
    ledger via `engine.logger.log_usage(...)` (cli.py:732), which reaches the
    real engine through `__getattr__` UNLOCKED. That is safe -- QueryLogger
    opens a fresh connection per call with busy_timeout set, and holds no
    shared mutable state -- but it means this wrapper covers `.search()`
    specifically, not "every engine access". A future caller that mutates
    engine state through `__getattr__` would bypass the lock silently."""

    def __init__(self, engine, lock: threading.Lock) -> None:
        self._engine = engine
        self._lock = lock

    def search(self, *args, **kwargs):
        with self._lock:
            return self._engine.search(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._engine, name)


@dataclass
class ServeContext:
    config: AppConfig
    corpus: Path
    engine: Any            # SearchEngine, used directly by /search
    locked_engine: _LockedEngine  # passed to run_pipeline for /answer
    embedder: Any
    store: Any
    lexical: Any
    engine_lock: threading.Lock
    started_monotonic: float
    llm_defaults: dict[str, str]


def build_serve_context(config: AppConfig, corpus: Path) -> ServeContext:
    """Build once at startup, reused by every request. Delegates the
    index-exists and manifest checks to `_build_search_engine` (S9: refuses
    to start with a named error on a provider/manifest mismatch) instead of
    re-implementing them -- the CLI and serve must refuse for the identical
    reason, not two reasons that can drift apart."""
    from .cli import _build_search_engine  # local import: cli imports nothing from serve

    engine = _build_search_engine(config, corpus, corpus_root=corpus, client="serve")
    _warm_embedder(engine.embedder)
    _warm_reranker(engine.reranker)
    lock = threading.Lock()
    return ServeContext(
        config=config, corpus=corpus, engine=engine,
        locked_engine=_LockedEngine(engine, lock),
        embedder=engine.embedder, store=engine.store, lexical=engine.bm25,
        engine_lock=lock, started_monotonic=time.monotonic(),
        llm_defaults={
            "base_url": os.environ.get("ALEXANDRIA_LLM_BASE_URL", "http://127.0.0.1:20128/v1"),
            "api_key_env": os.environ.get("ALEXANDRIA_LLM_KEY_ENV", "ALEXANDRIA_LLM_KEY"),
            "llm_model": os.environ.get("ALEXANDRIA_LLM_MODEL", "deepseek-v4-pro"),
            "grader_a_model": os.environ.get("ALEXANDRIA_GRADER_A_MODEL", "deepseek-v4-flash"),
            "grader_b_model": os.environ.get("ALEXANDRIA_GRADER_B_MODEL", "deepseek-v4-flash"),
            "prompt_version": os.environ.get("ALEXANDRIA_PROMPT_VERSION", "v1"),
        },
    )


def _warm_embedder(embedder) -> None:
    """Load the embedding model during startup, not on the first request.

    Measured after the first launchd start (2026-08-13): first query 26.29s,
    every query after it 0.03s. "Always on" was not "always warm", so the
    first user after every reboot paid the whole cold load -- the exact cost
    serve exists to amortize.

    Goes to the provider DIRECTLY. The startup manifest check does embed a
    probe, but through CachedEmbedder, which serves it from the on-disk
    embedding cache and never touches the provider -- which is precisely why
    serve stayed cold despite already embedding something at startup. Any
    warm-up routed through the cache is a no-op on the second start.

    A failure here is not caught: an engine whose model cannot load answers
    nothing, and refusing to start with the real error is the same choice S9
    already makes for a provider/manifest mismatch.
    """
    getattr(embedder, "provider", embedder).embed(["warm the embedding model"])


def _warm_reranker(reranker) -> None:
    """Load the reranker model too -- the embedder was only half the cold path.

    Measured live on 2026-08-13, immediately after `_warm_embedder` shipped:
    first novel query 16.11s, second 2.14s, third 0.80s. The embedder warm-up
    was working; `CrossEncoderReranker` simply loads its own ~90MB model
    lazily on the first *search*, so the first user after every restart still
    paid a cold load while startup reported ready.

    Warming component-by-component is whack-a-mole, so the TEST pins the
    invariant (nothing in the query path is still lazy after startup) rather
    than this function -- a fourth lazy component should fail the test, not
    slip through because only the two known ones are listed here.

    Best-effort, unlike the embedder. `search.py` is explicitly
    failure-tolerant on reranking: a reranker that cannot load degrades
    ranking but still answers, so refusing to start would trade a real
    outage for a latency problem. An embedder that cannot load answers
    nothing, which is why that one is allowed to kill startup.
    """
    from .retrieval.rerank import RerankCandidate
    try:
        reranker.rerank("warm", [RerankCandidate("warm", "warm the reranker model", 0.0)], 1)
    except Exception:
        pass


def start_drain(ctx: ServeContext, *,
                interval: float = liveness.DEFAULT_DRAIN_INTERVAL_SECONDS) -> threading.Event:
    """§11's offline drain, actually running.

    Until this existed the interval was only a threshold liveness.check()
    judged against: `promote_pending` had exactly two callers -- serve's
    inline /remember, and `alexandria promote` by hand -- so anything
    remembered through the CLI stayed invisible to search indefinitely
    (2026-08-13: nine entries, 3.3 hours, reported by /health as degraded
    against a drain nothing implemented).

    Takes `ctx.engine_lock` for the same reason the inline promote does: two
    writers in one process is the concurrency bug the write path was hardened
    against. `promote_pending`'s own flock still guards other processes, and
    its `skipped_locked` return is a normal outcome (W5), not an error --
    the entry stays pending and the next tick takes it.

    Returns the stop Event so a caller can end the loop; the thread is a
    daemon either way, so it never keeps the process alive.
    """
    stop = threading.Event()

    def loop() -> None:
        while not stop.wait(interval):
            try:
                with ctx.engine_lock:
                    result = promote_pending(ctx.corpus, ctx.config, ctx.embedder,
                                             ctx.store, ctx.lexical)
                if result.skipped_locked:
                    # Another process holds the write lock. Nothing ran, so
                    # nothing may be recorded as a successful cycle.
                    continue
                liveness.record_success(ctx.corpus, promoted_count=len(result.promoted),
                                        generation=read_index_generation(ctx.corpus))
                for message in result.errors:
                    print(f"alexandria drain: {message}", file=sys.stderr)
            except Exception:
                # A drain that dies on the first transient error recreates,
                # silently, the exact bug it was written to fix. Report it and
                # take the next tick.
                traceback.print_exc()

    threading.Thread(target=loop, name="alexandria-drain", daemon=True).start()
    return stop


def _json_error(status: int, message: str) -> tuple[int, bytes, str]:
    return status, json.dumps({"error": message}).encode(), "application/json"


def _json_ok(status: int, payload: dict) -> tuple[int, bytes, str]:
    return status, json.dumps(payload).encode(), "application/json"


def _source_document_count(corpus: Path) -> int:
    """An independent walk of sources/+wiki/ ON DISK -- not derived from
    either index. Counts markdown FILES, not chunks: re-chunking here would
    duplicate the cost /health exists to let callers avoid paying, and a
    per-document count is what a distinct-doc_id count from either index
    can be meaningfully compared against.

    Counts only what the indexer would actually ingest, via the shared
    predicate -- a walk with its own idea of which files count reports a
    permanent phantom shortfall and the boolean below is then false forever."""
    from .index.chunker import INDEX_ROOTS, is_indexable_source

    count = 0
    for sub in INDEX_ROOTS:
        base = corpus / sub
        if base.is_dir():
            count += sum(1 for p in base.rglob("*.md")
                         if is_indexable_source(p.relative_to(corpus)))
    return count


def _health_payload(ctx: ServeContext) -> dict:
    """S1: the reported chunk count is cross-checked against TWO
    independent sources -- FTS5's row count, and a walk of sources/+wiki/ on
    disk -- rather than echoed from the single LanceDB handle the request
    already trusted. FTS5 agreement catches the write-path failure gate W3
    exercises (the two derived indexes disagreeing); the source-document
    walk catches a different failure entirely: an index that is INTERNALLY
    consistent (Lance and FTS5 agree with each other) but has silently
    stopped seeing new or deleted documents on disk -- something no
    LanceDB-vs-FTS5 comparison could ever detect, since both are built from
    the same walk."""
    lance_count = ctx.store.count()
    fts_count = ctx.lexical.connection.execute("SELECT COUNT(*) FROM chunk_metadata").fetchone()[0]
    # chunk_metadata has no doc_id column (index/bm25.py's schema); chunk_id
    # is `{doc_id}#{10-hex-hash}` (index/chunker.py), so doc_id is derived by
    # stripping the hash suffix rather than adding a column just for this.
    chunk_ids = ctx.lexical.connection.execute("SELECT chunk_id FROM chunk_metadata").fetchall()
    distinct_docs_indexed = len({row[0].rsplit("#", 1)[0] for row in chunk_ids})
    source_doc_count = _source_document_count(ctx.corpus)
    live = liveness.check(ctx.corpus)
    return {
        "status": "degraded" if live.stale else "ok",
        "generation": read_index_generation(ctx.corpus),
        "chunk_count_lancedb": lance_count,
        "chunk_count_fts5": fts_count,
        "chunk_counts_agree": lance_count == fts_count,
        "source_document_count": source_doc_count,
        "distinct_documents_indexed": distinct_docs_indexed,
        "source_documents_agree": source_doc_count == distinct_docs_indexed,
        "uptime_seconds": round(time.monotonic() - ctx.started_monotonic, 1),
        "liveness_stale": live.stale,
        "liveness_reason": live.reason,
        "oldest_pending_age_seconds": live.oldest_pending_age_seconds,
    }


def _validate_text(payload: dict, field: str) -> tuple[str | None, str | None]:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        return None, f"{field} is required and must be a non-empty string"
    if len(value) > MAX_TEXT_CHARS:
        return None, f"{field} exceeds {MAX_TEXT_CHARS} characters"
    return value, None


def _validate_k(payload: dict) -> tuple[int | None, str | None]:
    if "k" not in payload:
        return DEFAULT_K, None
    k = payload["k"]
    if not isinstance(k, int) or isinstance(k, bool) or not (MIN_K <= k <= MAX_K):
        return None, f"k must be an integer between {MIN_K} and {MAX_K}"
    return k, None


def _validate_filters(payload: dict) -> tuple[dict | None, str | None]:
    filters = payload.get("filters")
    if filters is None:
        return {}, None
    if not isinstance(filters, dict):
        return None, "filters must be an object"
    for key in filters:
        if key not in SCALAR_FIELDS:
            return None, f"unknown filter key: {key}"
    return filters, None


def _handle_search(ctx: ServeContext, identity: str, payload: dict) -> tuple[int, bytes, str]:
    query, err = _validate_text(payload, "query")
    if err:
        return _json_error(400, err)
    k, err = _validate_k(payload)
    if err:
        return _json_error(400, err)
    filters, err = _validate_filters(payload)
    if err:
        return _json_error(400, err)
    t0 = time.time()
    with ctx.engine_lock:
        results = ctx.engine.search(query, k=k, filters=filters)
    AuditLogger(ctx.corpus).search(query=query, k=k, latency_ms=int((time.time() - t0) * 1000),
                                   hits=len(results), caller=identity, user=None,
                                   cache_hit=ctx.engine.last_cache_hit)
    return _json_ok(200, {"results": [
        {"chunk_id": r.chunk_id, "doc_id": r.doc_id, "text": r.text,
         "heading_path": r.heading_path, "layer": r.layer, "score": r.score, "rank": r.rank}
        for r in results]})


def _handle_answer(ctx: ServeContext, identity: str, payload: dict) -> tuple[int, bytes, str]:
    from .cli import run_answer

    question, err = _validate_text(payload, "question")
    if err:
        return _json_error(400, err)
    k, err = _validate_k(payload)
    if err:
        return _json_error(400, err)
    defaults = ctx.llm_defaults
    outcome = run_answer(
        ctx.config, ctx.corpus, question, engine=ctx.locked_engine, k=k,
        llm_model=payload.get("llm_model") or defaults["llm_model"],
        grader_a_model=payload.get("grader_a_model") or defaults["grader_a_model"],
        grader_b_model=payload.get("grader_b_model") or defaults["grader_b_model"],
        base_url=defaults["base_url"], api_key_env=defaults["api_key_env"],
        prompt_version=defaults["prompt_version"],
        # §5.3: identity is derived from the socket, never from the body --
        # a "caller"/"user" field in the request is not honored here.
        caller=identity, user=identity,
    )
    if not outcome.emitted:
        return _json_ok(422, {"emitted": False, "error": outcome.error,
                              "failed_claims": outcome.failed_claims or [],
                              "answer_id": outcome.answer_id})
    return _json_ok(200, {"emitted": True, "text": outcome.text, "n_claims": outcome.n_claims,
                          "cached": outcome.cached, "answer_id": outcome.answer_id})


def _handle_remember(ctx: ServeContext, identity: str, payload: dict) -> tuple[int, bytes, str]:
    """§4/S0: the headline claim, end to end. Inline promote runs under the
    same non-blocking write lock the CLI drain uses (`promote_pending`'s own
    flock); if a concurrent process holds it, this makes one bounded retry
    (contention is expected to be brief -- a few embed+upsert calls) rather
    than failing the request or falling back to a queue this package does
    not build."""
    from .cli import append_inbox_entry

    text, err = _validate_text(payload, "text")
    if err:
        return _json_error(400, err)
    # §5.3: identity comes from the socket (`identity`), never the body. The
    # remaining body fields are still attacker-controlled and land in the
    # inbox's in-band metadata, so append_inbox_entry validates them and can
    # refuse -- a refusal is the caller's fault, hence 400 not 500.
    result = append_inbox_entry(ctx.corpus, text, from_=identity,
                                session=payload.get("session"), corrects=payload.get("corrects"))
    if result.status == "invalid":
        return _json_error(400, result.error)
    if result.status == "duplicate":
        return _json_ok(200, {"status": "duplicate"})
    if result.status == "inbox_write_failed":
        return _json_error(500, f"failed to write the inbox entry: {result.error}")
    if result.status == "marker_failed":
        return _json_error(500, f"wrote inbox entry but failed to mark it pending: {result.error}")

    entry_id = result.entry.entry_id
    promote_result = None
    for attempt in range(2):
        with ctx.engine_lock:
            promote_result = promote_pending(ctx.corpus, ctx.config, ctx.embedder, ctx.store,
                                             ctx.lexical, entry_ids=[entry_id])
        if not promote_result.skipped_locked:
            break
        time.sleep(0.2 * (attempt + 1))

    if promote_result.skipped_locked:
        return _json_ok(202, {"status": "queued", "entry_id": entry_id,
                              "note": "write lock held by another process; will promote on the next drain"})
    liveness.record_success(ctx.corpus, promoted_count=len(promote_result.promoted),
                            generation=read_index_generation(ctx.corpus))
    if promote_result.errors:
        return _json_error(500, f"promote failed: {'; '.join(promote_result.errors)}")
    return _json_ok(200, {"status": "promoted", "entry_id": entry_id,
                          "chunks_written": promote_result.chunks_written})


def dispatch(ctx: ServeContext, identity: str, method: str, path: str, body: bytes) -> tuple[int, bytes, str]:
    """Pure request handler -- no socket I/O. Returns (status, body_bytes,
    content_type). Kept separate from the socketserver plumbing so most
    behavior (validation, routing, identity attribution) is testable without
    binding a real port (S5, S7's logic; S0/S2/S8/S9 need a real bound
    server and get one in test_serve.py)."""
    if method == "GET" and path == "/health":
        return _json_ok(200, _health_payload(ctx))
    if method != "POST":
        return _json_error(404, "not found")
    if len(body) > MAX_BODY_BYTES:
        return _json_error(413, "request body too large")
    try:
        payload = json.loads(body) if body else {}
    except json.JSONDecodeError:
        return _json_error(400, "malformed JSON")
    if not isinstance(payload, dict):
        return _json_error(400, "expected a JSON object")
    if path == "/search":
        return _handle_search(ctx, identity, payload)
    if path == "/answer":
        return _handle_answer(ctx, identity, payload)
    if path == "/remember":
        return _handle_remember(ctx, identity, payload)
    return _json_error(404, "not found")


def _make_handler_class(ctx: ServeContext, identity: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _respond(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            self._dispatch_safely("GET", b"")

        def do_POST(self) -> None:
            # Content-Length is attacker-controlled and was previously parsed
            # OUTSIDE _dispatch_safely, so a non-numeric value raised through
            # BaseHTTPRequestHandler and closed the socket with no response --
            # indistinguishable from a network failure. A negative value was
            # worse: it passed the `> MAX_BODY_BYTES` test and then reached
            # rfile.read(-1), which reads to EOF and bypasses the cap entirely.
            raw = self.headers.get("Content-Length", "") or "0"
            try:
                length = int(raw)
            except ValueError:
                length = -1
            if length < 0 or length > MAX_BODY_BYTES:
                status, msg = ((413, "request body too large") if length > MAX_BODY_BYTES
                               else (400, "invalid Content-Length"))
                self._respond(status, json.dumps({"error": msg}).encode(), "application/json")
                # The body is still unread in the socket; with keep-alive the
                # residue would be parsed as the next request line.
                self.close_connection = True
                return
            body = self.rfile.read(length) if length else b""
            self._dispatch_safely("POST", body)

        def _dispatch_safely(self, method: str, body: bytes) -> None:
            # An unhandled exception inside dispatch() must never abort the
            # connection silently (BaseHTTPRequestHandler's default behavior
            # on an uncaught exception is to close the socket with no
            # response, which looks identical to a network failure from the
            # caller's side -- undiagnosable). Always write SOME response.
            try:
                status, resp_body, ctype = dispatch(ctx, identity, method, self.path, body)
            except Exception:
                traceback.print_exc()
                status = 500
                resp_body = json.dumps({"error": "internal error"}).encode()
                ctype = "application/json"
            self._respond(status, resp_body, ctype)

        def log_message(self, fmt: str, *args) -> None:  # quiet by default; CLI stays the log surface
            pass

    return Handler


class UnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    """A threading HTTP-over-Unix-socket server. `http.server.HTTPServer`
    assumes an (host, port) address for its bind/server_name logic, which
    breaks for AF_UNIX -- this subclass skips that and binds a plain path."""
    daemon_threads = True
    allow_reuse_address = True


class TCPHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def bind(corpus: str | Path, *, config: AppConfig | None = None, host: str = "127.0.0.1",
        port: int = 8420, unix_sockets: dict[str, str] | None = None,
        allow_remote: bool | None = None) -> tuple[ServeContext, TCPHTTPServer, list[UnixHTTPServer]]:
    """Build the context and bind every listener without blocking. Split out
    of `serve()` so tests (and any future process supervisor) can control
    the serve_forever lifecycle explicitly -- start each server in its own
    thread, make requests against the now-known bound address, then shut
    down cleanly, none of which a single blocking call allows.

    §5.2/S2: a non-loopback `host` requires `ALEXANDRIA_SERVE_ALLOW_REMOTE=1`
    (or `allow_remote=True` explicitly) -- fail closed, since the failure
    being prevented is a default-open port serving a private corpus."""
    corpus = Path(corpus).expanduser()
    from .config import load_config
    cfg = config or load_config(corpus_override=corpus)
    if allow_remote is None:
        allow_remote = os.environ.get(REMOTE_ENV) == "1"
    if host not in ("127.0.0.1", "localhost", "::1") and not allow_remote:
        raise NonLoopbackRefused(
            f"refusing to bind {host}: set {REMOTE_ENV}=1 to allow a non-loopback bind")

    ctx = build_serve_context(cfg, corpus)
    # Started here, not in serve(), because bind() is the single chokepoint
    # both the blocking entry point and every test go through -- a drain wired
    # only into serve() would be exercised by nothing.
    start_drain(ctx)

    tcp_handler = _make_handler_class(ctx, LOCAL_ANONYMOUS)
    tcp_server = TCPHTTPServer((host, port), tcp_handler)

    uds_servers: list[UnixHTTPServer] = []
    for identity, sock_path in (unix_sockets or {}).items():
        sock_path = str(Path(sock_path).expanduser())
        if os.path.exists(sock_path):
            os.unlink(sock_path)
        handler = _make_handler_class(ctx, identity)
        uds_servers.append(UnixHTTPServer(sock_path, handler))

    return ctx, tcp_server, uds_servers


def serve(corpus: str | Path, *, config: AppConfig | None = None, host: str = "127.0.0.1",
         port: int = 8420, unix_sockets: dict[str, str] | None = None,
         allow_remote: bool | None = None) -> None:
    """Blocking entry point: bind every configured listener and run until
    interrupted."""
    _ctx, tcp_server, uds_servers = bind(corpus, config=config, host=host, port=port,
                                         unix_sockets=unix_sockets, allow_remote=allow_remote)
    servers: list[socketserver.BaseServer] = [tcp_server, *uds_servers]
    threads: list[threading.Thread] = []

    print(f"alexandria serve: listening on {host}:{port}"
          + (f" + {len(uds_servers)} unix socket(s)" if uds_servers else ""), file=sys.stderr)

    for server in uds_servers:
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        threads.append(t)
    try:
        tcp_server.serve_forever()
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
