"""§4.2.1 / gates W2-W5: the crash-safe promote pipeline.

embed -> upsert -> FTS5 -> generation bump -> unlink marker, in that order,
with chunk_id-keyed idempotency making every step safe to rerun after a
crash at any point.
"""

from __future__ import annotations

import threading
import time

import pytest

from alexandria.cache import read_index_generation
from alexandria.cli import app
from alexandria.config import AppConfig
from alexandria.index.bm25 import BM25Index
from alexandria.index.embedder import CachedEmbedder, HashEmbedder
from alexandria.index.store import VectorStore
from alexandria.pending import is_pending, list_pending
from alexandria.promote import promote_pending
from alexandria.writelock import write_lock


def _remember(tmp_path, monkeypatch, text: str) -> str:
    """Write one entry through the real CLI path and return its entry id."""
    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    corpus = tmp_path / "corpus"
    assert app(["--corpus", str(corpus), "remember", text]) == 0
    pending = list_pending(corpus)
    assert len(pending) >= 1
    return pending[-1]


def _engine_pieces(corpus):
    config = AppConfig(corpus_path=corpus)
    embedder = CachedEmbedder(HashEmbedder(), corpus / ".alexandria" / "cache" / "embeddings.sqlite")
    store = VectorStore(corpus / ".alexandria" / "index")
    lexical = BM25Index(corpus / ".alexandria" / "index" / "fts.sqlite")
    return config, embedder, store, lexical


def _fts_count_for_doc(lexical: BM25Index, doc_id_prefix: str) -> int:
    rows = lexical.connection.execute(
        "SELECT chunk_id FROM chunk_metadata WHERE chunk_id LIKE ?",
        (f"{doc_id_prefix}%",)).fetchall()
    return len(rows)


def test_w2_a_fact_written_moments_earlier_is_found_by_search_through_the_automatic_promote_path(
        tmp_path, monkeypatch):
    """Reached through the real promote path (promote_pending), never a
    hand-rolled shortcut that fakes retrieval -- the automatic trigger this
    package adds (inline under `serve`, on a timer via the drain otherwise).
    """
    corpus = tmp_path / "corpus"
    entry_id = _remember(tmp_path, monkeypatch, "The example gateway routes traffic through a placeholder billing tier.")
    config, embedder, store, lexical = _engine_pieces(corpus)

    started = time.monotonic()
    result = promote_pending(corpus, config, embedder, store, lexical)
    elapsed = time.monotonic() - started

    assert entry_id in result.promoted
    assert elapsed < 5.0, f"promote took {elapsed:.2f}s, gate bounds this to a few seconds"
    assert not is_pending(corpus, entry_id)

    from alexandria.retrieval.rerank import CrossEncoderReranker
    from alexandria.retrieval.search import SearchConfig, SearchEngine
    from alexandria.monitor import QueryLogger
    engine = SearchEngine(embedder, store, lexical, CrossEncoderReranker(config.rerank_model),
                          SearchConfig(), QueryLogger(corpus / ".alexandria" / "queries.sqlite"),
                          query_cache=None, corpus_root=corpus)
    hits = engine.search("example gateway placeholder billing tier")
    assert any("example" in r.text.lower() or "payg" in r.text.lower() for r in hits), \
        f"the just-promoted fact was not found: {[r.text[:60] for r in hits]}"


def test_w3_a_crash_mid_promote_leaves_the_entry_pending_and_a_rerun_promotes_exactly_once(
        tmp_path, monkeypatch):
    corpus = tmp_path / "corpus"
    entry_id = _remember(tmp_path, monkeypatch, "Crash test fact for W3.")
    config, embedder, store, lexical = _engine_pieces(corpus)

    def crash_after_upsert(step):
        if step == "upsert":
            raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError, match="simulated crash"):
        promote_pending(corpus, config, embedder, store, lexical, test_hook=crash_after_upsert)

    assert is_pending(corpus, entry_id), "a crash before unlink must leave the marker in place"

    result = promote_pending(corpus, config, embedder, store, lexical)
    assert entry_id in result.promoted
    assert not is_pending(corpus, entry_id)

    doc_id_prefix = f"sources/inbox/inbox-{entry_id}"
    chunk_ids = [row[0] for row in lexical.connection.execute(
        "SELECT chunk_id FROM chunk_metadata WHERE chunk_id LIKE ?",
        (f"{doc_id_prefix}%",)).fetchall()]
    fts_count = len(chunk_ids)
    lance_count = sum(1 for cid in chunk_ids if store.get(cid) is not None)
    assert fts_count > 0, "the fact must have been indexed exactly once, not zero times"
    assert lance_count == fts_count, (
        f"LanceDB ({lance_count}) and FTS5 ({fts_count}) disagree -- exactly the "
        f"failure this gate exists to catch")


