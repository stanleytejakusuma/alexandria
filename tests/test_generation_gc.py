"""Generation garbage collection: dry-run by default, real bytes, never the active."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from alexandria.generation_gc import GenerationGcError, plan_generation_gc, run_generation_gc
from alexandria.generation_pointer import activate_generation


def _control_root(tmp_path: Path) -> Path:
    root = tmp_path
    (root / ".alexandria" / "generations").mkdir(parents=True)
    subprocess.run(["git", "init", "--quiet", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "t"], check=True)
    return root


def _generation(root: Path, generation_id: str, *, payload_bytes: int = 4096) -> Path:
    gen = root / ".alexandria" / "generations" / generation_id
    (gen / "sources").mkdir(parents=True)
    (gen / "sources" / "note.md").write_bytes(b"x" * payload_bytes)
    return gen


def _publish(root: Path, generation_id: str, *, payload_bytes: int = 4096) -> Path:
    """A generation with a real history commit -- i.e. successfully published."""
    gen = _generation(root, generation_id, payload_bytes=payload_bytes)
    history = root / "history" / "generations" / generation_id
    (history / "sources").mkdir(parents=True)
    (history / "sources" / "note.md").write_bytes(b"x" * payload_bytes)
    relative = history.relative_to(root)
    subprocess.run(["git", "-C", str(root), "add", "--", str(relative)], check=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-m", f"weekly generation {generation_id}"],
        check=True, capture_output=True,
    )
    activate_generation(root, gen)
    return gen


def test_plan_never_lists_the_active_generation_as_reclaimable(tmp_path: Path) -> None:
    root = _control_root(tmp_path)
    _publish(root, "g1")
    _publish(root, "g2")
    active = _publish(root, "g3")
    plan = plan_generation_gc(root, keep_published=2, keep_unpublished=1)
    assert active.name not in {c.generation_id for c in plan.reclaim}


def test_plan_keeps_the_newest_n_published_generations(tmp_path: Path) -> None:
    root = _control_root(tmp_path)
    _publish(root, "g1")
    _publish(root, "g2")
    _publish(root, "g3")
    plan = plan_generation_gc(root, keep_published=2, keep_unpublished=1)
    reclaim_ids = {c.generation_id for c in plan.reclaim}
    keep_ids = {c.generation_id for c in plan.keep}
    assert reclaim_ids == {"g1"}
    assert keep_ids == {"g2", "g3"}


def test_plan_keeps_the_newest_n_unpublished_candidates_and_reclaims_the_rest(tmp_path: Path) -> None:
    root = _control_root(tmp_path)
    _publish(root, "g1")
    _generation(root, "fail-a")
    _generation(root, "fail-b")
    plan = plan_generation_gc(root, keep_published=2, keep_unpublished=1)
    reclaim_ids = {c.generation_id for c in plan.reclaim}
    keep_ids = {c.generation_id for c in plan.keep}
    assert reclaim_ids == {"fail-a"}
    assert "fail-b" in keep_ids


def test_plan_reports_unique_bytes_not_apparent_clone_inflated_bytes(tmp_path: Path) -> None:
    root = _control_root(tmp_path)
    _publish(root, "g1", payload_bytes=200_000)
    (root / ".alexandria" / "generations" / "g1" / "sources" / "note.md").write_bytes(b"x" * 200_000)
    g2 = _generation(root, "fail-a")
    (g2 / ".alexandria" / "cache").mkdir(parents=True)
    subprocess.run([
        "/bin/cp", "-c",
        str(root / ".alexandria" / "generations" / "g1" / "sources" / "note.md"),
        str(g2 / ".alexandria" / "cache" / "cloned.bin"),
    ], check=True)
    (g2 / "unique.bin").write_bytes(b"y" * 50_000)
    plan = plan_generation_gc(root, keep_published=1, keep_unpublished=0)
    reclaimed = next(c for c in plan.reclaim if c.generation_id == "fail-a")
    # The cloned 200KB shares physical blocks with g1 (kept) and must not be
    # counted as reclaimable; only the genuinely unique bytes should be.
    assert reclaimed.unique_bytes < 90_000
    assert reclaimed.unique_bytes >= 50_000


def test_run_is_a_dry_run_by_default_and_deletes_nothing(tmp_path: Path) -> None:
    root = _control_root(tmp_path)
    _publish(root, "g1")
    fail = _generation(root, "fail-a")
    plan = run_generation_gc(root, keep_published=1, keep_unpublished=0)
    assert fail.exists()
    assert any(c.generation_id == "fail-a" for c in plan.reclaim)


def test_run_with_apply_deletes_only_the_planned_generations(tmp_path: Path) -> None:
    root = _control_root(tmp_path)
    kept = _publish(root, "g1")
    fail = _generation(root, "fail-a")
    run_generation_gc(root, keep_published=1, keep_unpublished=0, apply=True)
    assert not fail.exists()
    assert kept.exists()


def test_apply_rechecks_the_live_pointer_and_never_deletes_the_current_active(tmp_path: Path) -> None:
    # A plan is a point-in-time snapshot. If a rollback (or any republish)
    # happens between planning and applying, a generation the plan marked for
    # reclaim could now be active -- apply must re-resolve the pointer
    # immediately before each delete, not trust the plan's stale snapshot.
    root = _control_root(tmp_path)
    g1 = _publish(root, "g1")
    _publish(root, "g2")
    plan = plan_generation_gc(root, keep_published=1, keep_unpublished=0)
    assert plan.reclaim and plan.reclaim[0].generation_id == "g1"
    activate_generation(root, g1)  # simulate an operator rollback after planning
    run_generation_gc(root, keep_published=1, keep_unpublished=0, apply=True, _plan=plan)
    assert g1.exists()


def test_apply_refuses_when_the_pointer_is_unreadable(tmp_path: Path) -> None:
    root = _control_root(tmp_path)
    _generation(root, "fail-a")
    # No active generation was ever published -- pointer file does not exist.
    with pytest.raises(GenerationGcError):
        run_generation_gc(root, keep_published=1, keep_unpublished=0, apply=True)


def test_dry_run_still_works_when_the_pointer_is_unreadable(tmp_path: Path) -> None:
    root = _control_root(tmp_path)
    _generation(root, "fail-a")
    plan = plan_generation_gc(root, keep_published=1, keep_unpublished=0)
    assert plan.active_generation_id is None
    assert any(c.generation_id == "fail-a" for c in plan.reclaim)
