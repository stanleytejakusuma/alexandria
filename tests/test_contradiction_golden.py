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
        "claim_a": ["sources/old-decision"],
        "claim_b": ["sources/retirement-notice"],
        "relationship": "supersedes",
        "note": "service retired, old doc still says active",
        "provenance": "hand",
    })

    entries = load_contradiction_golden(path)

    assert entries == [ContradictionPairEntry(
        id="example-service-status",
        query="example service sole-source status",
        claim_a=("sources/old-decision",),
        claim_b=("sources/retirement-notice",),
        relationship="supersedes",
        note="service retired, old doc still says active",
        provenance="hand",
    )]


def test_claim_a_and_claim_b_accept_multiple_any_of_targets(tmp_path):
    """The whole reason for this schema change: a real corpus fact can be restated
    across several near-duplicate documents, and any one of them surfacing during
    gather should count -- same ANY-OF discipline as the retrieval golden set's
    must_retrieve, applied here after the identical under-counting problem was
    found by measurement."""
    path = tmp_path / "contra.jsonl"
    _write(path, {
        "id": "a", "query": "q",
        "claim_a": ["sources/a1", "sources/a2"],
        "claim_b": ["sources/b1", "sources/b2", "sources/b3"],
        "relationship": "contradicts", "provenance": "hand",
    })
    entries = load_contradiction_golden(path)
    assert entries[0].claim_a == ("sources/a1", "sources/a2")
    assert entries[0].claim_b == ("sources/b1", "sources/b2", "sources/b3")


def test_note_is_optional(tmp_path):
    path = tmp_path / "contra.jsonl"
    _write(path, {
        "id": "a", "query": "q", "claim_a": ["sources/a"], "claim_b": ["sources/b"],
        "relationship": "contradicts", "provenance": "hand",
    })
    entries = load_contradiction_golden(path)
    assert entries[0].note is None


@pytest.mark.parametrize(
    ("rows", "line"),
    [
        # missing required field
        ([{"id": "a", "claim_a": ["sources/a"], "claim_b": ["sources/b"],
           "relationship": "contradicts", "provenance": "hand"}], 1),
        # claim_a and claim_b share a document -- not a valid pair
        ([{"id": "a", "query": "q", "claim_a": ["sources/a", "sources/x"],
           "claim_b": ["sources/x"], "relationship": "contradicts", "provenance": "hand"}], 1),
        # empty claim_a list
        ([{"id": "a", "query": "q", "claim_a": [], "claim_b": ["sources/b"],
           "relationship": "contradicts", "provenance": "hand"}], 1),
        # claim_a as a bare string instead of a list -- must not silently coerce
        ([{"id": "a", "query": "q", "claim_a": "sources/a", "claim_b": ["sources/b"],
           "relationship": "contradicts", "provenance": "hand"}], 1),
        # invalid relationship value
        ([{"id": "a", "query": "q", "claim_a": ["sources/a"], "claim_b": ["sources/b"],
           "relationship": "disagrees", "provenance": "hand"}], 1),
        # invalid provenance value
        ([{"id": "a", "query": "q", "claim_a": ["sources/a"], "claim_b": ["sources/b"],
           "relationship": "contradicts", "provenance": "robot"}], 1),
        # duplicate id
        ([{"id": "a", "query": "q", "claim_a": ["sources/a"], "claim_b": ["sources/b"],
           "relationship": "contradicts", "provenance": "hand"},
          {"id": "a", "query": "q2", "claim_a": ["sources/c"], "claim_b": ["sources/d"],
           "relationship": "contradicts", "provenance": "hand"}], 2),
    ],
)
def test_rejects_invalid_entries_with_the_offending_line(tmp_path, rows, line):
    path = tmp_path / "contra.jsonl"
    _write(path, *rows)

    with pytest.raises(ValueError, match=fr"line {line}"):
        load_contradiction_golden(path)


def test_verify_contradiction_targets_checks_every_candidate_in_both_sides(tmp_path):
    corpus = tmp_path / "corpus"
    present = corpus / "sources" / "present.md"
    present.parent.mkdir(parents=True)
    present.write_text("x", encoding="utf-8")
    (corpus / "sources" / "present2.md").write_text("x", encoding="utf-8")

    entries = [
        ContradictionPairEntry("ok", "q", ("sources/present",), ("sources/present2",),
                                "contradicts", None, "hand"),
        ContradictionPairEntry("missing-b", "q", ("sources/present",), ("sources/deleted",),
                                "contradicts", None, "hand"),
        ContradictionPairEntry("missing-one-of-two-a", "q",
                                ("sources/present", "sources/deleted2"), ("sources/present2",),
                                "contradicts", None, "hand"),
    ]

    assert verify_contradiction_targets(entries, corpus) == ["missing-b", "missing-one-of-two-a"]
