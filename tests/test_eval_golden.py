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
