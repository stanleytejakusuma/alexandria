"""§7 gate W7 at the real integration point: engine construction (used by
search/answer/eval) must warn loudly on stale liveness state but never fail
to return results. Unit-level liveness.check() behavior is covered in
test_liveness.py; this file exercises the wiring in cli._build_search_engine.
"""

from __future__ import annotations

import time

from alexandria.cli import app


def _index_a_tiny_corpus(tmp_path, monkeypatch):
    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    corpus = tmp_path / "corpus"
    doc = corpus / "sources" / "note.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("---\nsource: test\n---\n\nexample gateway routes traffic through a placeholder billing tier.\n")
    assert app(["--corpus", str(corpus), "index"]) == 0
    return corpus


def test_w7_a_healthy_freshly_indexed_corpus_prints_no_stale_warning(tmp_path, monkeypatch, capsys):
    corpus = _index_a_tiny_corpus(tmp_path, monkeypatch)
    capsys.readouterr()

    rc = app(["--corpus", str(corpus), "search", "example gateway"])

    assert rc == 0
    captured = capsys.readouterr()
    assert "stale" not in captured.err


def test_w7_a_stale_liveness_state_warns_but_search_still_returns_results(tmp_path, monkeypatch, capsys):
    corpus = _index_a_tiny_corpus(tmp_path, monkeypatch)
    liveness_path = corpus / ".alexandria" / "liveness.json"
    assert liveness_path.exists(), "cmd_index must call liveness.record_success"
    # Simulate a long-aged pending entry (the real staleness trigger) rather
    # than deleting the state file outright, matching §7's actual signal
    # (oldest unconsumed pending age), not just file presence.
    pending_dir = corpus / ".alexandria" / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)
    marker = pending_dir / "stale-entry-id"
    marker.touch()
    old = time.time() - 10_000  # far past the default drain_interval*2 threshold
    import os
    os.utime(marker, (old, old))
    capsys.readouterr()

    rc = app(["--corpus", str(corpus), "search", "example gateway"])

    assert rc == 0, "a stale liveness signal must never block results (fail loud, not fail closed on reads)"
    captured = capsys.readouterr()
    assert "stale" in captured.err
    assert "example" in captured.out.lower()


def test_w7_a_missing_liveness_state_file_warns_but_still_answers_queries(tmp_path, monkeypatch, capsys):
    corpus = _index_a_tiny_corpus(tmp_path, monkeypatch)
    liveness_path = corpus / ".alexandria" / "liveness.json"
    liveness_path.unlink()
    capsys.readouterr()

    rc = app(["--corpus", str(corpus), "search", "example gateway"])

    assert rc == 0
    captured = capsys.readouterr()
    assert "stale" in captured.err


def test_w1_remember_returns_in_under_half_a_second_with_no_model_load(tmp_path, monkeypatch):
    """§4.1/§11: remember must never load the embedding model -- it only
    appends to inbox/*.md and writes a pending marker. The bound is
    generous (500ms) because CI/disk variance is real; the point is
    orders of magnitude below the ~16s embedding-model cold load measured
    earlier this session, not a tight microbenchmark."""
    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    corpus = tmp_path / "corpus"

    started = time.monotonic()
    rc = app(["--corpus", str(corpus), "remember", "Fast fact for W1."])
    elapsed = time.monotonic() - started

    assert rc == 0
    assert elapsed < 0.5, f"remember took {elapsed:.3f}s -- did it touch the embedder or the index?"
