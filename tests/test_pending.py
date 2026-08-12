"""§4.1: the pending list is a directory of zero-length marker files, created
with O_CREAT|O_EXCL and consumed with unlink -- both atomic in the kernel."""

import time

from alexandria.pending import (
    create_pending,
    is_pending,
    list_pending,
    oldest_pending_age,
    pending_dir,
)


def test_create_pending_makes_a_zero_length_file(tmp_path):
    assert create_pending(tmp_path, "abc123") is True
    marker = pending_dir(tmp_path) / "abc123"
    assert marker.exists()
    assert marker.stat().st_size == 0


def test_create_pending_is_idempotent_via_o_excl(tmp_path):
    """O_CREAT|O_EXCL: calling twice for the same entry must not raise and
    must not double-queue -- the second call reports 'already pending'."""
    assert create_pending(tmp_path, "abc123") is True
    assert create_pending(tmp_path, "abc123") is False
    assert list_pending(tmp_path) == ["abc123"]


def test_unlink_pending_is_idempotent(tmp_path):
    create_pending(tmp_path, "abc123")
    assert is_pending(tmp_path, "abc123")
    from alexandria.pending import unlink_pending
    assert unlink_pending(tmp_path, "abc123") is True
    assert not is_pending(tmp_path, "abc123")
    assert unlink_pending(tmp_path, "abc123") is False  # already gone, not an error


def test_list_pending_orders_oldest_first(tmp_path):
    create_pending(tmp_path, "first")
    (pending_dir(tmp_path) / "first").touch()
    time.sleep(0.01)
    create_pending(tmp_path, "second")
    assert list_pending(tmp_path) == ["first", "second"]


def test_oldest_pending_age_none_when_nothing_pending(tmp_path):
    assert oldest_pending_age(tmp_path) is None
    create_pending(tmp_path, "x")
    from alexandria.pending import unlink_pending
    unlink_pending(tmp_path, "x")
    assert oldest_pending_age(tmp_path) is None  # directory exists but empty


def test_oldest_pending_age_measures_the_oldest_marker(tmp_path):
    create_pending(tmp_path, "old")
    marker = pending_dir(tmp_path) / "old"
    old_time = time.time() - 1000
    import os
    os.utime(marker, (old_time, old_time))
    create_pending(tmp_path, "new")

    age = oldest_pending_age(tmp_path)
    assert age is not None
    assert 999 <= age <= 1005, f"expected ~1000s, got {age}"
