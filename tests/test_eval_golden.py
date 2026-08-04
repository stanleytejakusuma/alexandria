import json

import pytest

from alexandria.eval.golden import GoldenEntry, load_golden, verify_targets


def _write(path, *rows) -> None:
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def test_load_golden_preserves_file_order_and_optional_note(tmp_path):
    path = tmp_path / "golden.jsonl"
    _write(
        path,
        {"id": "first", "query": "first query", "must_retrieve": ["sources/first"], "k": 5},
        {"id": "second", "query": "second query", "must_retrieve": ["sources/a", "sources/b"],
         "k": 3, "note": "ANY-OF"},
    )

    entries = load_golden(path)

    assert entries == [
        GoldenEntry("first", "first query", ("sources/first",), 5),
        GoldenEntry("second", "second query", ("sources/a", "sources/b"), 3, "ANY-OF"),
    ]


@pytest.mark.parametrize(
    ("rows", "line"),
    [
        ([{"id": "one", "query": "q", "must_retrieve": ["sources/a"], "k": 1,
           "unexpected": True}], 1),
        ([{"id": "one", "must_retrieve": ["sources/a"], "k": 1}], 1),
        ([{"id": "one", "query": "q", "must_retrieve": [], "k": 1}], 1),
        ([{"id": "one", "query": "q", "must_retrieve": ["sources/a"], "k": 1},
          {"id": "one", "query": "again", "must_retrieve": ["sources/b"], "k": 1}], 2),
    ],
)
def test_load_golden_rejects_invalid_entries_with_the_offending_line(tmp_path, rows, line):
    path = tmp_path / "golden.jsonl"
    _write(path, *rows)

    with pytest.raises(ValueError, match=fr"line {line}"):
        load_golden(path)


def test_load_golden_rejects_malformed_json_with_its_line_number(tmp_path):
    path = tmp_path / "golden.jsonl"
    path.write_text('{"id":"ok","query":"q","must_retrieve":["sources/a"],"k":1}\nnot json\n',
                    encoding="utf-8")

    with pytest.raises(ValueError, match=r"line 2"):
        load_golden(path)


def test_verify_targets_returns_entry_ids_with_missing_target_documents(tmp_path):
    corpus = tmp_path / "corpus"
    existing = corpus / "sources" / "present.md"
    existing.parent.mkdir(parents=True)
    existing.write_text("present", encoding="utf-8")
    entries = [
        GoldenEntry("present", "q", ("sources/present",), 5),
        GoldenEntry("missing", "q", ("sources/deleted",), 5),
    ]

    assert verify_targets(entries, corpus) == ["missing"]


# ---- overlap_band + provenance: NoLiMa-style diagnostic stratification, and
# provenance so a hand-written entry is distinguishable from an LLM-assisted one ----


def test_overlap_band_and_provenance_are_optional_and_accepted(tmp_path):
    p = tmp_path / "g.jsonl"
    p.write_text(json.dumps({
        "id": "a", "query": "q", "must_retrieve": ["d"], "k": 5,
        "overlap_band": "zero", "provenance": "assisted",
    }) + "\n")
    entries = load_golden(p)
    assert entries[0].overlap_band == "zero"
    assert entries[0].provenance == "assisted"


def test_entry_without_the_new_fields_still_loads(tmp_path):
    """Every one of the 15 existing entries predates these fields -- they must keep
    loading as-is, with the new fields simply absent (None), not required."""
    p = tmp_path / "g.jsonl"
    p.write_text(json.dumps({"id": "a", "query": "q", "must_retrieve": ["d"], "k": 5}) + "\n")
    entries = load_golden(p)
    assert entries[0].overlap_band is None
    assert entries[0].provenance is None


def test_invalid_overlap_band_is_rejected(tmp_path):
    p = tmp_path / "g.jsonl"
    p.write_text(json.dumps({
        "id": "a", "query": "q", "must_retrieve": ["d"], "k": 5, "overlap_band": "medium",
    }) + "\n")
    with pytest.raises(ValueError, match="overlap_band"):
        load_golden(p)


def test_invalid_provenance_is_rejected(tmp_path):
    p = tmp_path / "g.jsonl"
    p.write_text(json.dumps({
        "id": "a", "query": "q", "must_retrieve": ["d"], "k": 5, "provenance": "robot",
    }) + "\n")
    with pytest.raises(ValueError, match="provenance"):
        load_golden(p)
