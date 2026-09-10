"""`alexandria generation gc` through the real CLI entry point.

Exercises `_config_for(args).control_root` resolution and argument wiring,
not just the library functions `test_generation_gc.py` already covers.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from alexandria.cli import app
from alexandria.generation_pointer import activate_generation


def _control_root(tmp_path: Path) -> Path:
    root = tmp_path / "control"
    (root / ".alexandria" / "generations").mkdir(parents=True)
    subprocess.run(["git", "init", "--quiet", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "t"], check=True)
    return root


def _publish(root: Path, generation_id: str) -> Path:
    gen = root / ".alexandria" / "generations" / generation_id
    (gen / "sources").mkdir(parents=True)
    (gen / "sources" / "note.md").write_bytes(b"x" * 4096)
    history = root / "history" / "generations" / generation_id
    (history / "sources").mkdir(parents=True)
    (history / "sources" / "note.md").write_bytes(b"x" * 4096)
    relative = history.relative_to(root)
    subprocess.run(["git", "-C", str(root), "add", "--", str(relative)], check=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-m", f"weekly generation {generation_id}"],
        check=True, capture_output=True,
    )
    activate_generation(root, gen)
    return gen


def _stray(root: Path, generation_id: str) -> Path:
    gen = root / ".alexandria" / "generations" / generation_id
    (gen / "sources").mkdir(parents=True)
    (gen / "sources" / "note.md").write_bytes(b"y" * 4096)
    return gen


def test_dry_run_is_the_default_through_the_real_cli(tmp_path, capsys) -> None:
    control = _control_root(tmp_path)
    _publish(control, "g1")
    stray = _stray(control, "fail-a")

    exit_code = app(["--corpus", str(control), "generation", "gc",
                      "--keep-published", "1", "--keep-unpublished", "0"])

    assert exit_code == 0
    assert stray.exists()  # dry run: nothing deleted
    out = capsys.readouterr().out
    assert "would reclaim" in out
    assert "fail-a" in out
    assert "--apply" in out


def test_apply_flag_actually_deletes_through_the_real_cli(tmp_path, capsys) -> None:
    control = _control_root(tmp_path)
    kept = _publish(control, "g1")
    stray = _stray(control, "fail-a")

    exit_code = app(["--corpus", str(control), "generation", "gc",
                      "--keep-published", "1", "--keep-unpublished", "0", "--apply"])

    assert exit_code == 0
    assert not stray.exists()
    assert kept.exists()
    out = capsys.readouterr().out
    assert "reclaimed" in out
    assert "would reclaim" not in out


def test_gc_refuses_and_exits_nonzero_when_pointer_is_unreadable(tmp_path, capsys) -> None:
    control = _control_root(tmp_path)
    _stray(control, "fail-a")  # no publish ever happened -- no pointer

    exit_code = app(["--corpus", str(control), "generation", "gc",
                      "--keep-published", "1", "--keep-unpublished", "0", "--apply"])

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "refused" in err
