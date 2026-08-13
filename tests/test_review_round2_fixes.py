"""Second batch of review findings: the paths that fail silently.

Each of these was a step that reported success while doing nothing useful,
or a guard that could crash the health signal exactly when the system was
busiest. Grouped by the surface they defend.
"""

from __future__ import annotations

import errno
import io
import json
import os
import socket
import tarfile
import threading
import time
from pathlib import Path

import pytest

from alexandria.backup import backup_state, restore_state
from alexandria.pending import (create_pending, list_pending, oldest_pending_age,
                                pending_dir, unlink_pending)

from test_serve import _bind, _index_a_tiny_corpus, _running


# --- S2: Content-Length is attacker-controlled --------------------------------------

def _raw_post(addr, headers: str, body: bytes = b"", timeout: float = 10.0) -> bytes:
    """http.client refuses to send a malformed Content-Length, which is the
    whole point -- a hostile client has no such scruples. Raw socket it is."""
    sock = socket.create_connection(addr, timeout=timeout)
    try:
        try:
            sock.sendall(headers.encode() + body)
        except (ConnectionResetError, BrokenPipeError):
            # A correct server answers 4xx and closes WITHOUT draining the body,
            # so a large send races the close. Keep the body small enough to fit
            # the socket buffer; tolerate the reset if the kernel disagrees.
            pass
        sock.settimeout(timeout)
        chunks = []
        while True:
            try:
                data = sock.recv(4096)
            except socket.timeout:
                break
            if not data:
                break
            chunks.append(data)
            # Do NOT stop at the header terminator: the body frequently arrives
            # in a later segment, which made this read flaky (~40% of runs saw
            # headers only). Both rejection paths set close_connection, so the
            # server closes and recv returns b"" -- read to EOF.
        return b"".join(chunks)
    finally:
        sock.close()


def test_s2_a_negative_content_length_is_rejected_not_read_to_eof(tmp_path, monkeypatch):
    """The bypass: -1 passes `> MAX_BODY_BYTES`, then rfile.read(-1) reads to
    EOF, defeating the cap entirely. Reverting the parse makes this hang or
    return 200."""
    corpus = _index_a_tiny_corpus(tmp_path, monkeypatch)
    ctx, tcp_server, uds_servers = _bind(corpus, monkeypatch)
    addr = tcp_server.server_address
    with _running(tcp_server, uds_servers):
        # Small enough to fit the socket buffer (see _raw_post). Size is not what
        # proves the bug: under the old code rfile.read(-1) blocks until EOF, so
        # no response arrives at all and the assertions below fail.
        body = b"x" * 4096
        resp = _raw_post(addr, (
            "POST /search HTTP/1.1\r\nHost: localhost\r\n"
            "Content-Type: application/json\r\nContent-Length: -1\r\n\r\n"), body)

    assert b"400" in resp.split(b"\r\n")[0], resp[:200]
    assert b"invalid Content-Length" in resp


def test_s2_a_non_numeric_content_length_gets_a_4xx_not_a_dropped_socket(tmp_path, monkeypatch):
    """S5 promises a 4xx. Parsing outside _dispatch_safely raised instead and
    the socket closed with no response -- indistinguishable from a network
    failure, the worst possible signal for a write API."""
    corpus = _index_a_tiny_corpus(tmp_path, monkeypatch)
    ctx, tcp_server, uds_servers = _bind(corpus, monkeypatch)
    addr = tcp_server.server_address
    with _running(tcp_server, uds_servers):
        resp = _raw_post(addr, (
            "POST /search HTTP/1.1\r\nHost: localhost\r\n"
            "Content-Type: application/json\r\nContent-Length: abc\r\n\r\n"))

    assert resp, "server closed the connection with no response at all"
    assert b"400" in resp.split(b"\r\n")[0], resp[:200]
    assert b"invalid Content-Length" in resp


# --- F6: the pending scan races a drain, by design ----------------------------------

def _vanish_between_is_file_and_stat(monkeypatch, directory: Path, *, victims: int):
    """Reproduce the ACTUAL production window, which an earlier draft of these
    tests missed entirely.

    `Path.is_file()` calls `Path.stat()` and swallows a real ENOENT via
    `_ignore_error`, returning False. So a marker that vanishes BEFORE is_file
    is simply skipped -- no exception, no bug. The only window that can raise
    is between is_file's stat and the SECOND, explicit stat that reads st_mtime.

    That distinction is not pedantic: the earlier draft unlinked on the first
    stat and therefore passed against the unguarded code, which is exactly the
    class of test this project keeps getting burned by. Here the first stat for
    a given path succeeds and the second raises a real ENOENT (errno 2), which
    is what a concurrent promote actually produces.
    """
    real_stat = Path.stat
    seen: dict[Path, int] = {}
    hits = {"n": 0}

    def staged_stat(self, *a, **kw):
        if self.parent == directory and hits["n"] < victims:
            seen[self] = seen.get(self, 0) + 1
            if seen[self] == 2:
                hits["n"] += 1
                self.unlink(missing_ok=True)
                raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), str(self))
        return real_stat(self, *a, **kw)

    monkeypatch.setattr(Path, "stat", staged_stat)
    return hits


