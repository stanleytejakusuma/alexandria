"""§4.2: fcntl.flock, not an O_EXCL sentinel -- released by the kernel on
process death by any means, so a hard kill cannot wedge future writers.
Also §4.2's local-filesystem startup check: flock is a no-op on NFS/SMB."""

import subprocess

import pytest

from alexandria.writelock import (
    NotLocalFilesystem,
    WriteLock,
    _checked_local,
    assert_local_filesystem,
    write_lock,
)


def test_acquire_then_release_allows_a_second_acquire(tmp_path):
    lock = write_lock(tmp_path)
    assert lock.acquire() is True
    lock.release()
    lock2 = write_lock(tmp_path)
    assert lock2.acquire() is True
    lock2.release()


def test_a_second_process_scoped_lock_is_refused_while_the_first_holds_it(tmp_path):
    """Same-process flock semantics: acquiring twice on two DIFFERENT file
    handles for the same path is what a second process would observe --
    flock is per open-file-description, so two separate `open()` calls
    correctly model two separate processes contending for the same lock."""
    first = write_lock(tmp_path)
    assert first.acquire() is True
    second = write_lock(tmp_path)
    assert second.acquire() is False, "the lock must not be re-entrant across handles"
    first.release()
    assert second.acquire() is True
    second.release()


def test_context_manager_yields_acquired_bool_and_always_releases(tmp_path):
    with write_lock(tmp_path) as acquired:
        assert acquired is True
        blocked = write_lock(tmp_path)
        assert blocked.acquire() is False
    # released on exit
    again = write_lock(tmp_path)
    assert again.acquire() is True
    again.release()


def test_assert_local_filesystem_allows_a_normal_tmp_path(tmp_path):
    assert_local_filesystem(tmp_path)  # must not raise


def test_assert_local_filesystem_refuses_a_detected_network_mount(tmp_path, monkeypatch):
    """Mutation-style: fake `mount`'s output so the tmp_path's mount point
    reports as nfs, and confirm the guard actually reads and acts on it."""
    _checked_local.clear()  # this path may have been cached healthy by an earlier test
    resolved = str(tmp_path.resolve())

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(
            args, 0, stdout=f"fileserver:/export on {resolved} (nfs, nosuid)\n", stderr="")

    monkeypatch.setattr("alexandria.writelock.platform.system", lambda: "Darwin")
    monkeypatch.setattr("alexandria.writelock.subprocess.run", fake_run)

    with pytest.raises(NotLocalFilesystem) as exc_info:
        assert_local_filesystem(tmp_path)
    assert "nfs" in str(exc_info.value)
    _checked_local.discard(resolved)


def test_assert_local_filesystem_check_is_cached_per_path(tmp_path, monkeypatch):
    """The check must not shell out to `mount` on every single write-lock
    acquisition (inline promote on every /remember would make that a
    subprocess spawn per write) -- confirm it is consulted at most once."""
    _checked_local.clear()
    calls = []

    def fake_run(args, **kwargs):
        calls.append(1)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr("alexandria.writelock.platform.system", lambda: "Darwin")
    monkeypatch.setattr("alexandria.writelock.subprocess.run", fake_run)

    assert_local_filesystem(tmp_path)
    assert_local_filesystem(tmp_path)
    assert_local_filesystem(tmp_path)
    assert len(calls) == 1, f"expected exactly one `mount` call, got {len(calls)}"


def test_write_lock_acquire_runs_the_local_filesystem_check(tmp_path, monkeypatch):
    _checked_local.clear()
    resolved = str(tmp_path.resolve())

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(
            args, 0, stdout=f"server:/x on {resolved} (smbfs)\n", stderr="")

    monkeypatch.setattr("alexandria.writelock.platform.system", lambda: "Darwin")
    monkeypatch.setattr("alexandria.writelock.subprocess.run", fake_run)

    lock = write_lock(tmp_path)
    with pytest.raises(NotLocalFilesystem):
        lock.acquire()
    _checked_local.discard(resolved)
