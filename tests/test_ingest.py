"""#51 multimodal ingest: any artifact becomes a searchable memory.

The pensieve principle -- a PDF, a screenshot, a paper, a resume must all be
storable. The architectural constraint is that NONE of it may disturb the
retrieval stack: the original binary is preserved, a companion markdown carries
the extracted text, and ONLY the markdown is indexed. Vector space, manifest,
Embedder protocol, store search, fusion and reranker stay untouched.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from alexandria.ingest import (
    COMPANION_ROOT,
    ExtractionFailed,
    UnsupportedArtifact,
    ingest_path,
)


# A minimal but REAL single-page PDF: born-digital, has a text layer, so the
# lossless pdftotext path is exercised rather than a mocked stub.
_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]"
    b"/Resources<</Font<</F1 4 0 R>>>>/Contents 5 0 R>>endobj\n"
    b"4 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
    b"5 0 obj<</Length 60>>stream\n"
    b"BT /F1 12 Tf 20 100 Td (Pensieve ingest smoke test) Tj ET\n"
    b"endstream endobj\n"
    b"trailer<</Root 1 0 R>>\n"
)


def _corpus(tmp_path: Path) -> Path:
    (tmp_path / "sources").mkdir(parents=True, exist_ok=True)
    return tmp_path


def test_a_pdf_is_preserved_verbatim_and_gets_a_searchable_companion(tmp_path):
    """The artifact IS the memory: the original bytes must survive ingest.

    A description alone is not enough -- a research paper or a resume has to be
    retrievable as a file afterwards, which is the whole point of storing it.
    """
    corpus = _corpus(tmp_path)
    src = tmp_path / "paper.pdf"
    src.write_bytes(_PDF)

    result = ingest_path(src, corpus)

    stored = corpus / result.asset_path
    assert stored.is_file(), "the original binary was not preserved"
    assert stored.read_bytes() == _PDF, "the stored artifact is not byte-identical"
    assert stored.suffix == ".pdf", "the preserved original must keep its real suffix"

    companion = corpus / result.doc_path
    assert companion.is_file()
    assert companion.suffix == ".md"


def test_the_preserved_binary_is_invisible_to_the_indexer_by_construction(tmp_path):
    """No is_indexable_source change is needed -- and none may be required.

    The indexer and /health both walk rglob("*.md"). A stored binary is ignored
    because it is not markdown, which is exactly why this design leaves the
    retrieval architecture untouched. If an asset ever gained a .md suffix it
    would silently enter the index as garbage, so the suffix is pinned here.
    """
    corpus = _corpus(tmp_path)
    src = tmp_path / "paper.pdf"
    src.write_bytes(_PDF)

    result = ingest_path(src, corpus)

    walked = {p.relative_to(corpus).as_posix() for p in corpus.rglob("*.md")}
    assert result.doc_path in walked, "the companion must be picked up by the .md walk"
    assert result.asset_path not in walked, "the binary must NOT be picked up"
    assert not (corpus / result.asset_path).name.endswith(".md")


def test_the_companion_carries_provenance_that_survives_in_frontmatter(tmp_path):
    """Provenance is what makes an ingested memory auditable later."""
    from alexandria.corpus import Doc

    corpus = _corpus(tmp_path)
    src = tmp_path / "paper.pdf"
    src.write_bytes(_PDF)

    result = ingest_path(src, corpus)
    doc = Doc.read(corpus / result.doc_path, corpus)
    fm = doc.frontmatter

    assert fm["type"] == "doc"
    assert fm["source"] == "ingest"
    assert fm["ingest"]["original_name"] == "paper.pdf"
    assert fm["ingest"]["sha256"] == hashlib.sha256(_PDF).hexdigest()
    assert fm["ingest"]["extraction"] == "pdftotext"
    assert fm["ingest"]["asset"] == result.asset_path, (
        "the companion must point back at the artifact so a hit can open it")


def test_the_extracted_text_is_actually_in_the_body_so_search_can_find_it(tmp_path):
    """A companion with no text is a memory that cannot be recalled."""
    from alexandria.corpus import Doc

    corpus = _corpus(tmp_path)
    src = tmp_path / "paper.pdf"
    src.write_bytes(_PDF)

    result = ingest_path(src, corpus)
    body = Doc.read(corpus / result.doc_path, corpus).body

    assert "Pensieve ingest smoke test" in body


def test_content_addressed_storage_deduplicates_the_same_artifact(tmp_path):
    """Re-ingesting the same bytes must not fork the memory."""
    corpus = _corpus(tmp_path)
    a = tmp_path / "one.pdf"
    b = tmp_path / "two.pdf"
    a.write_bytes(_PDF)
    b.write_bytes(_PDF)

    first = ingest_path(a, corpus)
    second = ingest_path(b, corpus)

    assert first.asset_path == second.asset_path, "identical bytes must share one asset"
    assert len(list((corpus / "assets").rglob("*.pdf"))) == 1


def test_an_unsupported_artifact_refuses_loudly_instead_of_indexing_nothing(tmp_path):
    corpus = _corpus(tmp_path)
    src = tmp_path / "mystery.bin"
    src.write_bytes(b"\x00\x01\x02")

    with pytest.raises(UnsupportedArtifact, match="mystery.bin"):
        ingest_path(src, corpus)


def test_a_failed_extraction_never_produces_a_silently_empty_memory(tmp_path):
    """THE failure this project keeps finding: reporting success while doing nothing.

    If extraction yields no text, ingest must refuse rather than write an empty
    companion that indexes cleanly and then answers nothing forever.
    """
    import alexandria.ingest as ing

    corpus = _corpus(tmp_path)
    src = tmp_path / "blank.pdf"
    src.write_bytes(_PDF)

    # A PDF that parses cleanly but carries no text layer (a scanned page with
    # no OCR is the real-world case): extractor succeeds, yields nothing.
    class Empty:
        returncode = 0
        stdout = "   \n  "
        stderr = ""

    monkeypatch_run = lambda *a, **k: Empty()
    original = ing.subprocess.run
    ing.subprocess.run = monkeypatch_run
    try:
        with pytest.raises(ExtractionFailed, match="no text"):
            ingest_path(src, corpus)
    finally:
        ing.subprocess.run = original

    assert list(corpus.rglob("*.md")) == [], "a failed ingest must leave no companion"
    assert [p for p in (corpus / "assets").rglob("*") if p.is_file()] == [], (
        "a failed ingest must not strand an artifact")


def test_an_image_routes_to_the_vision_extractor_and_records_that_method(tmp_path):
    """Images have no text layer, so they take the vision path -- injected here
    so the test stays offline and deterministic."""
    from alexandria.corpus import Doc

    corpus = _corpus(tmp_path)
    src = tmp_path / "screenshot.png"
    src.write_bytes(b"\x89PNG\r\n\x1a\n" + b"fake-pixels")

    calls = []

    def fake_vision(path: Path) -> str:
        calls.append(path)
        return "A dashboard screenshot showing deploy status."

    result = ingest_path(src, corpus, describe_image=fake_vision)

    assert calls == [src]
    doc = Doc.read(corpus / result.doc_path, corpus)
    assert "dashboard screenshot" in doc.body
    assert doc.frontmatter["ingest"]["extraction"] == "vision"


def test_an_unreachable_vision_gateway_fails_closed_rather_than_guessing(tmp_path):
    """Offline-degradable means REFUSE, not fabricate or silently succeed."""
    corpus = _corpus(tmp_path)
    src = tmp_path / "screenshot.png"
    src.write_bytes(b"\x89PNG\r\n\x1a\n" + b"fake-pixels")

    def broken_vision(path: Path) -> str:
        raise RuntimeError("gateway unreachable")

    with pytest.raises(ExtractionFailed, match="gateway unreachable"):
        ingest_path(src, corpus, describe_image=broken_vision)

    assert list(corpus.rglob("*.md")) == []
    assert list((corpus / "assets").rglob("*")) == [] or all(
        p.is_dir() for p in (corpus / "assets").rglob("*")
    ), "a failed ingest must not leave a stranded artifact"


# --- CLI surface -----------------------------------------------------------
# The weekly loop never calls ingest (it drives connectors), so manual and
# bridge-skill invocation must be pleasant: a file, a directory, or a glob.

def test_cli_ingests_a_single_file_and_reports_what_it_stored(tmp_path, capsys):
    from alexandria.cli import app

    corpus = _corpus(tmp_path)
    src = tmp_path / "paper.pdf"
    src.write_bytes(_PDF)

    assert app(["--corpus", str(corpus), "ingest", str(src)]) == 0
    out = capsys.readouterr().out
    assert "paper.pdf" in out
    assert "sources/assets/" in out, "the operator must see where the memory landed"


def test_cli_ingests_a_whole_directory_in_one_invocation(tmp_path, capsys):
    from alexandria.cli import app

    corpus = _corpus(tmp_path)
    drop = tmp_path / "drop"
    drop.mkdir()
    (drop / "a.pdf").write_bytes(_PDF)
    (drop / "b.pdf").write_bytes(_PDF.replace(b"smoke test", b"second doc"))

    assert app(["--corpus", str(corpus), "ingest", str(drop)]) == 0
    assert len(list((corpus / "sources/assets").glob("*.md"))) == 2


def test_cli_keeps_going_after_a_BROKEN_supported_file_and_exits_nonzero(tmp_path, capsys):
    """A batch must not lose every good artifact to one bad one -- and a
    supported artifact that failed extraction must still show up in the code.

    (A merely UNSUPPORTED file swept up by the directory walk is a different
    class and must NOT force non-zero; see the exit-code tests below.)
    """
    from alexandria.cli import app

    corpus = _corpus(tmp_path)
    drop = tmp_path / "drop"
    drop.mkdir()
    (drop / "good.pdf").write_bytes(_PDF)
    (drop / "broken.pdf").write_bytes(b"%PDF-1.4\nnot really a pdf\n")

    code = app(["--corpus", str(corpus), "ingest", str(drop)])

    assert len(list((corpus / "sources/assets").glob("*.md"))) == 1, "the good file landed"
    assert code != 0, "a broken SUPPORTED artifact must not read as a clean success"
    assert "broken.pdf" in capsys.readouterr().err


# --- durability + batch semantics (Red review round 1) ---------------------

def test_a_torn_asset_write_is_never_left_behind_or_treated_as_valid(tmp_path, monkeypatch):
    """The dedup check makes corruption PERMANENT, so the write must be atomic.

    shutil.copy2 straight to the content-addressed path leaves a truncated file
    if interrupted (Ctrl-C mid-batch). Every later ingest of the same bytes then
    hits `if not exists()` and skips the repair forever, while the companion
    asserts a sha256 those bytes do not have. Byte-faithful preservation is this
    component's entire job.
    """
    import alexandria.ingest as ing

    corpus = _corpus(tmp_path)
    src = tmp_path / "paper.pdf"
    src.write_bytes(_PDF)

    real_copy = ing.shutil.copy2

    def torn_copy(a, b, *args, **kwargs):
        Path(b).write_bytes(_PDF[: len(_PDF) // 2])   # partial write
        raise KeyboardInterrupt("interrupted mid-copy")

    monkeypatch.setattr(ing.shutil, "copy2", torn_copy)
    with pytest.raises(KeyboardInterrupt):
        ingest_path(src, corpus)

    monkeypatch.setattr(ing.shutil, "copy2", real_copy)
    leftovers = [p for p in (corpus / "assets").rglob("*") if p.is_file()]
    assert leftovers == [], f"a torn asset survived and would never be repaired: {leftovers}"

    # The retry must now produce the real, complete artifact.
    result = ingest_path(src, corpus)
    assert (corpus / result.asset_path).read_bytes() == _PDF


def test_a_preexisting_corrupt_asset_is_repaired_not_trusted(tmp_path):
    """Dedup must verify what is stored, not just that something is stored."""
    corpus = _corpus(tmp_path)
    src = tmp_path / "paper.pdf"
    src.write_bytes(_PDF)

    first = ingest_path(src, corpus)
    stored = corpus / first.asset_path
    stored.write_bytes(b"corrupted")          # simulate a torn/damaged asset

    again = ingest_path(src, corpus)
    assert (corpus / again.asset_path).read_bytes() == _PDF, (
        "a damaged asset was trusted on the dedup path instead of repaired")


def test_an_unreadable_file_is_skipped_without_killing_the_batch(tmp_path, capsys):
    """OSError must be a per-file skip: one bad file must not abandon the rest."""
    from alexandria.cli import app

    corpus = _corpus(tmp_path)
    drop = tmp_path / "drop"
    drop.mkdir()
    good = drop / "good.pdf"
    good.write_bytes(_PDF)
    bad = drop / "locked.pdf"
    bad.write_bytes(_PDF)
    bad.chmod(0o000)
    try:
        code = app(["--corpus", str(corpus), "ingest", str(drop)])
        assert len(list((corpus / "sources/assets").glob("*.md"))) == 1, "the good file landed"
        assert code != 0
        assert "locked.pdf" in capsys.readouterr().err
    finally:
        bad.chmod(0o644)


def test_a_directory_of_unsupported_files_is_not_a_failure(tmp_path, capsys):
    """Exit codes must stay meaningful.

    rglob("*") sweeps .DS_Store, notes.txt and friends; if every stray file
    forced exit 1, the operator learns to ignore the code and the signal for a
    REAL extraction failure is destroyed. Unsupported-by-sweep and
    supported-but-broken are different classes.
    """
    from alexandria.cli import app

    corpus = _corpus(tmp_path)
    drop = tmp_path / "drop"
    drop.mkdir()
    (drop / "good.pdf").write_bytes(_PDF)
    (drop / "notes.txt").write_text("not an artifact")
    (drop / ".DS_Store").write_bytes(b"\x00")

    code = app(["--corpus", str(corpus), "ingest", str(drop)])

    assert len(list((corpus / "sources/assets").glob("*.md"))) == 1
    assert code == 0, "sweeping past unsupported files in a directory is not a failure"


def test_an_explicitly_named_unsupported_file_still_fails(tmp_path, capsys):
    """Naming a file directly is an instruction; refusing it must be visible."""
    from alexandria.cli import app

    corpus = _corpus(tmp_path)
    src = tmp_path / "mystery.bin"
    src.write_bytes(b"\x00\x01")

    assert app(["--corpus", str(corpus), "ingest", str(src)]) != 0
    assert "mystery.bin" in capsys.readouterr().err


def test_the_same_bytes_under_a_new_name_stay_ONE_memory(tmp_path):
    """Content-addressed means one artifact AND one companion.

    Two companions with identical (source, source_id) but different slugs would
    be two documents claiming to be the same memory -- the duplicate-identity
    shape that produced a phantom shortfall before.
    """
    corpus = _corpus(tmp_path)
    a = tmp_path / "resume.pdf"
    b = tmp_path / "resume-final-v2.pdf"
    a.write_bytes(_PDF)
    b.write_bytes(_PDF)

    first = ingest_path(a, corpus)
    second = ingest_path(b, corpus)

    assert first.asset_path == second.asset_path
    assert first.doc_path == second.doc_path, "re-ingest under a new name forked the memory"
    assert len(list((corpus / "sources/assets").glob("*.md"))) == 1


def test_a_hostile_filename_cannot_break_out_of_frontmatter(tmp_path):
    """original_name is attacker-influenced text landing in YAML."""
    from alexandria.corpus import Doc

    corpus = _corpus(tmp_path)
    nasty = tmp_path / 'we"ird\nname.pdf'
    nasty.write_bytes(_PDF)

    result = ingest_path(nasty, corpus)
    doc = Doc.read(corpus / result.doc_path, corpus)   # must still parse

    assert doc.frontmatter["ingest"]["original_name"] == nasty.name
    assert doc.frontmatter["type"] == "doc"


def test_a_pdftotext_failure_is_not_accepted_as_partial_text(tmp_path, monkeypatch):
    """A non-zero extractor must not have its partial stdout stored as the memory."""
    import alexandria.ingest as ing

    corpus = _corpus(tmp_path)
    src = tmp_path / "corrupt.pdf"
    src.write_bytes(_PDF)

    class Result:
        returncode = 1
        stdout = "half a pa"
        stderr = "Syntax Error: damaged xref"

    monkeypatch.setattr(ing.subprocess, "run", lambda *a, **k: Result())
    with pytest.raises(ExtractionFailed, match="damaged xref"):
        ingest_path(src, corpus)


def test_a_missing_absolute_path_reports_cleanly_instead_of_crashing(tmp_path, capsys):
    from alexandria.cli import app

    corpus = _corpus(tmp_path)
    code = app(["--corpus", str(corpus), "ingest", "/definitely/not/here.pdf"])

    assert code != 0
    assert "no such path" in capsys.readouterr().err.lower()


# --- companion identity + memory stability (Red review round 2) ------------

def test_a_digest8_collision_can_never_overwrite_another_artifacts_memory(tmp_path):
    """The companion glob matches on 32 bits; a false hit would DESTROY a memory.

    A forged companion is actually WRITTEN here at the attacker's own digest8
    (so the attacker's lookup really does glob it up) but carrying a DIFFERENT
    full sha256. Pre-fix, reuse-by-prefix rewrote that file and the victim's
    memory silently became the attacker's. Post-fix, the full-sha confirmation
    rejects it and the attacker is diverted to its own path.
    """
    from alexandria.corpus import Doc

    corpus = _corpus(tmp_path)
    attacker = tmp_path / "attacker.pdf"
    attacker.write_bytes(_PDF.replace(b"smoke test", b"other doc"))
    attacker_sha = hashlib.sha256(attacker.read_bytes()).hexdigest()

    # A pre-existing memory whose companion name collides on digest[:8] with
    # the attacker's, but whose recorded identity is a different artifact.
    victim_sha = "f" * 64
    forged = corpus / COMPANION_ROOT / f"victim-{attacker_sha[:8]}.md"
    forged.parent.mkdir(parents=True, exist_ok=True)
    forged.write_text(
        "---\n"
        "type: doc\ntitle: victim\nsource: ingest\nsource_id: victimid\n"
        "generated:\n  by: connector/ingest\n  at: '2026-01-01T00:00:00+0000'\n"
        f"ingest:\n  sha256: {victim_sha}\n  asset: assets/ff/{victim_sha}.pdf\n"
        "---\nthe victim's irreplaceable memory\n",
        encoding="utf-8")
    before = forged.read_bytes()

    result = ingest_path(attacker, corpus)

    assert forged.read_bytes() == before, (
        "a digest8 collision overwrote another artifact's memory")
    assert result.doc_path != forged.relative_to(corpus).as_posix()
    assert Doc.read(corpus / result.doc_path, corpus).frontmatter["ingest"]["sha256"] == attacker_sha


def test_a_collision_widened_companion_is_still_found_on_re_ingest(tmp_path):
    """The seam between discovery and allocation must not disagree.

    Allocation widens a colliding name to digest[:12]; if discovery only globs
    digest[:8], that artifact is invisible forever -- so every re-ingest
    re-extracts (Gate 2 void for exactly the artifacts Gate 1 protects) and
    allocates ANOTHER companion, accumulating duplicates that assert one
    identity, until the widths run out and it hard-fails.
    """
    corpus = _corpus(tmp_path)
    art = tmp_path / "paper.pdf"
    art.write_bytes(_PDF)
    digest = hashlib.sha256(_PDF).hexdigest()

    # Occupy the exact digest[:8] name with a DIFFERENT artifact's memory,
    # forcing the allocator to widen.
    squatter = corpus / COMPANION_ROOT / f"paper-{digest[:8]}.md"
    squatter.parent.mkdir(parents=True, exist_ok=True)
    squatter.write_text(
        "---\ntype: doc\ntitle: squatter\nsource: ingest\nsource_id: sq\n"
        "generated:\n  by: connector/ingest\n  at: '2026-01-01T00:00:00+0000'\n"
        f"ingest:\n  sha256: {'a' * 64}\n  asset: assets/aa/x.pdf\n"
        "---\nsquatter body\n", encoding="utf-8")

    first = ingest_path(art, corpus)
    assert first.doc_path != squatter.relative_to(corpus).as_posix()

    calls = []
    real = _count_extractions(calls)
    second = ingest_path(art, corpus, describe_image=real)

    assert second.doc_path == first.doc_path, (
        "a widened companion was invisible on re-ingest -- duplicates would accumulate")
    assert len(list((corpus / COMPANION_ROOT).glob("paper-*.md"))) == 2, (
        "exactly the squatter plus one companion for this artifact")


def _count_extractions(calls):
    def describe(path):
        calls.append(path)
        return "should never be called for known bytes"
    return describe


def test_reingesting_known_bytes_does_not_re_extract_or_mutate_the_memory(tmp_path):
    """One artifact is one memory, and that memory must be STABLE.

    Re-running extraction on re-encounter is wasted work at best; with a
    nondeterministic vision extractor it silently rewrites a stored memory, and
    it clobbers any operator edits to the companion. Known bytes short-circuit
    BEFORE extraction.
    """
    from alexandria.corpus import Doc

    corpus = _corpus(tmp_path)
    src = tmp_path / "screenshot.png"
    src.write_bytes(b"\x89PNG\r\n\x1a\n" + b"fake-pixels")

    calls = []

    def vision(path):
        calls.append(path)
        return f"description number {len(calls)}"

    first = ingest_path(src, corpus, describe_image=vision)
    body_before = Doc.read(corpus / first.doc_path, corpus).body

    renamed = tmp_path / "renamed.png"
    renamed.write_bytes(src.read_bytes())          # identical bytes, new name
    again = ingest_path(renamed, corpus, describe_image=vision)

    assert calls == [src], "extraction re-ran on already-known bytes"
    assert again.doc_path == first.doc_path
    assert Doc.read(corpus / again.doc_path, corpus).body == body_before, (
        "re-ingest mutated a stored memory")


def test_a_directory_with_only_unsupported_files_is_not_a_silent_success(tmp_path, capsys):
    """The boundary of the new exit-code taxonomy: doing nothing must not read
    as success (exit 2), even though swept strays alone are not failures."""
    from alexandria.cli import app

    corpus = _corpus(tmp_path)
    drop = tmp_path / "drop"
    drop.mkdir()
    (drop / "notes.txt").write_text("nothing ingestable here")
    (drop / ".DS_Store").write_bytes(b"\x00")

    assert app(["--corpus", str(corpus), "ingest", str(drop)]) == 2
    assert "nothing to ingest" in capsys.readouterr().err


def test_restoring_a_lost_asset_never_rewrites_the_surviving_memory(tmp_path):
    """The side door: companion known, asset missing.

    The pre-extraction shortcut is skipped (the asset must be restored), so
    extraction re-runs and the post-write path would rewrite the Doc -- mutating
    a stored memory through a path Gate 2 does not cover. Restoring bytes needs
    a byte copy, not an extractor call whose output overwrites the survivor.
    """
    from alexandria.corpus import Doc

    corpus = _corpus(tmp_path)
    src = tmp_path / "screenshot.png"
    src.write_bytes(b"\x89PNG\r\n\x1a\n" + b"fake-pixels")

    calls = []

    def vision(path):
        calls.append(path)
        return f"description number {len(calls)}"

    first = ingest_path(src, corpus, describe_image=vision)
    body_before = Doc.read(corpus / first.doc_path, corpus).body
    (corpus / first.asset_path).unlink()          # the artifact is lost

    again = ingest_path(src, corpus, describe_image=vision)

    assert (corpus / again.asset_path).read_bytes() == src.read_bytes(), "asset not restored"
    assert again.doc_path == first.doc_path
    assert Doc.read(corpus / again.doc_path, corpus).body == body_before, (
        "restoring a lost asset rewrote the surviving memory")
    assert calls == [src], "restoring bytes should not re-run extraction"
