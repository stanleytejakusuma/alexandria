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
    _sha256_bytes,
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
def test_re_extract_mode_prints_that_it_is_overwriting_a_stored_memory(tmp_path, capsys):
    """The destructive mode is the one that must be loud (metadata-only is
    not destructive, so it needs no alarm)."""
    corpus = _corpus(tmp_path)
    src = tmp_path / "paper.pdf"
    src.write_bytes(_PDF)
    first = ingest_path(src, corpus)
    refresh_ingest(corpus, first.asset_path, re_extract=True)
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


# ---------------------------------------------------------------------------
# Red review, 2026-08-20: --refresh must NOT silently destroy the exact state
# the default ingest path protects. The motivating use case (backfilling
# `pages`) only needs SYSTEM-OWNED metadata; the companion body and any
# operator edits must survive. Full re-extraction becomes an explicit,
# separately-gated destructive mode.
# ---------------------------------------------------------------------------

@requires_pdftotext
def test_default_refresh_updates_metadata_and_preserves_an_edited_body(tmp_path, monkeypatch, capsys):
    """THE review finding, fixed: the default refresh must be metadata-only.
    An operator-edited body survives; only system-owned provenance fields
    (pages, extraction, generated.at) change."""
    corpus = _corpus(tmp_path)
    src = tmp_path / "paper.pdf"
    src.write_bytes(_PDF)
    first = ingest_path(src, corpus)
    doc_path = first.doc_path

    # simulate an operator edit to the companion body
    doc = corpus / doc_path
    doc.write_text(doc.read_text().replace(
        "Refresh test paper", "OPERATOR EDITED: this is the real meaning"))

    monkeypatch.setattr("alexandria.ingest._pdf_page_count", lambda p: 7)
    result = refresh_ingest(corpus, first.asset_path)  # default: re_extract=False

    rewritten = (corpus / result.doc_path).read_text()
    assert "OPERATOR EDITED" in rewritten, "the default refresh must NOT clobber operator edits"
    fm = yaml.safe_load(rewritten.split("---")[1])
    assert fm["ingest"]["pages"] == 7, "the metadata backfill must still land"


@requires_pdftotext
def test_re_extract_mode_does_a_full_regeneration(tmp_path, monkeypatch, capsys):
    """The destructive mode still exists, but only when EXPLICITLY asked for
    (re_extract=True), with the loud warning."""
    corpus = _corpus(tmp_path)
    src = tmp_path / "paper.pdf"
    src.write_bytes(_PDF)
    first = ingest_path(src, corpus)

    monkeypatch.setattr("alexandria.ingest._extract_pdf_text", lambda p: "FULLY REGENERATED body")
    result = refresh_ingest(corpus, first.asset_path, re_extract=True)

    rewritten = (corpus / result.doc_path).read_text()
    assert "FULLY REGENERATED body" in rewritten
    out = capsys.readouterr().out
    assert "overwrit" in out.lower() or "re-extract" in out.lower(),         "the destructive mode must still be loud"


@requires_pdftotext
def test_cli_re_extract_flag_regenerates_the_body(tmp_path, monkeypatch):
    """The destructive mode is reachable from the CLI only via the explicit
    --re-extract flag, proving the default is metadata-only even at the
    command-line layer."""
    import yaml as _y
    corpus = _corpus(tmp_path)
    src = tmp_path / "paper.pdf"
    src.write_bytes(_PDF)
    first = ingest_path(src, corpus)

    # an operator edit, then a metadata-only CLI refresh must preserve it
    doc_path = corpus / first.doc_path
    doc_path.write_text(doc_path.read_text().replace("Refresh test paper", "OPERATOR KEEPS THIS"))
    monkeypatch.setattr("alexandria.ingest._pdf_page_count", lambda p: 3)
    assert app(["--corpus", str(corpus), "ingest", "--refresh", str(corpus / first.asset_path)]) == 0
    assert "OPERATOR KEEPS THIS" in doc_path.read_text()

    # now the destructive flag: body regenerates, edit lost
    monkeypatch.setattr("alexandria.ingest._extract_pdf_text", lambda p: "REGENERATED FROM SCRATCH")
    assert app(["--corpus", str(corpus), "ingest", "--refresh", "--re-extract",
                str(corpus / first.asset_path)]) == 0
    rewritten = doc_path.read_text()
    assert "REGENERATED FROM SCRATCH" in rewritten
    assert "OPERATOR KEEPS THIS" not in rewritten,         "--re-extract must actually replace the body (and is the only way to)"


