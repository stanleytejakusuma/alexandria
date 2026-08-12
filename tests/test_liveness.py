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
