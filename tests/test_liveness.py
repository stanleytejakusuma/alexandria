"""§7: oldest-pending-age is the primary liveness signal, not a heartbeat --
a run that aborts early never writes a heartbeat, which is exactly how the
weekly loop's three-day outage survived undetected. Rules exercised here:
warn past 2x the drain interval; a missing or unparseable state file counts
as stale and warns (fail closed); check() never raises."""

import json
import os
import time

from alexandria.liveness import DEFAULT_DRAIN_INTERVAL_SECONDS, check, record_success
from alexandria.pending import create_pending, pending_dir


def test_check_warns_when_state_file_has_never_been_written(tmp_path):
    """§7's fail-closed rule. Safe by construction: every real caller of
    check() (via _build_search_engine) already passed _require_index, so a
    real corpus at this point has always run cmd_index at least once, which
    always calls record_success -- this exercises the anomalous case where
    that expectation is violated."""
    result = check(tmp_path)
    assert result.stale is True
    assert "does not exist" in result.reason


def test_check_is_healthy_after_a_recorded_success_with_nothing_pending(tmp_path):
    record_success(tmp_path, promoted_count=0, generation=1)
    result = check(tmp_path)
    assert result.stale is False
    assert result.oldest_pending_age_seconds is None


def test_check_warns_when_a_pending_entry_exceeds_twice_the_drain_interval(tmp_path):
    record_success(tmp_path, promoted_count=0, generation=1)
    create_pending(tmp_path, "stuck")
    marker = pending_dir(tmp_path) / "stuck"
    old = time.time() - (3 * DEFAULT_DRAIN_INTERVAL_SECONDS)
    os.utime(marker, (old, old))

    result = check(tmp_path)
    assert result.stale is True
    assert "drain interval" in result.reason
    assert result.oldest_pending_age_seconds is not None
    assert result.oldest_pending_age_seconds > 2 * DEFAULT_DRAIN_INTERVAL_SECONDS


def test_check_does_not_warn_just_under_the_threshold(tmp_path):
    record_success(tmp_path, promoted_count=0, generation=1)
    create_pending(tmp_path, "recent")
    marker = pending_dir(tmp_path) / "recent"
    recent = time.time() - (0.5 * DEFAULT_DRAIN_INTERVAL_SECONDS)
    os.utime(marker, (recent, recent))

    result = check(tmp_path)
    assert result.stale is False


def test_check_warns_on_a_corrupt_state_file_fails_loud_not_silent(tmp_path):
    record_success(tmp_path, promoted_count=0, generation=1)
    state_path = tmp_path / ".alexandria" / "liveness.json"
    state_path.write_text("{not valid json")

    result = check(tmp_path)
    assert result.stale is True
    assert "could not be parsed" in result.reason


def test_check_never_raises_even_on_a_pending_permission_error(tmp_path, monkeypatch):
    record_success(tmp_path, promoted_count=0, generation=1)
    create_pending(tmp_path, "x")

    def boom(self):
        raise PermissionError("denied")

    import pathlib
    monkeypatch.setattr(pathlib.Path, "iterdir", boom)
    result = check(tmp_path)  # must not raise
    assert result.stale is True
    assert "fail closed" in result.reason


def test_record_success_persists_the_telemetry_fields(tmp_path):
    record_success(tmp_path, promoted_count=3, generation=7)
    state_path = tmp_path / ".alexandria" / "liveness.json"
    data = json.loads(state_path.read_text())
    assert data["promoted_count"] == 3
    assert data["generation"] == 7
    assert "last_success_at" in data


def test_record_success_is_atomic_leaves_no_temp_file(tmp_path):
    record_success(tmp_path, promoted_count=0, generation=1)
    leftovers = list((tmp_path / ".alexandria").glob("liveness.json.tmp*"))
    assert leftovers == [], f"temp file(s) left behind: {leftovers}"


# ---------------------------------------------------------------------------
# The drain HEARTBEAT: proving the asynchronous half is alive, not merely idle.
#
# check() flagged a stuck promotion only via oldest-pending age, so a DEAD drain
# thread was invisible whenever the queue happened to be empty -- which is most
# of the time. The failure only surfaced once real user data was already late
# (2026-08-13: nine entries, 3.3 hours). record_success() has always written
# last_success_at on every completed cycle; nothing ever asserted its AGE.
# ---------------------------------------------------------------------------

def _record_at(corpus, *, seconds_ago: float, promoted_count: int = 0, generation: int = 1) -> None:
    """Write a liveness heartbeat backdated by `seconds_ago`."""
    import json as _json
    import os as _os
    from alexandria import liveness as lv

    lv.record_success(corpus, promoted_count=promoted_count, generation=generation)
    path = corpus / ".alexandria" / "liveness.json"
    state = _json.loads(path.read_text())
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(time.time() - seconds_ago))
    state["last_success_at"] = stamp
    path.write_text(_json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")
    _os.utime(path, (time.time() - seconds_ago, time.time() - seconds_ago))


def test_a_dead_drain_is_detected_from_the_heartbeat_even_with_an_EMPTY_queue(tmp_path):
    """The blind spot: nothing pending, so the old age-based check saw nothing.

    A drain thread that died silently keeps the corpus looking perfectly healthy
    until someone happens to write an entry AND it ages past 2x the interval.
    The heartbeat closes that: no completed cycle for several intervals is
    itself the fault, regardless of whether anything is queued.
    """
    from alexandria.liveness import check

    _record_at(tmp_path, seconds_ago=5000)          # >> 2 x 600s, queue empty
    result = check(tmp_path)

    assert result.stale is True
    assert "drain" in result.reason or "cycle" in result.reason
    assert result.oldest_pending_age_seconds is None, "nothing was pending -- that is the point"


def test_a_recent_heartbeat_with_an_empty_queue_is_healthy(tmp_path):
    """The common case must stay quiet, or the signal gets ignored."""
    from alexandria.liveness import check

    _record_at(tmp_path, seconds_ago=30)
    assert check(tmp_path).stale is False


def test_the_heartbeat_tolerates_one_missed_cycle_before_alarming(tmp_path):
    """A single skipped tick (lock contention) is normal, not an outage.

    promote_pending returning skipped_locked deliberately records NO success,
    so an ordinary index run holding the write lock will skip a cycle. Alarming
    on that would train the operator to ignore this signal.
    """
    from alexandria.liveness import check

    _record_at(tmp_path, seconds_ago=700)           # one interval missed
    assert check(tmp_path).stale is False


def test_a_stuck_pending_entry_still_reports_the_pending_reason_not_the_heartbeat(tmp_path):
    """Precedence: a late ENTRY is the more actionable diagnosis of the two."""
    from alexandria.liveness import check
    from alexandria.pending import create_pending

    _record_at(tmp_path, seconds_ago=30)            # drain alive...
    create_pending(tmp_path, "stuck-entry")
    marker = tmp_path / ".alexandria" / "pending" / "stuck-entry"
    old = time.time() - 5000
    os.utime(marker, (old, old))                    # ...but this entry is late

    result = check(tmp_path)
    assert result.stale is True
    assert "pending" in result.reason
