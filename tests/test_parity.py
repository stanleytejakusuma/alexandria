from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from alexandria.parity import (
    CorpusSnapshot,
    ParityError,
    compare_snapshots,
    probe_remote_snapshot,
    snapshot_corpus,
)


def _corpus(tmp_path: Path, name: str, *, docs: dict[str, str], generation: int) -> Path:
    root = tmp_path / name
    for relative, content in docs.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    index = root / ".alexandria" / "index"
    index.mkdir(parents=True, exist_ok=True)
    (index / "generation.json").write_text(json.dumps({"generation": generation}))
    return root


def test_snapshot_records_source_ids_generation_and_logical_bytes(tmp_path: Path) -> None:
    root = _corpus(
        tmp_path,
        "local",
        docs={
            "sources/a.md": "# A\n",
            "wiki/b.md": "# B\n",
            "sources/._ignored.md": "metadata",
        },
        generation=12,
    )

    snapshot = snapshot_corpus(root)

    assert snapshot.document_ids == frozenset({"sources/a", "wiki/b"})
    assert snapshot.generation == 12
    assert snapshot.logical_bytes["sources"] == len("# A\n")
    assert snapshot.logical_bytes["wiki"] == len("# B\n")


def test_compare_reports_every_mac_only_and_nas_only_source(tmp_path: Path) -> None:
    local = snapshot_corpus(_corpus(
        tmp_path, "local", docs={"sources/mac.md": "M", "sources/shared.md": "S"}, generation=262
    ))
    remote = snapshot_corpus(_corpus(
        tmp_path, "nas", docs={"sources/nas.md": "N", "sources/shared.md": "S"}, generation=194
    ))

    report = compare_snapshots(local, remote)

    assert report.local_only == ("sources/mac",)
    assert report.remote_only == ("sources/nas",)
    assert report.document_delta == 0
    assert report.generation_delta == 68
    assert report.local_only_families == {"sources/mac": 1}
    assert report.remote_only_families == {"sources/nas": 1}
    assert not report.source_history_diverged
    assert compare_snapshots(
        replace(local, git_head="local-head"), replace(remote, git_head="remote-head")
    ).source_history_diverged
    assert not report.in_sync


def test_compare_distinguishes_same_id_with_changed_content(tmp_path: Path) -> None:
    local = snapshot_corpus(_corpus(
        tmp_path, "local", docs={"sources/shared.md": "new fact"}, generation=3
    ))
    remote = snapshot_corpus(_corpus(
        tmp_path, "nas", docs={"sources/shared.md": "old fact"}, generation=3
    ))

    report = compare_snapshots(local, remote)

    assert report.local_only == ()
    assert report.remote_only == ()
    assert report.content_mismatches == ("sources/shared",)
    assert not report.in_sync


def test_remote_probe_uses_validated_host_and_json_only(tmp_path: Path) -> None:
    expected = CorpusSnapshot(
        corpus="/remote/corpus",
        document_ids=frozenset({"sources/remote"}),
        document_hashes={"sources/remote": "a" * 64},
        generation=7,
        active_generation=None,
        git_head="abc123",
        logical_bytes={"sources": 1, "wiki": 0, "state": 0, "index": 0, "cache": 0},
    )
    calls: list[list[str]] = []

    def run(command: list[str]) -> str:
        calls.append(command)
        return json.dumps(expected.to_json())

    actual = probe_remote_snapshot("nas", "/remote/corpus", run=run)

    assert actual == expected
    assert calls[0][0:2] == ["ssh", "nas"]
    assert "python3 -c" in calls[0][2]
    with pytest.raises(ParityError, match="invalid remote host"):
        probe_remote_snapshot("nas; rm -rf /", "/remote/corpus", run=run)


def test_remote_probe_rejects_malformed_json() -> None:
    with pytest.raises(ParityError, match="invalid JSON"):
        probe_remote_snapshot("nas", "/remote/corpus", run=lambda _: "not-json")
