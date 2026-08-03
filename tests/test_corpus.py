"""Document IO, ids, and the immutability guard."""

import pytest

from alexandria.corpus import (
    Doc,
    body_hash,
    doc_id,
    render,
    slugify,
    source_filename,
    split_frontmatter,
)

SAMPLE = """---
type: observation
title: A thing
tags:
  - one
  - two
---

Body line one.

Body line two.
"""


def test_split_frontmatter():
    fm, body = split_frontmatter(SAMPLE)
    assert fm["type"] == "observation"
    assert fm["tags"] == ["one", "two"]
    assert body.strip().startswith("Body line one.")


def test_no_frontmatter_returns_none():
    fm, body = split_frontmatter("Just a body, no frontmatter.\n")
    assert fm is None
    assert body.startswith("Just a body")


def test_unterminated_frontmatter_is_not_frontmatter():
    fm, body = split_frontmatter("---\ntype: observation\nno closing fence\n")
    assert fm is None


def test_non_mapping_frontmatter_returns_none():
    fm, _ = split_frontmatter("---\n- a\n- b\n---\nbody\n")
    assert fm is None


def test_round_trip_preserves_body_exactly():
    fm, body = split_frontmatter(SAMPLE)
    out = render(fm, body)
    fm2, body2 = split_frontmatter(out)
    assert fm2 == fm
    assert body2 == body
    assert body_hash(body2) == body_hash(body)


def test_doc_id_is_path_minus_extension():
    assert doc_id("sources/pi/pi-1-a-note.md") == "sources/pi/pi-1-a-note"
    assert doc_id("wiki/systems/thing.md") == "wiki/systems/thing"
    assert doc_id("./sources/pi/x.md") == "sources/pi/x"


def test_body_hash_ignores_trailing_whitespace_only():
    assert body_hash("hello\n") == body_hash("hello\n\n\n")
    assert body_hash("  hello") != body_hash("hello")


def test_body_hash_detects_real_edits():
    """The immutability tripwire: a changed body must change the hash."""
    assert body_hash("The guard failed open.") != body_hash("The guard failed closed.")


def test_slugify():
    assert slugify("Solved: the DB schema gotcha!") == "solved-the-db-schema-gotcha"
    assert slugify("  multiple   spaces  ") == "multiple-spaces"
    assert slugify("émoji 🚀 test") == "emoji-test"
    assert slugify("") == "untitled"
    assert len(slugify("x" * 200)) <= 60


def test_source_filename_shape():
    name = source_filename("pi-sessions", "abc123", "A thing that happened")
    assert name == "pi-sessions-abc123-a-thing-that-happened.md"


def test_source_filename_is_filesystem_safe():
    name = source_filename("pi-sessions", "a/b:c", "Title/with slashes")
    assert "/" not in name.replace(".md", "")
    assert ":" not in name


def test_doc_write_read_round_trip(tmp_path):
    d = Doc(
        path="sources/pi/pi-1-x.md",
        frontmatter={"type": "observation", "title": "X",
                     "generated": {"by": "connector/pi-sessions", "at": "2026-07-31T00:00:00Z"},
                     "source": "pi-sessions", "source_id": "1"},
        body="Some body.\n",
    )
    d.write(tmp_path)
    back = Doc.read(tmp_path / d.path, root=tmp_path)
    assert back.frontmatter == d.frontmatter
    assert back.body == d.body
    assert back.doc_id == "sources/pi/pi-1-x"


def test_doc_read_rejects_missing_frontmatter(tmp_path):
    p = tmp_path / "sources" / "pi" / "bad.md"
    p.parent.mkdir(parents=True)
    p.write_text("no frontmatter here\n")
    with pytest.raises(ValueError, match="frontmatter"):
        Doc.read(p, root=tmp_path)