@requires_pdftotext
def test_refresh_refuses_when_the_companion_digest_disagrees_with_the_asset(tmp_path, monkeypatch):
    """Red review: refresh must validate the FULL canonical identity tuple,
    not just the filename. A companion whose recorded sha256 differs from the
    asset's asserted filename digest must be refused as ambiguous/corrupt."""
    corpus = _corpus(tmp_path)
    src = tmp_path / "paper.pdf"
    src.write_bytes(_PDF)
    first = ingest_path(src, corpus)

    # forge a disagreement: change the companion's recorded sha256 to something
    # that does NOT match the asset filename's asserted digest
    doc_path = corpus / first.doc_path
    text = doc_path.read_text()
    import re as _re
    text = text.replace(first.sha256, "f" * 64)
    doc_path.write_text(text)

    with pytest.raises(ExtractionFailed, match="companion"):
        refresh_ingest(corpus, first.asset_path)


@requires_pdftotext
def test_refresh_refuses_when_two_companions_claim_the_same_asset(tmp_path, monkeypatch):
    """Red review, finding #2: refresh must fail on AMBIGUOUS companion
    selection, not pick the first one. Two companions claiming the same
    digest is a duplicate state the at-rest lint flags; refresh must refuse
    to operate in it rather than silently choosing one."""
    corpus = _corpus(tmp_path)
    src = tmp_path / "paper.pdf"
    src.write_bytes(_PDF)
    first = ingest_path(src, corpus)

    # forge a second companion claiming the same asset (duplicate digest) --
    # the filename MUST carry the digest prefix, or it isn't even a candidate
    # for _find_companions (matching the allocator's real naming scheme)
    dup = corpus / "sources" / "assets" / f"duplicate-paper-{first.sha256[:8]}.md"
    dup.write_text((corpus / first.doc_path).read_text())

    with pytest.raises(ExtractionFailed, match="ambigu|duplicate|more than one"):
        refresh_ingest(corpus, first.asset_path)


@requires_pdftotext
def test_re_extract_extracts_from_a_verified_snapshot_not_the_mutating_original(tmp_path, monkeypatch):
    """Red review, finding #3 (TOCTOU): in re-extract mode, the extractor
    must consume the EXACT bytes that were verified -- never a path that a
    concurrent writer could have mutated in between. This test proves the
    snapshot is what reaches the extractor by mutating the original AFTER
    verification is impossible to observe directly, so it instead verifies
    the mechanism: the extractor receives a DIFFERENT path than the asset
    (the snapshot), and the snapshot's bytes are what was verified."""
    corpus = _corpus(tmp_path)
    src = tmp_path / "paper.pdf"
    src.write_bytes(_PDF)
    first = ingest_path(src, corpus)

    extracted_from = []

    def capturing_extract(path):
        extracted_from.append(Path(path))
        # the original asset, if mutated AFTER verification, must NOT be what
        # we read here -- this extractor asserts the snapshot's digest holds
        actual = _sha256_bytes(Path(path).read_bytes())
        assert actual == first.sha256,             "extractor consumed bytes that do not match the verified digest"
        return "snapshot-verified extraction"

    monkeypatch.setattr("alexandria.ingest._extract_pdf_text", capturing_extract)
    refresh_ingest(corpus, first.asset_path, re_extract=True)

    assert extracted_from and extracted_from[0] != (corpus / first.asset_path), (
        "re-extract must run the extractor against a private snapshot, "
        "not the live asset path")
    rewritten = (corpus / first.doc_path).read_text()
    assert "snapshot-verified extraction" in rewritten
