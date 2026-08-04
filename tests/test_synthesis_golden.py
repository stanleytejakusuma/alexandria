import json

import pytest

from alexandria.eval.synthesis_golden import (
    LoadBearingFact,
    SynthesisClusterEntry,
    load_synthesis_golden,
    verify_synthesis_targets,
)


def _write(path, *rows) -> None:
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def test_load_synthesis_golden_parses_cluster_with_facts(tmp_path):
    path = tmp_path / "synth.jsonl"
    _write(path, {
        "id": "mlx-embedder-switch",
        "topic": "why Alexandria switched from PyTorch to MLX for embeddings",
        "source_docs": ["sources/a", "sources/b"],
        "load_bearing_facts": [
            {"id": "f1", "text": "MLX is 3.18x faster than PyTorch/MPS",
             "supported_by": ["sources/a"]},
            {"id": "f2", "text": "PyTorch MPS had a graph-cache memory leak",
             "supported_by": ["sources/a", "sources/b"]},
        ],
        "provenance": "hand",
    })

    entries = load_synthesis_golden(path)

    assert entries == [SynthesisClusterEntry(
        id="mlx-embedder-switch",
        topic="why Alexandria switched from PyTorch to MLX for embeddings",
        source_docs=("sources/a", "sources/b"),
        load_bearing_facts=(
            LoadBearingFact("f1", "MLX is 3.18x faster than PyTorch/MPS", ("sources/a",)),
            LoadBearingFact("f2", "PyTorch MPS had a graph-cache memory leak",
                            ("sources/a", "sources/b")),
        ),
        provenance="hand",
    )]


@pytest.mark.parametrize(
    ("rows", "line"),
    [
        # missing required field
        ([{"id": "a", "source_docs": ["sources/a"], "load_bearing_facts": [
            {"id": "f1", "text": "t", "supported_by": ["sources/a"]}], "provenance": "hand"}], 1),
        # empty source_docs
        ([{"id": "a", "topic": "t", "source_docs": [], "load_bearing_facts": [
            {"id": "f1", "text": "t", "supported_by": ["sources/a"]}], "provenance": "hand"}], 1),
        # empty load_bearing_facts -- a cluster with zero facts to check is meaningless
        ([{"id": "a", "topic": "t", "source_docs": ["sources/a"], "load_bearing_facts": [],
           "provenance": "hand"}], 1),
        # fact.supported_by references a doc not in source_docs
        ([{"id": "a", "topic": "t", "source_docs": ["sources/a"], "load_bearing_facts": [
            {"id": "f1", "text": "t", "supported_by": ["sources/unlisted"]}],
           "provenance": "hand"}], 1),
        # invalid provenance value
        ([{"id": "a", "topic": "t", "source_docs": ["sources/a"], "load_bearing_facts": [
            {"id": "f1", "text": "t", "supported_by": ["sources/a"]}], "provenance": "robot"}], 1),
        # duplicate cluster id
        ([{"id": "a", "topic": "t", "source_docs": ["sources/a"], "load_bearing_facts": [
            {"id": "f1", "text": "t", "supported_by": ["sources/a"]}], "provenance": "hand"},
          {"id": "a", "topic": "t2", "source_docs": ["sources/a"], "load_bearing_facts": [
            {"id": "f1", "text": "t", "supported_by": ["sources/a"]}], "provenance": "hand"}], 2),
        # duplicate fact id within one cluster
        ([{"id": "a", "topic": "t", "source_docs": ["sources/a"], "load_bearing_facts": [
            {"id": "f1", "text": "t", "supported_by": ["sources/a"]},
            {"id": "f1", "text": "t2", "supported_by": ["sources/a"]}], "provenance": "hand"}], 1),
    ],
)
def test_rejects_invalid_entries_with_the_offending_line(tmp_path, rows, line):
    path = tmp_path / "synth.jsonl"
    _write(path, *rows)

    with pytest.raises(ValueError, match=fr"line {line}"):
        load_synthesis_golden(path)


def test_verify_synthesis_targets_checks_source_docs_and_fact_support(tmp_path):
    corpus = tmp_path / "corpus"
    present = corpus / "sources" / "present.md"
    present.parent.mkdir(parents=True)
    present.write_text("x", encoding="utf-8")

    entries = [
        SynthesisClusterEntry(
            "ok", "topic", ("sources/present",),
            (LoadBearingFact("f1", "text", ("sources/present",)),), "hand",
        ),
        SynthesisClusterEntry(
            "missing-source", "topic", ("sources/deleted",),
            (LoadBearingFact("f1", "text", ("sources/deleted",)),), "hand",
        ),
    ]

    assert verify_synthesis_targets(entries, corpus) == ["missing-source"]
