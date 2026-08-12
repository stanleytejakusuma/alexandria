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

    # (A previous revision guarded the crash_step == "unlink" case with an
    # `assert ... or ... or True`, which can never fail and was therefore not a
    # check at all. The unconditional assertion below is the real one: unlink is
    # idempotent, so one rerun consumes the marker no matter which step failed.)
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


def test_w4_the_generation_bump_lands_after_both_stores_are_written(tmp_path, monkeypatch):
    """§4.2.1 step 4, the ordering itself -- not merely that a bump happened.

    Bumped BEFORE the writes land, a concurrent reader can retrieve pre-promote
    results and cache them under the NEW generation, where nothing will ever
    invalidate them (the generation is the only cache epoch). Counting bumps
    cannot see that: moving `write_index_generation` above `store.upsert` leaves
    every count-based assertion green. This pins the position by reading the
    on-disk generation at each step boundary."""
    corpus = tmp_path / "corpus"
    _remember(tmp_path, monkeypatch, "Ordering fact for W4.")
    config, embedder, store, lexical = _engine_pieces(corpus)
    gen_before = read_index_generation(corpus)
    seen: dict[str, int] = {}

    promote_pending(corpus, config, embedder, store, lexical,
                    test_hook=lambda step: seen.__setitem__(step, read_index_generation(corpus)))

    assert seen["embed"] == gen_before, "generation bumped before anything was written"
    assert seen["upsert"] == gen_before, (
        "generation bumped before/with the vector write -- a reader between the "
        "bump and the FTS write caches a partial index under the new epoch")
    assert seen["fts"] == gen_before, "generation bumped before the FTS write landed"
    assert seen["bump"] == gen_before + 1, "generation did not bump at step 4"
    assert seen["unlink"] == gen_before + 1, "generation moved again after step 4"


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


@pytest.mark.parametrize("kill_step", ["embed", "upsert", "fts", "bump", "unlink"])
def test_w3a_a_real_sigkill_mid_promote_still_reconverges_on_rerun(tmp_path, monkeypatch, kill_step):
    """The stronger form of W3a.

    The parametrized test above raises an exception between steps, in-process,
    and the rerun reuses the same live store/FTS/embedder handles. That proves
    "an exception at a step boundary converges" -- not the claim promote.py
    makes, which is that a CRASH converges. A real crash kills the process
    mid-flight: buffers are not flushed, `finally` never runs, the flock is
    dropped by the kernel, and the rerun must reopen every store cold.

    This kills the promoting process with SIGKILL at each of the five ordering
    points and then reconverges from a genuinely cold start.
    """
    import subprocess as sp
    import sys
    import textwrap
    from pathlib import Path

    corpus = tmp_path / "corpus"
    entry_id = _remember(tmp_path, monkeypatch, f"Real-crash fact at {kill_step}.")
    src = str(Path(__file__).resolve().parents[1] / "src")

    script = tmp_path / "crasher.py"
    script.write_text(textwrap.dedent(f"""
        import os, signal, sys
        sys.path.insert(0, {src!r})
        os.environ["ALEXANDRIA_EMBED_PROVIDER"] = "hash"
        from pathlib import Path
        from alexandria.config import AppConfig
        from alexandria.index.bm25 import BM25Index
        from alexandria.index.embedder import CachedEmbedder, HashEmbedder
        from alexandria.index.store import VectorStore
        from alexandria.promote import promote_pending

        corpus = Path({str(corpus)!r})
        config = AppConfig(corpus_path=corpus)
        embedder = CachedEmbedder(HashEmbedder(),
                                  corpus / ".alexandria" / "cache" / "embeddings.sqlite")
        store = VectorStore(corpus / ".alexandria" / "index")
        lexical = BM25Index(corpus / ".alexandria" / "index" / "fts.sqlite")

        def hook(step):
            if step == {kill_step!r}:
                os.kill(os.getpid(), signal.SIGKILL)   # no unwind, no finally

        promote_pending(corpus, config, embedder, store, lexical, test_hook=hook)
    """))

    proc = sp.run([sys.executable, str(script)], capture_output=True, timeout=120)
    assert proc.returncode == -9, (
        f"expected SIGKILL (-9), got {proc.returncode}: {proc.stderr.decode()[:400]}")

    # Cold reopen -- brand new handles, exactly as a restarted process would.
    config, embedder, store, lexical = _engine_pieces(corpus)
    result = promote_pending(corpus, config, embedder, store, lexical)

    assert not is_pending(corpus, entry_id), (
        f"entry still pending after a rerun following SIGKILL at {kill_step}")

    doc_id_prefix = f"sources/inbox/inbox-{entry_id}"
    chunk_ids = [row[0] for row in lexical.connection.execute(
        "SELECT chunk_id FROM chunk_metadata WHERE chunk_id LIKE ?",
        (f"{doc_id_prefix}%",)).fetchall()]
    assert chunk_ids, f"nothing indexed after SIGKILL at {kill_step} + rerun"
    assert len(chunk_ids) == len(set(chunk_ids)), "duplicate chunk_ids after a real crash"
    for chunk_id in chunk_ids:
        assert store.get(chunk_id) is not None, (
            f"{chunk_id} is in FTS5 but missing from the vector store after "
            f"SIGKILL at {kill_step} -- the two stores diverged across a real crash")
