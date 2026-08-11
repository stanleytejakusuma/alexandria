"""Which bursts get marked consumed.

Getting this wrong is expensive in both directions: committing a FAILED burst
loses it permanently (it is never retried), while refusing to commit an EMPTY
burst means every weekly run re-distils every session that had nothing to say,
forever. Before 2026-08-11 the loop did the second: ~33% of bursts produce no
note, and none of them were ever consumed.

A connector may signal failure two ways -- by raising, or by recording into
`self.errors` and returning [] (pi-sessions does the latter so one bad burst
cannot kill a 465-burst batch). Both must count as failure.
"""

from types import SimpleNamespace

import pytest

from alexandria import cli
from alexandria.connectors.base import RawItem
from alexandria.corpus import Doc

FM = {"type": "memory", "title": "T", "generated": {"by": "test", "at": "2026-01-01"},
      "status": "stable", "source": "fake", "source_id": "x"}


class FakeConnector:
    name = "fake"

    def __init__(self):
        self.errors: list[str] = []
        self.committed: list[str] = []

    def discover(self):
        return [RawItem(source_id=i, content="c")
                for i in ("produces", "empty", "records-error", "raises")]

    def normalize(self, item):
        if item.source_id == "produces":
            return [Doc(path="sources/fake/a.md", frontmatter=FM, body="body\n")]
        if item.source_id == "empty":
            return []                                   # nothing durable: correct + common
        if item.source_id == "records-error":
            self.errors.append(f"{item.source_id}: LLMError: gateway said no")
            return []                                   # swallowed, NOT a success
        raise RuntimeError("boom")

    def commit(self, items):
        self.committed.extend(i.source_id for i in items)

    def skip_log(self):
        return []


@pytest.fixture
def run(tmp_path, monkeypatch):
    def _run():
        conn = FakeConnector()
        monkeypatch.setattr(cli, "_sync_connector", lambda args: conn)
        args = SimpleNamespace(connector="fake", corpus=str(tmp_path), workers=2,
                               limit=0, dry_run=False)
        assert cli.cmd_sync(args) == 0
        return conn
    return _run


def test_an_empty_burst_is_consumed_so_it_is_not_redistilled_every_week(run):
    assert "empty" in run().committed


def test_a_productive_burst_is_consumed(run):
    assert "produces" in run().committed


def test_a_raising_burst_stays_unconsumed_for_retry(run):
    assert "raises" not in run().committed


def test_a_burst_whose_error_was_RECORDED_stays_unconsumed_for_retry(run):
    """The connector caught its own error and returned [] -- indistinguishable
    from 'nothing durable' unless the recorded error is checked. Committing it
    would silently discard the burst."""
    conn = run()
    assert "records-error" not in conn.committed, \
        "a recorded failure was treated as an empty success and consumed"
    # Both failure styles surface: the one the connector recorded itself, and
    # the one the pool caught when normalize() raised.
    assert any(e.startswith("records-error:") for e in conn.errors)
    assert any(e.startswith("raises:") for e in conn.errors)
