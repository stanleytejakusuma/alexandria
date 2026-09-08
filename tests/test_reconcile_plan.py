from __future__ import annotations

from alexandria.parity import CorpusSnapshot, build_reconcile_plan, compare_snapshots


def _snapshot(
    name: str,
    hashes: dict[str, str],
    *,
    generation: int,
    head: str,
) -> CorpusSnapshot:
    return CorpusSnapshot(
        corpus=f"/{name}",
        document_ids=frozenset(hashes),
        document_hashes=hashes,
        generation=generation,
        active_generation=None,
        git_head=head,
        logical_bytes={"sources": 1, "wiki": 0, "state": 0, "index": 0, "cache": 0},
    )


def test_reconcile_plan_is_directional_and_never_an_apply_instruction() -> None:
    local = _snapshot(
        "mac",
        {"sources/local": "a" * 64, "sources/shared": "b" * 64},
        generation=263,
        head="local-head",
    )
    remote = _snapshot(
        "nas",
        {"sources/remote": "c" * 64, "sources/shared": "d" * 64},
        generation=194,
        head="remote-head",
    )

    plan = build_reconcile_plan(compare_snapshots(local, remote))

    assert plan.add_to_remote == ("sources/local",)
    assert plan.add_to_local == ("sources/remote",)
    assert plan.conflicts == (("sources/shared", "b" * 64, "d" * 64),)
    assert plan.requires_operator_confirmation
    assert plan.to_json()["apply"] is False
    assert plan.to_json()["source_history"] == {
        "local": "local-head",
        "remote": "remote-head",
        "diverged": True,
    }
