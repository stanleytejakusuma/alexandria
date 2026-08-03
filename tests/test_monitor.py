"""Query logs are append-only and never make logging a query failure."""

import sqlite3
from pathlib import Path

from alexandria.monitor import QueryLogger


def test_query_logger_appends_complete_retrieval_records(tmp_path: Path):
    logger = QueryLogger(tmp_path / "queries.sqlite")
    assert logger.log(query="retry lint", filters={"layer": "wiki"}, tier="map",
                      retrieved_ids=["wiki/a"], scores=[0.9], latency_ms=12.5,
                      cache_hit=True, client="cli")

    connection = sqlite3.connect(tmp_path / "queries.sqlite")
    row = connection.execute("SELECT q, filters, tier, retrieved_ids, cache_hit FROM queries").fetchone()
    assert row == ("retry lint", '{"layer": "wiki"}', "map", '["wiki/a"]', 1)


def test_query_logger_swallows_storage_failures(tmp_path: Path):
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("file blocks a directory")
    logger = QueryLogger(blocked / "queries.sqlite")
    assert not logger.log(query="q", filters={}, tier="map", retrieved_ids=[], scores=[],
                          latency_ms=1.0, cache_hit=False, client="test")
