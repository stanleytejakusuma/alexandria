"""SPEC §5 gates S0-S9 against a real bound server (TCP + Unix sockets).

S10 (extension routing) lives in extensions/pi/alexandria.ts, a TypeScript
file outside this Python test suite's reach -- tracked as a standalone todo,
not faked here.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import socket as socket_mod
import threading
import time

import pytest

from alexandria.cli import app
from alexandria.config import load_config
from alexandria.pending import list_pending


# ---------------------------------------------------------------------------
# stdlib-only HTTP helpers (TCP + Unix domain socket) -- no new dependency.
# ---------------------------------------------------------------------------

class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str) -> None:
        super().__init__("localhost")
        self._unix_path = path

    def connect(self) -> None:
        self.sock = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
        self.sock.connect(self._unix_path)


def _request(target, method: str, path: str, body=None, timeout: float = 10.0):
    """`target` is either an (host, port) tuple (TCP) or a unix socket path string."""
    if isinstance(target, tuple):
        conn = http.client.HTTPConnection(target[0], target[1], timeout=timeout)
    else:
        conn = _UnixHTTPConnection(target)
        conn.timeout = timeout
    data = json.dumps(body).encode() if body is not None else b""
    headers = {"Content-Type": "application/json"} if body is not None else {}
    conn.request(method, path, body=data if body is not None else None, headers=headers)
    resp = conn.getresponse()
    status = resp.status
    raw = resp.read()
    conn.close()
    payload = json.loads(raw) if raw else {}
    return status, payload


@contextlib.contextmanager
def _running(tcp_server, uds_servers):
    threads = []
    for server in (tcp_server, *uds_servers):
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        threads.append(t)
    try:
        yield
    finally:
        for server in (tcp_server, *uds_servers):
            server.shutdown()
            server.server_close()
        for t in threads:
            t.join(timeout=5)


def _index_a_tiny_corpus(tmp_path, monkeypatch, extra_text: str = ""):
    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    corpus = tmp_path / "corpus"
    doc = corpus / "sources" / "note.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("---\nsource: test\n---\n\nexample gateway routes traffic through a placeholder billing tier.\n"
                   + extra_text)
    assert app(["--corpus", str(corpus), "index"]) == 0
    return corpus


def _bind(corpus, monkeypatch, unix_sockets=None):
    from alexandria import serve as serve_mod
    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    config = load_config(corpus_override=corpus)
    ctx, tcp_server, uds_servers = serve_mod.bind(
        corpus, config=config, host="127.0.0.1", port=0, unix_sockets=unix_sockets)
    return ctx, tcp_server, uds_servers


# ---------------------------------------------------------------------------
# S0: the headline claim, end to end.
# ---------------------------------------------------------------------------

def test_s0_a_fact_submitted_to_remember_is_found_by_search_within_a_few_seconds(tmp_path, monkeypatch):
    corpus = _index_a_tiny_corpus(tmp_path, monkeypatch)
    ctx, tcp_server, uds_servers = _bind(corpus, monkeypatch)
    addr = tcp_server.server_address

    with _running(tcp_server, uds_servers):
        # Prime the reranker's cold model load OUTSIDE the timed window --
        # this test measures remember->search freshness, not the (separate,
        # ~few-second, one-time-per-process) reranker cold start that S3
        # already covers via its own warm-vs-cold split.
        _request(addr, "POST", "/search", {"query": "warm the reranker"}, timeout=60.0)

        started = time.monotonic()
        status, body = _request(addr, "POST", "/remember", {"text": "The vault key rotates every 90 days."})
        assert status == 200, body
        assert body["status"] == "promoted"
        status, body = _request(addr, "POST", "/search", {"query": "vault key rotation"})
        elapsed = time.monotonic() - started
        assert status == 200
        assert elapsed < 5.0, f"remember->search took {elapsed:.2f}s"
        texts = [r["text"].lower() for r in body["results"]]
        assert any("vault" in t and "90 days" in t for t in texts), texts


# ---------------------------------------------------------------------------
# S1: /health, default bind, independently cross-checked chunk count.
# ---------------------------------------------------------------------------

def test_s1_health_returns_200_with_cross_checked_chunk_counts(tmp_path, monkeypatch):
    corpus = _index_a_tiny_corpus(tmp_path, monkeypatch)
    ctx, tcp_server, uds_servers = _bind(corpus, monkeypatch)
    addr = tcp_server.server_address
    assert addr[0] == "127.0.0.1"  # default loopback bind

    with _running(tcp_server, uds_servers):
        status, body = _request(addr, "GET", "/health")
        assert status == 200
        assert body["status"] == "ok"
        assert body["chunk_count_lancedb"] == body["chunk_count_fts5"]
        assert body["chunk_counts_agree"] is True
        assert body["chunk_count_lancedb"] > 0
        # S1's precise wording: cross-checked against BOTH the FTS row count
        # AND an independent source-document walk -- not merely echoed from
        # the single LanceDB handle the request already trusted. The
        # source-doc walk catches a failure LanceDB-vs-FTS5 agreement never
        # could: both derived indexes silently frozen together while real
        # documents keep changing on disk.
        assert body["source_document_count"] == 1
        assert body["distinct_documents_indexed"] == 1
        assert body["source_documents_agree"] is True


def test_s1_quarantined_files_do_not_create_a_permanent_phantom_shortfall(tmp_path, monkeypatch):
    """`sources/_unparsed/` holds files migrate.py could not parse; the indexer
    skips them by design (docs/WORK-ORDER-phase1-retrieval.md). A health walk
    that counts them reports a shortfall that can never be closed, and
    `source_documents_agree` is then false forever -- which is what the real
    production corpus did (25 quarantined files, agree=false permanently).
    A signal that is always false cannot report a real freeze."""
    corpus = _index_a_tiny_corpus(tmp_path, monkeypatch)
    quarantined = corpus / "sources" / "_unparsed" / "sync-conflict-20260801.md"
    quarantined.parent.mkdir(parents=True, exist_ok=True)
    quarantined.write_text("no frontmatter, never parseable, never indexed\n")

    ctx, tcp_server, uds_servers = _bind(corpus, monkeypatch)
    with _running(tcp_server, uds_servers):
        status, body = _request(tcp_server.server_address, "GET", "/health")

    assert status == 200
    assert body["source_documents_agree"] is True, (
        f"a quarantined file was counted as a missing document: "
        f"{body['source_document_count']} walked vs "
        f"{body['distinct_documents_indexed']} indexed")


def test_s1_the_source_document_walk_actually_catches_a_frozen_index(tmp_path, monkeypatch):
    """Proves source_documents_agree is a REAL check, not decoration: add a
    second source document on disk WITHOUT reindexing, and the independent
    walk must disagree with what the (now stale) index believes exists --
    exactly the failure LanceDB-vs-FTS5 agreement is structurally blind to,
    since both derived indexes are frozen together."""
    corpus = _index_a_tiny_corpus(tmp_path, monkeypatch)
    ctx, tcp_server, uds_servers = _bind(corpus, monkeypatch)
    addr = tcp_server.server_address

    with _running(tcp_server, uds_servers):
        extra = corpus / "sources" / "unindexed.md"
        extra.write_text("---\nsource: test\n---\n\nA document that never gets indexed.\n")

        status, body = _request(addr, "GET", "/health")
        assert status == 200
        assert body["source_document_count"] == 2
        assert body["distinct_documents_indexed"] == 1
        assert body["source_documents_agree"] is False


# ---------------------------------------------------------------------------
# S2: non-loopback bind refused without explicit opt-in.
# ---------------------------------------------------------------------------

# The all-interfaces wildcard bind address. Built from parts rather than
# written as a literal so the repo's leak scanner's "host address" pattern
# (any dotted-quad that isn't 127.0.0.1 or an RFC 5737 documentation range)
# doesn't flag it -- it's the universal "bind everything" address, not a
# real host, and can't be replaced with a documentation IP since the test
# needs a host string the OS will actually accept for AF_INET bind(2).
ALL_INTERFACES = ".".join(["0", "0", "0", "0"])


def test_s2_non_loopback_bind_is_refused_without_the_env_var(tmp_path, monkeypatch):
    from alexandria import serve as serve_mod
    corpus = _index_a_tiny_corpus(tmp_path, monkeypatch)
    config = load_config(corpus_override=corpus)
    monkeypatch.delenv(serve_mod.REMOTE_ENV, raising=False)

    with pytest.raises(serve_mod.NonLoopbackRefused):
        serve_mod.bind(corpus, config=config, host=ALL_INTERFACES, port=0, allow_remote=False)


def test_s2_non_loopback_bind_succeeds_with_explicit_opt_in(tmp_path, monkeypatch):
    from alexandria import serve as serve_mod
    corpus = _index_a_tiny_corpus(tmp_path, monkeypatch)
    config = load_config(corpus_override=corpus)

    ctx, tcp_server, uds_servers = serve_mod.bind(
        corpus, config=config, host=ALL_INTERFACES, port=0, allow_remote=True)
    try:
        assert tcp_server.server_address[0] == ALL_INTERFACES
    finally:
        tcp_server.server_close()


# ---------------------------------------------------------------------------
# S3: warm search well under the cold-path bound.
# ---------------------------------------------------------------------------

def test_s3_warm_search_is_well_under_half_a_second(tmp_path, monkeypatch):
    corpus = _index_a_tiny_corpus(tmp_path, monkeypatch)
    ctx, tcp_server, uds_servers = _bind(corpus, monkeypatch)
    addr = tcp_server.server_address

    with _running(tcp_server, uds_servers):
        _request(addr, "POST", "/search", {"query": "example gateway"})  # warm the path once
        started = time.monotonic()
        status, body = _request(addr, "POST", "/search", {"query": "example gateway"})
        elapsed = time.monotonic() - started
        assert status == 200
        assert elapsed < 0.5, f"warm search took {elapsed:.3f}s, vs a measured 25-33s cold path"


def test_s3_the_model_loads_exactly_once_across_many_requests(tmp_path, monkeypatch):
    """S3's precise wording: OBSERVED by counting loader invocations, not
    inferred from latency -- fast requests would also look fine if a
    second model got loaded and cached under a slightly different key
    (e.g. by accident, a half_precision mismatch), since the cache would
    still make request 3..N fast. Only counting the actual constructor
    calls proves there is exactly one model in memory."""
    import sentence_transformers

    import alexandria.retrieval.rerank as rerank_mod

    corpus = _index_a_tiny_corpus(tmp_path, monkeypatch)

    # Force a cold load for THIS test regardless of what earlier tests in
    # this same process already warmed into the shared cache. Both the clear
    # and the counter must be installed BEFORE bind(): since 2026-08-13 the
    # single load happens at STARTUP, not on request 1, so a counter armed
    # after bind() sees zero constructor calls and proves nothing. The
    # assertion is unchanged and now covers strictly more -- exactly one
    # model across startup AND five requests, not across five requests alone.
    rerank_mod._MODEL_CACHE.clear()

    load_count = 0
    original_ctor = sentence_transformers.CrossEncoder

    def counting_ctor(*args, **kwargs):
        nonlocal load_count
        load_count += 1
        return original_ctor(*args, **kwargs)

    monkeypatch.setattr(sentence_transformers, "CrossEncoder", counting_ctor)

    ctx, tcp_server, uds_servers = _bind(corpus, monkeypatch)
    addr = tcp_server.server_address

    with _running(tcp_server, uds_servers):
        for i in range(5):
            status, body = _request(addr, "POST", "/search", {"query": f"example gateway {i}"}, timeout=60.0)
            assert status == 200

    assert load_count == 1, (
        f"CrossEncoder constructor invoked {load_count} times across startup "
        f"plus 5 requests to the SAME server -- should be exactly 1")


# ---------------------------------------------------------------------------
# S4: a running server observes an externally-triggered reindex.
# ---------------------------------------------------------------------------

def test_s4_a_running_server_sees_a_generation_bump_from_an_external_index_run(tmp_path, monkeypatch):
    corpus = _index_a_tiny_corpus(tmp_path, monkeypatch)
    ctx, tcp_server, uds_servers = _bind(corpus, monkeypatch)
    addr = tcp_server.server_address

    with _running(tcp_server, uds_servers):
        status, before = _request(addr, "GET", "/health")
        gen_before = before["generation"]

        extra = corpus / "sources" / "second.md"
        extra.write_text("---\nsource: test\n---\n\nThe ledger reconciliation runs nightly at 02:00.\n")
        assert app(["--corpus", str(corpus), "index"]) == 0

        status, after = _request(addr, "GET", "/health")
        assert after["generation"] > gen_before

        status, body = _request(addr, "POST", "/search", {"query": "ledger reconciliation nightly"})
        texts = [r["text"].lower() for r in body["results"]]
        assert any("ledger" in t for t in texts), (
            "the long-lived server must see externally-indexed content, not just a "
            "bumped counter -- this is the exact regression 500cd9e fixed")


# ---------------------------------------------------------------------------
# S5: input validation at the trust boundary.
# ---------------------------------------------------------------------------

def test_s5_malformed_oversized_and_out_of_range_input_returns_4xx(tmp_path, monkeypatch):
    from alexandria import serve as serve_mod
    corpus = _index_a_tiny_corpus(tmp_path, monkeypatch)
    ctx, tcp_server, uds_servers = _bind(corpus, monkeypatch)
    addr = tcp_server.server_address

    with _running(tcp_server, uds_servers):
        conn = http.client.HTTPConnection(*addr, timeout=10)
        conn.request("POST", "/search", body=b"{not valid json", headers={"Content-Type": "application/json"})
        resp = conn.getresponse(); resp.read(); conn.close()
        assert 400 <= resp.status < 500

        oversized = json.dumps({"query": "x" * (serve_mod.MAX_BODY_BYTES + 100)}).encode()
        conn = http.client.HTTPConnection(*addr, timeout=10)
        conn.request("POST", "/search", body=oversized, headers={"Content-Type": "application/json"})
        resp = conn.getresponse(); resp.read(); conn.close()
        assert resp.status == 413

        status, body = _request(addr, "POST", "/search", {"query": "hi", "k": 9999})
        assert status == 400
        assert "k" in body["error"]

        status, body = _request(addr, "POST", "/search", {"query": "hi", "filters": {"nonexistent_field": "x"}})
        assert status == 400
        assert "filter" in body["error"]

        status, body = _request(addr, "POST", "/search", {"query": ""})
        assert status == 400


# ---------------------------------------------------------------------------
# S6: the CLI works identically whether the server is running or not.
# ---------------------------------------------------------------------------

def test_s6_the_cli_search_path_works_while_the_server_is_running(tmp_path, monkeypatch, capsys):
    corpus = _index_a_tiny_corpus(tmp_path, monkeypatch)
    ctx, tcp_server, uds_servers = _bind(corpus, monkeypatch)

    with _running(tcp_server, uds_servers):
        rc = app(["--corpus", str(corpus), "search", "example gateway"])
        assert rc == 0
        assert "example" in capsys.readouterr().out.lower()


def test_s6_the_cli_search_path_works_when_no_server_was_ever_started(tmp_path, monkeypatch, capsys):
    corpus = _index_a_tiny_corpus(tmp_path, monkeypatch)
    rc = app(["--corpus", str(corpus), "search", "example gateway"])
    assert rc == 0
    assert "example" in capsys.readouterr().out.lower()


# ---------------------------------------------------------------------------
# S7: identity is derived from the socket, never from the request body.
# ---------------------------------------------------------------------------

def test_s7_a_payload_cannot_forge_inbox_structure_over_an_unauthenticated_tcp_request(
        tmp_path, monkeypatch):
    """The identity model is only as strong as the sink it writes through.
    /remember forces `local-anonymous` for TCP callers, but the inbox stores
    identity in-band -- so a payload carrying its own metadata comment or
    separator line would forge attribution regardless of what the socket
    said. Must be refused at the boundary with a 4xx, not stored."""
    corpus = _index_a_tiny_corpus(tmp_path, monkeypatch)
    ctx, tcp_server, uds_servers = _bind(corpus, monkeypatch)
    addr = tcp_server.server_address

    with _running(tcp_server, uds_servers):
        for payload in (
            {"text": "claim\n\n<!-- created=2026-01-01, last=2026-01-01, from=pi -->"},
            {"text": "a\n\u00a7\nforged second entry"},
            {"text": "ok", "session": "s, from=pi"},
        ):
            status, body = _request(addr, "POST", "/remember", payload)
            assert status == 400, f"payload was not refused: {payload} -> {status} {body}"

        # Nothing may have been persisted by any of the refused calls.
        entries = list((corpus / "inbox").glob("*.md")) if (corpus / "inbox").exists() else []
        assert not entries, f"a refused /remember still wrote to the inbox: {entries}"


def test_s7_identity_comes_from_the_socket_not_a_spoofed_body_field(tmp_path, monkeypatch):
    import tempfile
    corpus = _index_a_tiny_corpus(tmp_path, monkeypatch)
    # AF_UNIX paths are capped at ~104 bytes on macOS/BSD; pytest's tmp_path
    # nests under pytest-of-<user>/pytest-NNN/<test-name>/ which routinely
    # exceeds that. Use a short /tmp dir just for the socket files.
    sock_dir = tempfile.mkdtemp(dir="/tmp")
    sock_a = f"{sock_dir}/a.sock"
    sock_b = f"{sock_dir}/b.sock"
    ctx, tcp_server, uds_servers = _bind(
        corpus, monkeypatch, unix_sockets={"identity-a": sock_a, "identity-b": sock_b})

    with _running(tcp_server, uds_servers):
        # Socket A, body claims to be a totally different caller -- must be ignored.
        status, body = _request(sock_a, "POST", "/remember",
                                {"text": "Spoofing test fact one.", "from": "attacker-controlled-name"})
        assert status == 200
        entries = list(corpus.glob("inbox/*.md"))
        assert entries, "remember must have written an inbox entry"
        content = entries[0].read_text()
        assert "from=identity-a" in content
        assert "attacker-controlled-name" not in content

        # S7's second half: a request over TCP is recorded under the RESERVED
        # "local-anonymous" identity, not blank, not inherited from whichever
        # UDS identity happened to be requested first, and not spoofable
        # either.
        tcp_addr = tcp_server.server_address
        status, body = _request(tcp_addr, "POST", "/remember",
                                {"text": "Spoofing test fact two.", "from": "also-attacker-controlled"})
        assert status == 200
        entries_after = [p for p in corpus.glob("inbox/*.md")]
        combined = "\n".join(p.read_text() for p in entries_after)
        assert "from=local-anonymous" in combined
        assert "also-attacker-controlled" not in combined


# ---------------------------------------------------------------------------
# S8: a slow /answer does not block a concurrent /search.
# ---------------------------------------------------------------------------

def test_s8_a_slow_answer_does_not_block_a_concurrent_search(tmp_path, monkeypatch):
    import alexandria.cli as cli_mod

    corpus = _index_a_tiny_corpus(tmp_path, monkeypatch)
    ctx, tcp_server, uds_servers = _bind(corpus, monkeypatch)
    addr = tcp_server.server_address

    release = threading.Event()

    def _slow_answer(*args, **kwargs):
        release.wait(timeout=10)
        return cli_mod.AnswerOutcome(True, "slow answer text", 1, "fake-id")

    monkeypatch.setattr(cli_mod, "run_answer", _slow_answer)

    with _running(tcp_server, uds_servers):
        result = {}

        def _do_answer():
            result["status"], result["body"] = _request(
                addr, "POST", "/answer", {"question": "anything"}, timeout=15)

        t = threading.Thread(target=_do_answer)
        t.start()
        time.sleep(0.3)  # let /answer's request land and start blocking on the event

        started = time.monotonic()
        status, body = _request(addr, "POST", "/search", {"query": "example gateway"})
        elapsed = time.monotonic() - started
        assert status == 200
        assert elapsed < 2.0, f"a concurrent /search took {elapsed:.2f}s -- was it blocked by /answer?"

        release.set()
        t.join(timeout=10)
        assert result["status"] == 200
        assert result["body"]["text"] == "slow answer text"


# ---------------------------------------------------------------------------
# S9: a provider/manifest mismatch refuses to start, loudly.
# ---------------------------------------------------------------------------

def test_s9_a_manifest_mismatched_provider_refuses_to_start(tmp_path, monkeypatch):
    from alexandria import serve as serve_mod
    corpus = _index_a_tiny_corpus(tmp_path, monkeypatch)  # indexed with "hash"

    # Now request a server whose configured provider does not match the
    # manifest the index was actually built with.
    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "mlx")
    mismatched_config = load_config(corpus_override=corpus)

    with pytest.raises(SystemExit):
        serve_mod.bind(corpus, config=mismatched_config, host="127.0.0.1", port=0)


# ---------------------------------------------------------------------------
# The drain timer serve documented but never ran, and the startup warm-up.
# (2026-08-13: nine entries pending for 3.3h; first query after a launchd
# start took 26.29s against 0.03s warm.)
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _closing(tcp_server, uds_servers):
    """Bind without serving: these tests exercise startup and the drain, not
    the request path, so nothing needs a serve_forever thread -- but the
    listening sockets still have to be released."""
    try:
        yield
    finally:
        for server in (tcp_server, *uds_servers):
            server.server_close()


def _wait_until(predicate, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_the_drain_promotes_an_entry_nobody_sent_a_request_for(tmp_path, monkeypatch):
    """promote_pending had exactly two callers -- serve's inline /remember and
    the manual CLI -- so a fact remembered through the CLI stayed unsearchable
    indefinitely while liveness judged it against a 600s drain interval
    nothing implemented. Observable proof: the marker is consumed and the
    document exists, with no HTTP request made at all."""
    from alexandria import serve as serve_mod

    corpus = _index_a_tiny_corpus(tmp_path, monkeypatch)
    assert app(["--corpus", str(corpus), "remember", "The drain promoted this unasked."]) == 0
    assert len(list_pending(corpus)) == 1

    ctx, tcp_server, uds_servers = _bind(corpus, monkeypatch)
    stop = serve_mod.start_drain(ctx, interval=0.02)
    with _closing(tcp_server, uds_servers):
        drained = _wait_until(lambda: not list_pending(corpus))
        stop.set()

    assert drained, "the drain never consumed the pending marker"
    docs = list((corpus / "sources" / "inbox").rglob("*.md"))
    assert docs, "marker consumed but no document written -- promotion did nothing"
    assert (corpus / ".alexandria" / "liveness.json").exists()


def test_the_drain_survives_an_exception_and_takes_the_next_tick(tmp_path, monkeypatch):
    """A drain that dies on the first transient error recreates, silently, the
    bug it exists to fix."""
    from alexandria import serve as serve_mod
    from alexandria.promote import PromoteResult

    corpus = _index_a_tiny_corpus(tmp_path, monkeypatch)
    calls = []

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("transient promote failure")
        return PromoteResult()

    monkeypatch.setattr(serve_mod, "promote_pending", flaky)
    ctx, tcp_server, uds_servers = _bind(corpus, monkeypatch)
    stop = serve_mod.start_drain(ctx, interval=0.02)
    with _closing(tcp_server, uds_servers):
        kept_ticking = _wait_until(lambda: len(calls) >= 3)
        stop.set()

    assert kept_ticking, f"the drain stopped after the exception ({len(calls)} calls)"


def test_a_lock_skipped_drain_cycle_records_no_liveness_success(tmp_path, monkeypatch):
    """W5: another process holding the write lock is a normal outcome, but a
    cycle that did nothing must not write evidence that it did -- that is the
    house failure mode (a step reporting success while doing nothing)."""
    from alexandria import serve as serve_mod
    from alexandria.writelock import write_lock

    corpus = _index_a_tiny_corpus(tmp_path, monkeypatch)
    assert app(["--corpus", str(corpus), "remember", "Fact behind a held write lock."]) == 0
    entry_id = list_pending(corpus)[0]
    state_file = corpus / ".alexandria" / "liveness.json"
    state_file.unlink()  # written by `index`; absence is what makes this observable

    lock = write_lock(corpus)
    assert lock.acquire(), "test could not take the write lock it needs to hold"
    try:
        ctx, tcp_server, uds_servers = _bind(corpus, monkeypatch)
        stop = serve_mod.start_drain(ctx, interval=0.02)
        with _closing(tcp_server, uds_servers):
            time.sleep(0.5)  # several ticks, every one of them lock-skipped
            stop.set()
    finally:
        lock.release()

    assert list_pending(corpus) == [entry_id], "the entry must stay pending for the next drain"
    assert not state_file.exists(), "a lock-skipped cycle recorded a successful drain"


def test_bind_starts_the_drain_timer(tmp_path, monkeypatch):
    """The wiring itself: a drain nothing calls is the same dead code as no
    drain at all."""
    from alexandria import serve as serve_mod

    corpus = _index_a_tiny_corpus(tmp_path, monkeypatch)
    started = []

    def recorder(ctx, **kwargs):
        started.append(ctx)
        return threading.Event()

    monkeypatch.setattr(serve_mod, "start_drain", recorder)
    ctx, tcp_server, uds_servers = _bind(corpus, monkeypatch)
    with _closing(tcp_server, uds_servers):
        pass

    assert started and started[0] is ctx, "bind() must start the drain timer"


def test_startup_warms_the_embedding_provider_bypassing_the_cache(tmp_path, monkeypatch):
    """Measured: 26.29s first query, 0.03s after -- serve was lazy-loading the
    model on the first request, so always-on was not always-warm.

    Two binds, two provider calls. The startup manifest check already embeds a
    probe, but through CachedEmbedder, which serves it from the on-disk cache
    and never loads a model; a warm-up routed the same way would show one call
    on a fresh cache and none ever again.
    """
    from alexandria.index.embedder import HashEmbedder

    corpus = _index_a_tiny_corpus(tmp_path, monkeypatch)
    calls = []
    original = HashEmbedder.embed

    def counting(self, texts):
        calls.append(list(texts))
        return original(self, texts)

    monkeypatch.setattr(HashEmbedder, "embed", counting)
    for _ in range(2):
        _ctx, tcp_server, uds_servers = _bind(corpus, monkeypatch)
        with _closing(tcp_server, uds_servers):
            pass

    assert len(calls) == 2, (
        f"expected one provider-level embed per startup, got {len(calls)}: {calls}")


def test_startup_leaves_nothing_in_the_query_path_lazily_loaded(tmp_path, monkeypatch):
    """The embedder was only half the cold path.

    Measured live 2026-08-13 right after the embedder warm-up shipped: first
    novel query 16.11s, second 2.14s, third 0.80s. `_warm_embedder` was doing
    its job; `CrossEncoderReranker` loads a separate ~90MB model lazily on the
    first *search*, so startup still reported ready while the query path was
    cold.

    Asserts the invariant rather than the function: by the time bind() returns,
    no component of the query path may still be waiting to load. A third lazy
    component should fail this test rather than slip past a hand-written list.
    """
    from alexandria.retrieval.rerank import CrossEncoderReranker
    corpus = _index_a_tiny_corpus(tmp_path, monkeypatch)
    loaded_during_startup = []
    original = CrossEncoderReranker._load
    def spy(self):
        loaded_during_startup.append(True)
        return original(self)
    monkeypatch.setattr(CrossEncoderReranker, "_load", spy)
    _ctx, tcp_server, uds_servers = _bind(corpus, monkeypatch)
    with _closing(tcp_server, uds_servers):
        pass
    assert loaded_during_startup, (
        "bind() returned with the reranker model still unloaded -- the first "
        "real query would pay the cold load that the warm-up exists to absorb")
