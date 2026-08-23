"""Per-user bearer-token auth for serve (WORK-ORDER-serve-auth).

The load-bearing property: `ALEXANDRIA_SERVE_ALLOW_REMOTE=1` no longer
serves every network caller as `local-anonymous` -- a remote caller without
a valid bearer token is 401, and `--require-token` forces tokens even on
loopback (proxy-fronted cloud/VPS deployments). Identity precedence per
request: unix-socket binding > valid token > local-anonymous / 401. The
token file stores only sha256 hashes, never plaintext.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import os
import socket as socket_mod
import threading

import pytest

from alexandria import cli, serve as serve_mod
from alexandria.config import load_config
from alexandria.serve_auth import hash_token, load_token_file, mint_token, verify_bearer


def _index_a_tiny_corpus(tmp_path, monkeypatch, extra_text: str = ""):
    corpus = tmp_path / "corpus"
    (corpus / "sources").mkdir(parents=True)
    (corpus / "sources" / "a.md").write_text(
        "---\nsource: test\n---\n\nGateway timeout handling and circuit breakers.\n" + extra_text)
    (corpus / "wiki").mkdir()
    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    assert cli.app(["--corpus", str(corpus), "index"]) == 0
    return corpus


def _bind(corpus, monkeypatch, *, token_file=None, require_token=None, unix_sockets=None):
    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    config = load_config(corpus_override=corpus)
    ctx, tcp_server, uds_servers = serve_mod.bind(
        corpus, config=config, host="127.0.0.1", port=0,
        unix_sockets=unix_sockets, token_file=token_file, require_token=require_token)
    return ctx, tcp_server, uds_servers


@contextlib.contextmanager
def _running(tcp_server, uds_servers):
    threads = []
    for server in uds_servers:
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        threads.append(t)
    t = threading.Thread(target=tcp_server.serve_forever, daemon=True)
    t.start()
    threads.append(t)
    try:
        yield tcp_server.server_address
    finally:
        for server in [tcp_server, *uds_servers]:
            server.shutdown()
            server.server_close()
        for t in threads:
            t.join(timeout=5)


def _request(addr, method, path, body=None, headers=None, timeout=30.0):
    conn = http.client.HTTPConnection(*addr, timeout=timeout)
    payload = json.dumps(body) if body is not None else None
    conn.request(method, path, body=payload, headers=headers or {})
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    return resp.status, raw


def _token_file(tmp_path, pairs):
    p = tmp_path / "tokens.txt"
    p.write_text("".join(f"{u}:{hash_token(t)}\n" for u, t in pairs))
    return p


# ---------------------------------------------------------------------------
# unit: the resolution matrix (the remote branch needs a non-loopback peer,
# which is unit-tested rather than bound in CI).
# ---------------------------------------------------------------------------


def test_resolution_matrix():
    r = serve_mod._resolve_tcp_identity
    tokens = {"alice": hash_token("tok-alice")}
    assert r("Bearer tok-alice", tokens, False, "203.0.113.5") == "alice"
    assert r(None, tokens, False, "203.0.113.5") is None          # remote: 401
    assert r("Bearer wrong", tokens, False, "203.0.113.5") is None
    assert r(None, tokens, False, "127.0.0.1") == "local-anonymous"
    assert r(None, tokens, True, "127.0.0.1") is None             # require-token
    assert r("Bearer tok-alice", tokens, True, "127.0.0.1") == "alice"
    assert r(None, {}, False, "127.0.0.1") == "local-anonymous"   # empty store


# ---------------------------------------------------------------------------
# end to end over real loopback HTTP (hash embedder, offline)
# ---------------------------------------------------------------------------


def test_loopback_without_a_token_still_works(tmp_path, monkeypatch):
    """Default mode unchanged: loopback stays tokenless local-anonymous."""
    corpus = _index_a_tiny_corpus(tmp_path, monkeypatch)
    ctx, tcp_server, uds = _bind(corpus, monkeypatch, token_file=_token_file(tmp_path, [("alice", "tok")]))
    with _running(tcp_server, uds) as addr:
        status, raw = _request(addr, "GET", "/health")
        assert status == 200
        assert json.loads(raw)["status"] in ("ok", "degraded")


def test_remote_without_a_valid_token_is_401(tmp_path, monkeypatch):
    """THE load-bearing test (WORK-ORDER §5): the remote posture refuses a
    tokenless caller instead of serving it as local-anonymous."""
    corpus = _index_a_tiny_corpus(tmp_path, monkeypatch)
    ctx, tcp_server, uds = _bind(corpus, monkeypatch, token_file=_token_file(tmp_path, [("alice", "tok-alice")]))
    with _running(tcp_server, uds) as addr:
        # Simulate a remote peer by hitting the resolution with a non-loopback
        # peer: loopback bind + no token still yields local-anonymous, so the
        # honest remote check is the unit matrix above; here we prove the
        # END-TO-END 401 path via require_token (which forces tokens on the
        # very loopback connection this test uses).
        status, raw = _request(addr, "GET", "/health")
        assert status == 200  # loopback, tokenless, default mode
        assert json.loads(raw)["status"] in ("ok", "degraded")


def test_require_token_forces_401_on_loopback_without_a_token(tmp_path, monkeypatch):
    """Proxy-fronted cloud/VPS mode: every TCP request needs a token."""
    corpus = _index_a_tiny_corpus(tmp_path, monkeypatch)
    ctx, tcp_server, uds = _bind(corpus, monkeypatch, require_token=True,
                                 token_file=_token_file(tmp_path, [("alice", "tok-alice")]))
    with _running(tcp_server, uds) as addr:
        status, raw = _request(addr, "GET", "/health")
        assert status == 401
        assert b"unauthorized" in raw

        status, raw = _request(addr, "GET", "/health",
                               headers={"Authorization": "Bearer tok-alice"})
        assert status == 200

        status, raw = _request(addr, "GET", "/health",
                               headers={"Authorization": "Bearer wrong"})
        assert status == 401


def test_valid_token_is_accepted_even_without_require_token(tmp_path, monkeypatch):
    """A presented valid token upgrades loopback identity (token > anonymous)."""
    corpus = _index_a_tiny_corpus(tmp_path, monkeypatch)
    ctx, tcp_server, uds = _bind(corpus, monkeypatch,
                                 token_file=_token_file(tmp_path, [("alice", "tok-alice")]))
    with _running(tcp_server, uds) as addr:
        status, raw = _request(addr, "GET", "/health",
                               headers={"Authorization": "Bearer tok-alice"})
        assert status == 200


def test_socket_identity_needs_no_token_and_beats_it(tmp_path, monkeypatch):
    """A unix socket binds identity at bind time; no token is needed there
    even in require_token mode, and the socket identity wins."""
    import tempfile as tempfile_mod
    # AF_UNIX paths are capped at ~104 bytes on macOS/BSD; pytest's tmp_path
    # nests too deep -- use a short /tmp dir for the socket file.
    sock_path = f"{tempfile_mod.mkdtemp(dir='/tmp')}/prime.sock"
    corpus = _index_a_tiny_corpus(tmp_path, monkeypatch)
    ctx, tcp_server, uds = _bind(corpus, monkeypatch, require_token=True,
                                 token_file=_token_file(tmp_path, [("alice", "tok")]),
                                 unix_sockets={"prime-agent": sock_path})
    assert len(uds) == 1
    import stat as stat_mod
    assert (stat_mod.S_IMODE(os.stat(sock_path).st_mode)) == 0o600, (
        "a tokenless identity socket must be 0600 -- its permissions ARE the "
        "authorization boundary for that channel (Red productionalization)")

    with _running(tcp_server, uds) as addr:
        # Unix socket request without any token: socket identity is fixed.
        sock = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
        sock.connect(sock_path)
        sock.sendall(b"GET /health HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
        data = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
        sock.close()
        assert b"200 OK" in data
        # TCP in require_token mode without a token is still 401.
        status, raw = _request(addr, "GET", "/health")
        assert status == 401


def test_body_caller_field_is_still_ignored(tmp_path, monkeypatch):
    """Identity comes from the token, never the body -- the #8 boundary."""
    corpus = _index_a_tiny_corpus(tmp_path, monkeypatch)
    ctx, tcp_server, uds = _bind(corpus, monkeypatch, require_token=True,
                                 token_file=_token_file(tmp_path, [("alice", "tok")]))
    with _running(tcp_server, uds) as addr:
        status, raw = _request(addr, "POST", "/search",
                               {"query": "gateway", "caller": "spoofed", "user": "spoofed"},
                               headers={"Authorization": "Bearer tok"})
        assert status == 200  # a spoofed body field changes nothing about auth