def test_listing_pending_survives_a_marker_consumed_mid_scan(tmp_path, monkeypatch):
    """The marker IS the redo log, so promote unlinks it concurrently. A scan
    that stats what it just listed hits FileNotFoundError."""
    corpus = tmp_path / "corpus"
    for i in range(30):
        create_pending(corpus, f"entry-{i}")

    hits = _vanish_between_is_file_and_stat(monkeypatch, pending_dir(corpus), victims=5)
    names = list_pending(corpus)  # must not raise

    assert hits["n"] == 5, "the race never fired; this test would prove nothing"
    assert len(names) == 25


def test_the_liveness_signal_does_not_die_when_a_drain_is_running(tmp_path, monkeypatch):
    """oldest_pending_age is the health surface (§7). If it raises during a
    drain, monitoring goes blind precisely when work is in flight."""
    corpus = tmp_path / "corpus"
    for i in range(10):
        create_pending(corpus, f"entry-{i}")

    hits = _vanish_between_is_file_and_stat(monkeypatch, pending_dir(corpus), victims=3)
    age = oldest_pending_age(corpus)  # must not raise

    assert hits["n"] == 3, "the race never fired; this test would prove nothing"
    assert age is not None, "seven markers survived, so there is still an oldest"


def test_a_real_concurrent_drain_does_not_break_the_scan(tmp_path):
    """The monkeypatched tests prove the guard; this one proves the race is
    real without simulating it."""
    corpus = tmp_path / "corpus"
    ids = [f"entry-{i}" for i in range(200)]
    for entry_id in ids:
        create_pending(corpus, entry_id)

    errors: list[BaseException] = []
    stop = threading.Event()

    def drain():
        for entry_id in ids:
            unlink_pending(corpus, entry_id)
            time.sleep(0)
        stop.set()

    def scan():
        try:
            while not stop.is_set():
                list_pending(corpus)
                oldest_pending_age(corpus)
        except BaseException as exc:  # noqa: BLE001 -- the assertion IS "nothing escaped"
            errors.append(exc)

    t1, t2 = threading.Thread(target=drain), threading.Thread(target=scan)
    t1.start(), t2.start()
    t1.join(), t2.join()

    assert not errors, f"scan raised during a concurrent drain: {errors[:1]}"


# --- restore: silence about dropped members -----------------------------------------

def test_restore_reports_members_it_refused_to_extract(tmp_path):
    """Trimming an unexpected member is deliberate. Reporting the same
    'restored N paths' as a clean archive is not -- the operator could not
    distinguish a good backup from a tampered one."""
    corpus = tmp_path / "corpus"
    state = corpus / ".alexandria"
    state.mkdir(parents=True)
    (state / "liveness.json").write_text(json.dumps({"ok": True}))

    archive = tmp_path / "backup.tar.gz"
    backup_state(corpus, archive)

    # Smuggle a member outside the allowlist, as a corrupted, hand-edited, or
    # hostile archive would.
    with tarfile.open(tmp_path / "tampered.tar.gz", "w:gz") as tar:
        with tarfile.open(archive, "r:gz") as src:
            for m in src.getmembers():
                tar.addfile(m, src.extractfile(m))
        evil = tarfile.TarInfo(name="../../etc/passwd")
        evil.size = 5
        tar.addfile(evil, io.BytesIO(b"pwned"))

    target = tmp_path / "restored"
    target.mkdir()
    result = restore_state(target, tmp_path / "tampered.tar.gz")

    assert "../../etc/passwd" in result.skipped, "the refusal must be visible, not silent"
    assert not any("passwd" in r for r in result.restored)
    assert not (tmp_path / "etc" / "passwd").exists()


def test_a_clean_archive_reports_nothing_skipped(tmp_path):
    corpus = tmp_path / "corpus"
    state = corpus / ".alexandria"
    state.mkdir(parents=True)
    (state / "liveness.json").write_text("{}")
    archive = tmp_path / "backup.tar.gz"
    backup_state(corpus, archive)

    target = tmp_path / "restored"
    target.mkdir()
    result = restore_state(target, archive)

    assert result.skipped == []
    assert result.restored
