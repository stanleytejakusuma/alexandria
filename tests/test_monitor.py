"""Query logs are append-only and never make logging a query failure."""

import sqlite3
from pathlib import Path

from alexandria.monitor import QueryLogger


def test_query_logger_appends_complete_retrieval_records(tmp_path: Path):
    logger = QueryLogger(tmp_path / "queries.sqlite")
    query_id = logger.log(query="retry lint", filters={"layer": "wiki"}, tier="map",
                          retrieved_ids=["wiki/a"], scores=[0.9], latency_ms=12.5,
                          cache_hit=True, client="cli")
    assert query_id

    connection = sqlite3.connect(tmp_path / "queries.sqlite")
    row = connection.execute(
        "SELECT query_id, q, filters, tier, retrieved_ids, cache_hit FROM queries").fetchone()
    assert row == (query_id, "retry lint", '{"layer": "wiki"}', "map", '["wiki/a"]', 1)


def test_query_logger_log_returns_the_query_id_not_a_bare_bool(tmp_path: Path):
    """#9/C1.1: the id was always generated internally but discarded before this
    fix -- returning it is what makes a later citation record joinable back to
    the search that produced the evidence."""
    logger = QueryLogger(tmp_path / "queries.sqlite")
    query_id = logger.log(query="q", filters={}, tier="map", retrieved_ids=[],
                          scores=[], latency_ms=1.0, cache_hit=0, client="cli")
    assert isinstance(query_id, str)
    import uuid as _uuid
    _uuid.UUID(query_id)  # a real uuid4, not a placeholder string


def test_query_logger_swallows_storage_failures(tmp_path: Path):
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("file blocks a directory")
    logger = QueryLogger(blocked / "queries.sqlite")
    result = logger.log(query="q", filters={}, tier="map", retrieved_ids=[], scores=[],
                        latency_ms=1.0, cache_hit=False, client="test")
    assert result is None  # #9: None on failure, not bare False -- still falsy


def test_log_usage_records_tokens_joinable_to_a_query_id(tmp_path: Path):
    """SPEC-write-path-and-serve.md F5: token usage must be recoverable per call,
    keyed to the id the caller already tracks -- never a fabricated dollar cost."""
    logger = QueryLogger(tmp_path / "queries.sqlite")
    assert logger.log_usage(query_id="answer-abc123", model="gpt-5.6-terra",
                            prompt_tokens=1200, completion_tokens=340,
                            total_tokens=1540, cache_read=900)

    connection = sqlite3.connect(tmp_path / "queries.sqlite")
    row = connection.execute(
        "SELECT query_id, model, prompt_tokens, completion_tokens, total_tokens, cache_read FROM usage"
    ).fetchone()
    assert row == ("answer-abc123", "gpt-5.6-terra", 1200, 340, 1540, 900)


def test_log_usage_swallows_storage_failures(tmp_path: Path):
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("file blocks a directory")
    logger = QueryLogger(blocked / "queries.sqlite")
    assert not logger.log_usage(query_id="x", model="m", prompt_tokens=1,
                                completion_tokens=1, total_tokens=2)


def test_queries_db_waits_instead_of_locking_under_a_concurrent_writer(tmp_path: Path):
    """SPEC §3.1 / F1: a second writer must WAIT (busy_timeout), not raise
    'database is locked' immediately -- the prerequisite for serve's concurrent
    search+answer logging."""
    path = tmp_path / "queries.sqlite"
    logger = QueryLogger(path)
    logger.log(query="warm the schema", filters={}, tier="map", retrieved_ids=[],
              scores=[], latency_ms=1.0, cache_hit=0, client="cli")

    holder = sqlite3.connect(path)
    holder.execute("PRAGMA busy_timeout=5000")
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("INSERT INTO queries VALUES ('held', 't', 'q', '{}', 'map', '[]', '[]', 1.0, 0, 'cli')")

    import threading
    result = {}
    def writer():
        result["ok"] = logger.log(query="second writer", filters={}, tier="map",
                                  retrieved_ids=[], scores=[], latency_ms=1.0,
                                  cache_hit=0, client="cli")
    t = threading.Thread(target=writer)
    t.start()
    import time as _time
    _time.sleep(0.2)
    holder.commit()
    t.join(timeout=5)
    assert isinstance(result["ok"], str) and result["ok"]  # #9: now returns query_id, not bool