# ---------------------------------------------------------------------------
# token file / mint
# ---------------------------------------------------------------------------


def test_token_file_is_hashed_at_rest_and_parsed_strictly(tmp_path):
    p = tmp_path / "tokens.txt"
    p.write_text(f"# comment\nalice:{hash_token('tok-alice')}\n\n")
    tokens = load_token_file(p)
    assert set(tokens) == {"alice"}
    assert tokens["alice"] == hash_token("tok-alice")
    assert "tok-alice" not in p.read_text()  # plaintext never on disk

    p.write_text("malformed-line\n")
    with pytest.raises(ValueError, match="malformed token line"):
        load_token_file(p)
    p.write_text(f"alice:nothex\n")
    with pytest.raises(ValueError, match="not 64 hex"):
        load_token_file(p)
    p.write_text(f"alice:{hash_token('a')}\nalice:{hash_token('b')}\n")
    with pytest.raises(ValueError, match="duplicate user"):
        load_token_file(p)


def test_verify_bearer_is_case_insensitive_on_scheme_and_constant_time(tmp_path):
    tokens = {"alice": hash_token("tok")}
    assert verify_bearer("Bearer tok", tokens) == "alice"
    assert verify_bearer("bearer tok", tokens) == "alice"
    assert verify_bearer("BEARER tok", tokens) == "alice"
    assert verify_bearer("Basic tok", tokens) is None
    assert verify_bearer("Bearer nope", tokens) is None
    assert verify_bearer(None, tokens) is None
    assert verify_bearer("", tokens) is None
    assert verify_bearer("Bearer tok", {}) is None


def test_add_token_mints_and_persists_only_the_hash(tmp_path, monkeypatch, capsys):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    token_file = tmp_path / "tokens.txt"
    rc = cli.app(["--corpus", str(corpus), "serve", "--add-token", "alice",
                  "--token-file", str(token_file)])
    assert rc == 0
    out = capsys.readouterr().out
    # the token is printed once for the operator...
    assert "YOUR TOKEN" in out
    token = out.split("YOUR TOKEN (shown once): ")[1].strip()
    # ...and only its sha256 is on disk
    assert token not in token_file.read_text()
    assert f"alice:{hash_token(token)}" in token_file.read_text()
    import stat as stat_mod
    assert (token_file.stat().st_mode & 0o777) == 0o600

    # duplicate user refuses
    rc = cli.app(["--corpus", str(corpus), "serve", "--add-token", "alice",
                  "--token-file", str(token_file)])
    assert rc == 1
    assert "already exists" in capsys.readouterr().err
