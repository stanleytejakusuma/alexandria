"""Chunking: heading-aware, token-based, with breadcrumbs carried onto every chunk."""

import pytest

from pathlib import Path

from alexandria.index.chunker import (
    Chunk,
    chunk_document,
    count_tokens,
    is_indexable_source,
    split_headings,
)

DOC = """# Payments service

Intro paragraph about the service.

## Retry behaviour

The guard retries three times.

### Backoff

Exponential with jitter.

## Storage

Rows live in Postgres.
"""


def test_token_count_is_not_a_word_count():
    """A 0.75 words-per-token fudge breaks on code, tables and CJK. Count tokens."""
    assert count_tokens("hello world") > 0
    dense = count_tokens("def f(x): return {'a': [1,2,3]}")
    assert dense > len("def f(x): return {'a': [1,2,3]}".split())


def test_split_headings_builds_breadcrumbs():
    sections = split_headings(DOC)
    paths = [s.heading_path for s in sections]
    assert "Payments service" in paths[0]
    assert "Payments service > Retry behaviour" in paths
    assert "Payments service > Retry behaviour > Backoff" in paths
    assert "Payments service > Storage" in paths


def test_content_is_never_lost_across_sections():
    joined = "".join(s.text for s in split_headings(DOC))
    for marker in ["Intro paragraph", "retries three times", "Exponential with jitter",
                   "Rows live in Postgres"]:
        assert marker in joined


def test_document_without_headings_is_one_section():
    sections = split_headings("Just a body with no headings at all.\n")
    assert len(sections) == 1
    assert sections[0].heading_path == ""


def test_chunks_carry_doc_id_and_breadcrumb():
    chunks = chunk_document("wiki/systems/payments", DOC, max_tokens=64, overlap=0.15)
    assert chunks
    for c in chunks:
        assert c.doc_id == "wiki/systems/payments"
        assert c.chunk_id.startswith("wiki/systems/payments#")
    # packing spans headings, so the covered breadcrumbs live in heading_paths
    assert any("Retry behaviour" in path for c in chunks for path in c.heading_paths)


def test_chunks_respect_the_token_budget():
    body = "\n\n".join(f"Paragraph number {i} with some filler words in it." for i in range(80))
    chunks = chunk_document("sources/x/y", f"# Title\n\n{body}\n", max_tokens=100, overlap=0.15)
    assert len(chunks) > 1
    for c in chunks:
        assert count_tokens(c.text) <= 130      # budget + one paragraph of slack


def test_overlap_repeats_tail_context():
    body = "\n\n".join(f"Sentence {i} carries distinct content." for i in range(40))
    chunks = chunk_document("d", body, max_tokens=80, overlap=0.25)
    assert len(chunks) > 1
    # some text from the end of chunk N reappears at the start of chunk N+1
    assert any(chunks[i].text.split()[-4:] and
               any(w in chunks[i + 1].text for w in chunks[i].text.split()[-4:])
               for i in range(len(chunks) - 1))


def test_zero_overlap_is_allowed():
    chunks = chunk_document("d", "\n\n".join(f"Para {i}." for i in range(40)),
                            max_tokens=40, overlap=0.0)
    assert len(chunks) > 1


def test_a_single_oversized_paragraph_is_split_not_dropped():
    """A 5k-token wall of text must still be indexed."""
    giant = " ".join(f"word{i}" for i in range(4000))
    chunks = chunk_document("d", giant, max_tokens=100, overlap=0.1)
    assert len(chunks) > 1
    assert "word0" in chunks[0].text
    assert any("word3999" in c.text for c in chunks)


def test_empty_document_yields_no_chunks():
    assert chunk_document("d", "") == []
    assert chunk_document("d", "   \n\n  ") == []


def test_appledouble_metadata_is_not_an_indexable_source_in_either_tree():
    """Finder's ``._`` sidecars are metadata, never malformed corpus documents.

    Check both indexed top-level trees and a non-final component: the exclusion is
    deliberately about the final basename, so a real document under an ordinary
    directory is not discarded merely because an ancestor happens to resemble a
    metadata name.
    """
    assert not is_indexable_source(Path("sources/pi/._note.md"))
    assert not is_indexable_source(Path("wiki/topics/._overview.md"))
    assert is_indexable_source(Path("sources/._archive/real-note.md"))


def test_chunk_ids_are_stable_and_unique():
    a = chunk_document("d", DOC, max_tokens=64)
    b = chunk_document("d", DOC, max_tokens=64)
    assert [c.chunk_id for c in a] == [c.chunk_id for c in b]
    assert len({c.chunk_id for c in a}) == len(a)


def test_frontmatter_is_not_indexed():
    """Frontmatter is metadata for filtering, not prose to retrieve against."""
    doc = "---\ntype: observation\ntitle: T\n---\n\n# Heading\n\nReal body text here.\n"
    chunks = chunk_document("d", doc, max_tokens=64)
    joined = " ".join(c.text for c in chunks)
    assert "Real body text here." in joined
    assert "type: observation" not in joined


def test_small_sections_are_packed_not_one_chunk_each():
    """Emitting a chunk per section gave a median of 15 tokens on the real corpus --
    fragments too small to embed, and a 160k-chunk index for 23k documents."""
    doc = "\n".join(f"## Section {i}\n\nA short line about topic {i}.\n" for i in range(12))
    chunks = chunk_document("d", doc, max_tokens=512)
    assert len(chunks) < 4, f"expected packing, got {len(chunks)} chunks"
    joined = " ".join(c.text for c in chunks)
    for i in range(12):
        assert f"topic {i}" in joined          # packing must not lose content


def test_packing_still_respects_the_budget():
    doc = "\n".join(f"## S{i}\n\n{'filler words here ' * 20}\n" for i in range(20))
    for c in chunk_document("d", doc, max_tokens=100):
        assert count_tokens(c.text) <= 140


def test_identical_passages_keep_distinct_ids():
    doc = "# A\n\nrepeated boilerplate line\n\n## B\n\nrepeated boilerplate line\n"
    chunks = chunk_document("d", doc, max_tokens=8, overlap=0.0)
    assert len({c.chunk_id for c in chunks}) == len(chunks)


def test_packed_chunks_record_every_breadcrumb_they_cover():
    """Keeping only the first section's path would silently drop provenance."""
    doc = "\n".join(f"## Section {i}\n\nShort line {i}.\n" for i in range(6))
    chunks = chunk_document("d", doc, max_tokens=512)
    covered = {p for c in chunks for p in c.heading_paths}
    assert len(covered) == 6


def test_a_single_unbreakable_token_is_split_not_overflowed():
    """base64 blobs, hashes and CJK runs tokenize at many tokens per whitespace unit;
    falling through hands the embedder an over-length chunk to truncate silently."""
    blob = "A" * 20000
    chunks = chunk_document("d", f"prefix\n\n{blob}\n\nsuffix", max_tokens=100)
    assert all(count_tokens(c.text) <= 140 for c in chunks), \
        [count_tokens(c.text) for c in chunks]
    assert "prefix" in chunks[0].text
    assert sum(c.text.count("A") for c in chunks) >= 20000     # nothing dropped
