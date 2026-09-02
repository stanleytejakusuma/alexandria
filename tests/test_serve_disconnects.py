"""Expected client disconnects are not server failures."""
from __future__ import annotations

import pytest

from alexandria import serve


def _handler():
    # The boundary is reached before ctx is used; a plain sentinel keeps this
    # test focused on BaseHTTPRequestHandler.handle() behavior.
    return object.__new__(serve._make_handler_class_impl(object(), None, ("127.0.0.1",)))


def test_handler_swallows_an_expected_connection_reset(monkeypatch):
    def reset(_self):
        raise ConnectionResetError("client cancelled")

    monkeypatch.setattr(serve.BaseHTTPRequestHandler, "handle", reset)
    _handler().handle()


def test_handler_does_not_hide_a_real_programming_error(monkeypatch):
    def bug(_self):
        raise RuntimeError("real defect")

    monkeypatch.setattr(serve.BaseHTTPRequestHandler, "handle", bug)
    with pytest.raises(RuntimeError, match="real defect"):
        _handler().handle()
