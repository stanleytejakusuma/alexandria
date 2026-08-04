import json

import pytest

from alexandria.eval.contradiction_golden import (
    ContradictionPairEntry,
    load_contradiction_golden,
    verify_contradiction_targets,
)


def _write(path, *rows) -> None:
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def test_load_contradiction_golden_parses_a_pair(tmp_path):
    path = tmp_path / "contra.jsonl"
    _write(path, {
        "id": "example-service-status",
        "query": "example service sole-source status",
        "claim_a": "sources/old-decision",
        "claim_b": "sources/retirement-notice",
        "relationship": "supersedes",
        "note": "service retired, old doc still says active",
        "provenance": "hand",
    })

    entries = load_contradiction_golden(path)

    assert entries == [ContradictionPairEntry(
        id="example-service-status",
        query="example service sole-source status",
        claim_a="sources/old-decision",
        claim_b="sources/retirement-notice",
        relationship="supersedes",
        note="service retired, old doc still says active",
        provenance="hand",
    )]


def test_note_is_optional(tmp_path):
    path = tmp_path / "contra.jsonl"
    _write(path, {
        "id": "a", "query": "q", "claim_a": "sources/a", "claim_b": "sources/b",
        "relationship": "contradicts", "provenance": "hand",
    })
    entries = load_contradiction_golden(path)
    assert entries[0].note is None


@pytest.mark.parametrize(
    ("rows", "line"),
    [
        # missing required field
        ([{"id": "a", "claim_a": "sources/a", "claim_b": "sources/b",
           "relationship": "contradicts", "provenance": "hand"}], 1),
        # claim_a == claim_b -- a pair must be two distinct documents
        ([{"id": "a", "query": "q", "claim_a": "sources/a", "claim_b": "sources/a",
           "relationship": "contradicts", "provenance": "hand"}], 1),
        # invalid relationship value
        ([{"id": "a", "query": "q", "claim_a": "sources/a", "claim_b": "sources/b",
           "relationship": "disagrees", "provenance": "hand"}], 1),
        # invalid provenance value
        ([{"id": "a", "query": "q", "claim_a": "sources/a", "claim_b": "sources/b",
           "relationship": "contradicts", "provenance": "robot"}], 1),
        # duplicate id
        ([{"id": "a", "query": "q", "claim_a": "sources/a", "claim_b": "sources/b",
           "relationship": "contradicts", "provenance": "hand"},
          {"id": "a", "query": "q2", "claim_a": "sources/c", "claim_b": "sources/d",
           "relationship": "contradicts", "provenance": "hand"}], 2),
    ],
)
def test_rejects_invalid_entries_with_the_offending_line(tmp_path, rows, line):
    path = tmp_path / "contra.jsonl"
    _write(path, *rows)

    with pytest.raises(ValueError, match=fr"line {line}"):
        load_contradiction_golden(path)


def test_verify_contradiction_targets_checks_both_members(tmp_path):
    corpus = tmp_path / "corpus"
    present = corpus / "sources" / "present.md"
    present.parent.mkdir(parents=True)
    present.write_text("x", encoding="utf-8")

    entries = [
        ContradictionPairEntry("ok", "q", "sources/present", "sources/present2",
                                "contradicts", None, "hand"),
        ContradictionPairEntry("missing-b", "q", "sources/present", "sources/deleted",
                                "contradicts", None, "hand"),
    ]
    (corpus / "sources" / "present2.md").write_text("x", encoding="utf-8")

    assert verify_contradiction_targets(entries, corpus) == ["missing-b"]
