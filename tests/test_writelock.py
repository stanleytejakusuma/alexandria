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


def test_a_sibling_mount_with_a_shared_name_prefix_is_not_mistaken_for_it(tmp_path, monkeypatch):
    """The mount table is matched by longest prefix. Matched without respecting
    path component boundaries, "/Volumes/Databank" reads as being under the
    "/Volumes/Data" NFS share, and a perfectly local corpus is refused for a
    filesystem it is not on. Fail-closed, so this cost availability rather
    than safety -- but an unexplainable refusal is how a guard gets removed."""
    _checked_local.clear()
    corpus = tmp_path / "Databank" / "corpus"
    corpus.mkdir(parents=True)
    base = str(tmp_path.resolve())

    def fake_run(args, **kwargs):
        # "Databank" is an ordinary DIRECTORY on the local disk -- it has no
        # mount entry of its own. The NFS share's mountpoint is a string prefix
        # of it and sorts longer than the local disk's, so a boundary-blind
        # longest-prefix match hands back "nfs" for a path that is not on it.
        return subprocess.CompletedProcess(args, 0, stderr="", stdout=(
            f"/dev/disk1s1 on {base} (apfs, local, journaled)\n"
            f"fileserver:/export on {base}/Data (nfs, nosuid)\n"))

    monkeypatch.setattr("alexandria.writelock.platform.system", lambda: "Darwin")
    monkeypatch.setattr("alexandria.writelock.subprocess.run", fake_run)

    assert_local_filesystem(corpus)  # must not raise
    _checked_local.discard(str(corpus.resolve()))


def test_a_real_path_actually_on_the_network_mount_is_still_refused(tmp_path, monkeypatch):
    """The other half of the same fix: narrowing the match must not stop it
    catching a path genuinely beneath the network mount."""
    _checked_local.clear()
    corpus = tmp_path / "Data" / "corpus"
    corpus.mkdir(parents=True)
    base = str(tmp_path.resolve())

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stderr="", stdout=(
            f"/dev/disk1s1 on {base} (apfs, local, journaled)\n"
            f"fileserver:/export on {base}/Data (nfs, nosuid)\n"))

    monkeypatch.setattr("alexandria.writelock.platform.system", lambda: "Darwin")
    monkeypatch.setattr("alexandria.writelock.subprocess.run", fake_run)

    with pytest.raises(NotLocalFilesystem):
        assert_local_filesystem(corpus)
    _checked_local.discard(str(corpus.resolve()))


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


def test_the_lock_is_released_by_the_kernel_when_a_holder_is_sigkilled(tmp_path):
    """§4.2 chose fcntl.flock over an O_EXCL sentinel file specifically because
    a sentinel survives SIGKILL with no owner and wedges every future writer
    silently, which §7's liveness signal would not attribute to a stuck lock.

    That rationale was prose until now. This kills a real holding process with
    SIGKILL -- no handler, no unwind, no `finally` -- and proves the next
    acquirer succeeds.
    """
    import signal
    import subprocess as sp
    import sys
    import textwrap
    import time
    from pathlib import Path

    corpus = tmp_path / "corpus"
    (corpus / ".alexandria" / "index").mkdir(parents=True)
    ready = tmp_path / "acquired.flag"

    holder_src = textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {str(Path(__file__).resolve().parents[1] / "src")!r})
        from alexandria.writelock import write_lock
        lock = write_lock({str(corpus)!r})
        assert lock.acquire() is True
        open({str(ready)!r}, "w").close()
        time.sleep(120)
    """)
    script = tmp_path / "holder.py"
    script.write_text(holder_src)

    holder = sp.Popen([sys.executable, str(script)], stdout=sp.PIPE, stderr=sp.PIPE)
    try:
        deadline = time.monotonic() + 30
        while not ready.exists() and time.monotonic() < deadline:
            if holder.poll() is not None:
                raise AssertionError(f"holder died early: {holder.communicate()[1].decode()}")
            time.sleep(0.05)
        assert ready.exists(), "holder never acquired the lock"

        # Contended while the holder is alive -- proves the lock is real.
        contender = write_lock(corpus)
        assert contender.acquire() is False, "lock was not held by the live holder"

        holder.send_signal(signal.SIGKILL)
        holder.wait(timeout=10)

        # The kernel drops the flock when the process dies, however it died.
        deadline = time.monotonic() + 10
        acquired = False
        while time.monotonic() < deadline and not acquired:
            acquired = write_lock(corpus).acquire()
            if not acquired:
                time.sleep(0.05)
        assert acquired, "lock was NOT released after the holder was SIGKILLed"
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=5)