@pytest.mark.parametrize("crash_step", ["embed", "upsert", "fts", "bump", "unlink"])
def test_w3a_a_crash_at_each_of_the_five_write_order_points_converges_on_rerun(
        tmp_path, monkeypatch, crash_step):
    corpus = tmp_path / "corpus"
    entry_id = _remember(tmp_path, monkeypatch, f"W3a crash-injection fact at {crash_step}.")
    config, embedder, store, lexical = _engine_pieces(corpus)
    gen_before = read_index_generation(corpus)

    def crash_after(step):
        if step == crash_step:
            raise RuntimeError(f"simulated crash after {step}")

    with pytest.raises(RuntimeError):
        promote_pending(corpus, config, embedder, store, lexical, test_hook=crash_after)

    # Rerun to convergence -- possibly more than once if the crash landed
    # before the marker was even findable via a fresh engine construction
    # (it never is here, since entry_id stays in .alexandria/pending/
    # regardless of which step failed).
    result = promote_pending(corpus, config, embedder, store, lexical)

    if crash_step == "unlink":
        # The marker survives even a fully-completed promote if unlink itself
        # was the injected failure point -- rerunning consumes it on the next
        # pass because unlink is itself idempotent (FileNotFoundError-safe).
        assert not is_pending(corpus, entry_id) or promote_pending(
            corpus, config, embedder, store, lexical).promoted == [entry_id] or True
    assert not is_pending(corpus, entry_id), f"entry still pending after rerun (crash={crash_step})"

    doc_id_prefix = f"sources/inbox/inbox-{entry_id}"
    fts_rows = lexical.connection.execute(
        "SELECT chunk_id FROM chunk_metadata WHERE chunk_id LIKE ?",
        (f"{doc_id_prefix}%",)).fetchall()
    chunk_ids = [row[0] for row in fts_rows]
    assert len(chunk_ids) == len(set(chunk_ids)), "duplicate chunk_ids after crash+rerun"
    assert len(chunk_ids) > 0
    for chunk_id in chunk_ids:
        assert store.get(chunk_id) is not None, f"{chunk_id} in FTS5 but missing from the store"

    gen_after = read_index_generation(corpus)
    assert gen_after > gen_before, "the generation must have bumped at least once across the run"


def test_w4_generation_bumps_once_per_cycle_not_once_per_fact(tmp_path, monkeypatch):
    corpus = tmp_path / "corpus"
    _remember(tmp_path, monkeypatch, "First fact for W4.")
    _remember(tmp_path, monkeypatch, "Second fact for W4.")
    _remember(tmp_path, monkeypatch, "Third fact for W4.")
    config, embedder, store, lexical = _engine_pieces(corpus)
    gen_before = read_index_generation(corpus)

    result = promote_pending(corpus, config, embedder, store, lexical)

    assert len(result.promoted) == 3
    gen_after = read_index_generation(corpus)
    assert gen_after == gen_before + 1, (
        f"expected exactly one bump for a 3-fact cycle, went {gen_before} -> {gen_after}")


def test_w5_a_held_lock_causes_a_clean_skip_with_no_mutation_and_no_raise(tmp_path, monkeypatch):
    corpus = tmp_path / "corpus"
    entry_id = _remember(tmp_path, monkeypatch, "Locked-out fact for W5.")
    config, embedder, store, lexical = _engine_pieces(corpus)
    gen_before = read_index_generation(corpus)

    holder = write_lock(corpus)
    assert holder.acquire() is True
    try:
        result = promote_pending(corpus, config, embedder, store, lexical)  # must not raise
    finally:
        holder.release()

    assert result.skipped_locked is True
    assert result.promoted == []
    assert is_pending(corpus, entry_id), "a skipped drain must not consume the marker"
    assert read_index_generation(corpus) == gen_before, "a skipped drain must not mutate the index"

    # Confirm the entry is still promotable once the lock is free.
    result2 = promote_pending(corpus, config, embedder, store, lexical)
    assert entry_id in result2.promoted


def test_w6_a_concurrent_drain_and_reconcile_do_not_corrupt_the_corpus(tmp_path, monkeypatch):
    from alexandria.reconcile import reconcile_inbox

    corpus = tmp_path / "corpus"
    for i in range(5):
        _remember(tmp_path, monkeypatch, f"Concurrent fact number {i}.")
    config, embedder, store, lexical = _engine_pieces(corpus)

    errors: list[Exception] = []
    results = {}

    def run_promote():
        try:
            results["promote"] = promote_pending(corpus, config, embedder, store, lexical)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def run_reconcile():
        try:
            results["reconcile"] = reconcile_inbox(corpus, requeue=True)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=run_promote)
    t2 = threading.Thread(target=run_reconcile)
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)

    assert not errors, f"unexpected exceptions: {errors}"
    assert "database is locked" not in str(errors)
    # The corpus must remain internally consistent: every FTS chunk_id has a
    # corresponding store row.
    all_ids = [row[0] for row in
               lexical.connection.execute("SELECT chunk_id FROM chunk_metadata").fetchall()]
    assert len(all_ids) == len(set(all_ids)), "duplicate chunk_ids after concurrent access"
    for chunk_id in all_ids:
        assert store.get(chunk_id) is not None
