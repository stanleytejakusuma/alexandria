"""#54: alexandria ingest --refresh -- re-derive a companion for ALREADY-
ingested bytes, plus the at-rest lints Red deferred out of #52.

Contract (decided in the goal audit for #51/#52):
- --refresh re-extracts and rewrites ONLY when explicitly asked -- the
  default ingest path is a stable, content-addressed no-op by design, so a
  new provenance field (like `pages`, added in #51's second commit) can never
  reach an artifact ingested before it existed without an explicit refresh.
- it preserves the existing doc path (nothing forks into a second memory)
- it refuses if the asset no longer hashes to its recorded sha256 (corruption
  must never be silently re-extracted into a companion asserting the old sha)
- it is LOUD that it is overwriting a stored memory -- the thing the default
  path forbids for a documented reason.
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from alexandria.cli import app
from alexandria.corpus import Doc
from alexandria.ingest import (
    ExtractionFailed,
    UnsupportedArtifact,
    ingest_path,
    refresh_ingest,
)

requires_pdftotext = pytest.mark.skipif(
    shutil.which("pdftotext") is None, reason="pdftotext (poppler) not installed")

_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]"
    b"/Resources<</Font<</F1 4 0 R>>>>/Contents 5 0 R>>endobj\n"
    b"4 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
    b"5 0 obj<</Length 60>>stream\n"
    b"BT /F1 12 Tf 20 100 Td (Refresh test paper) Tj ET\n"
    b"endstream endobj\n"
    b"trailer<</Root 1 0 R>>\n"
)


def _corpus(tmp_path: Path) -> Path:
    (tmp_path / "sources").mkdir(parents=True, exist_ok=True)
    return tmp_path


# ---------------------------------------------------------------------------
# refresh_ingest: the core function
# ---------------------------------------------------------------------------

@requires_pdftotext
def test_refresh_rewrites_the_companion_in_place(tmp_path, monkeypatch):
    corpus = _corpus(tmp_path)
    src = tmp_path / "paper.pdf"
    src.write_bytes(_PDF)
    first = ingest_path(src, corpus)
    doc_path = first.doc_path

    monkeypatch.setattr("alexandria.ingest._pdf_page_count", lambda p: 7)
    result = refresh_ingest(corpus, first.asset_path)

    assert result.doc_path == doc_path, "refresh must not fork a second memory"
    fm = Doc.read(corpus / doc_path, corpus).frontmatter
    assert fm["ingest"]["pages"] == 7


@requires_pdftotext
def test_refresh_refuses_when_the_asset_no_longer_matches_its_sha256(tmp_path):
    corpus = _corpus(tmp_path)
    src = tmp_path / "paper.pdf"
    src.write_bytes(_PDF)
    first = ingest_path(src, corpus)
    (corpus / first.asset_path).write_bytes(b"corrupted, not the original bytes")

    with pytest.raises(ExtractionFailed, match="sha256|digest|corrupt"):
        refresh_ingest(corpus, first.asset_path)
    # the companion must be untouched by a refused refresh
    fm = Doc.read(corpus / first.doc_path, corpus).frontmatter
    assert fm["ingest"]["sha256"] == first.sha256


def test_refresh_refuses_an_asset_with_no_companion(tmp_path):
    corpus = _corpus(tmp_path)
    (corpus / "assets" / "ab").mkdir(parents=True)
    orphan = corpus / "assets" / "ab" / "abcd1234.pdf"
    orphan.write_bytes(_PDF)
    with pytest.raises(ExtractionFailed, match="companion|memory"):
        refresh_ingest(corpus, "assets/ab/abcd1234.pdf")


@requires_pdftotext
def test_refresh_prints_that_it_is_overwriting_a_stored_memory(tmp_path, capsys):
    corpus = _corpus(tmp_path)
    src = tmp_path / "paper.pdf"
    src.write_bytes(_PDF)
    first = ingest_path(src, corpus)
    refresh_ingest(corpus, first.asset_path)
    out = capsys.readouterr().out
    assert "overwrit" in out.lower() or "refresh" in out.lower()


@requires_pdftotext
def test_a_plain_ingest_after_the_fact_still_does_not_refresh(tmp_path, monkeypatch):
    """The DEFAULT path stays a no-op -- refresh must be opt-in, never
    triggered implicitly by ingesting the same bytes again."""
    corpus = _corpus(tmp_path)
    src = tmp_path / "paper.pdf"
    src.write_bytes(_PDF)
    ingest_path(src, corpus)
    monkeypatch.setattr("alexandria.ingest._pdf_page_count", lambda p: 999)
    second = ingest_path(src, corpus)
    fm = Doc.read(corpus / second.doc_path, corpus).frontmatter
    assert fm["ingest"].get("pages") != 999


# ---------------------------------------------------------------------------
# CLI: --refresh flag
# ---------------------------------------------------------------------------

@requires_pdftotext
def test_cli_ingest_refresh_flag_rewrites_a_named_asset(tmp_path, monkeypatch, capsys):
    corpus = _corpus(tmp_path)
    src = tmp_path / "paper.pdf"
    src.write_bytes(_PDF)
    first = ingest_path(src, corpus)
    monkeypatch.setattr("alexandria.ingest._pdf_page_count", lambda p: 3)

    rc = app(["--corpus", str(corpus), "ingest", "--refresh",
             str(corpus / first.asset_path)])
    assert rc == 0
    fm = Doc.read(corpus / first.doc_path, corpus).frontmatter
    assert fm["ingest"]["pages"] == 3


def test_cli_ingest_refresh_on_a_never_ingested_path_fails_loudly(tmp_path):
    corpus = _corpus(tmp_path)
    rc = app(["--corpus", str(corpus), "ingest", "--refresh",
             str(tmp_path / "assets" / "zz" / "nope.pdf")])
    assert rc != 0
