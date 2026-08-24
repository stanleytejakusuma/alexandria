"""Fault injection for the weekly-loop self-check.

A verifier that has never been seen to FAIL is not evidence of anything, so every
condition here is checked by deliberately breaking it. Each case corresponds to a
real 2026-08-11 failure that exit codes and log lines did not catch.
"""

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "verify-loop-run.py"

# Long enough to be a real query: a one-word or bare-wikilink title cannot rank
# against a large corpus, and the verifier skips it rather than failing on it.
TITLE = "Alpha document about gateway routing decisions"
STEM = "doc-alpha"


def _corpus(tmp_path: Path, *, generation: int = 3, commit_files: bool = True,
            indexed: bool = True, title: str = TITLE) -> Path:
    (tmp_path / "sources" / "x").mkdir(parents=True)
    (tmp_path / "wiki").mkdir()
    (tmp_path / ".alexandria" / "index").mkdir(parents=True)
    (tmp_path / "sources" / "x" / f"{STEM}.md").write_text(
        f"---\ntitle: {title}\n---\nbody\n", encoding="utf-8")
    (tmp_path / ".alexandria" / "index" / "generation.json").write_text(
        json.dumps({"generation": generation}), encoding="utf-8")

    con = sqlite3.connect(tmp_path / ".alexandria" / "index" / "fts.sqlite")
    con.execute("CREATE TABLE chunk_metadata (chunk_id TEXT PRIMARY KEY)")
    if indexed:
        con.execute("INSERT INTO chunk_metadata VALUES (?)", (f"sources/x/{STEM}#ab12cd34",))
    con.commit()
    con.close()

    git = ["git", "-C", str(tmp_path), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    if commit_files:
        subprocess.run(["git", "-C", str(tmp_path), "add", "sources"], check=True)
        subprocess.run(git + ["commit", "-q", "-m", "real"], check=True)
    else:
        subprocess.run(git + ["commit", "-q", "--allow-empty", "-m", "empty"], check=True)
    return tmp_path


def _finder(tmp_path: Path, *, finds: bool) -> Path:
    """Stand-in for the alexandria CLI: prints a hit line, or nothing."""
    p = tmp_path / ("find.sh" if finds else "miss.sh")
    body = f'echo "1. sources/x/{STEM}#ab12cd34  score=0.99"' if finds else 'echo "no hits"'
    p.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    p.chmod(0o755)
    return p


def _run(corpus: Path, binary: Path, docs_before: int, gen_before: int):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--corpus", str(corpus), "--binary", str(binary),
         "--docs-before", str(docs_before), "--generation-before", str(gen_before)],
        capture_output=True, text=True)


def test_a_healthy_run_passes(tmp_path):
    out = _run(_corpus(tmp_path / "c"), _finder(tmp_path, finds=True), 1, 3)
    assert out.returncode == 0, out.stdout
    assert "LOOP VERIFY: PASS" in out.stdout


def test_documents_disappearing_fails(tmp_path):
    out = _run(_corpus(tmp_path / "c"), _finder(tmp_path, finds=True), 500, 3)
    assert out.returncode == 1
    assert "documents" in out.stdout


def test_new_documents_with_an_unmoved_index_generation_fails(tmp_path):
    """The three-day freeze: documents on disk, invisible to every query."""
    out = _run(_corpus(tmp_path / "c", generation=3), _finder(tmp_path, finds=True), 0, 3)
    assert out.returncode == 1
    assert "the index did not move" in out.stdout


def test_a_document_absent_from_the_index_fails(tmp_path):
    """Membership is exact and required of EVERY probe -- no ranking involved, so
    a document on disk with no chunks is unambiguously an indexing failure."""
    out = _run(_corpus(tmp_path / "c", indexed=False), _finder(tmp_path, finds=True), 1, 3)
    assert out.returncode == 1
    assert "0/1 newest documents have chunks" in out.stdout


def test_an_indexed_document_that_search_cannot_surface_fails(tmp_path):
    """Indexed but unsearchable is a DIFFERENT failure from not indexed, which is
    why the two checks are kept apart."""
    out = _run(_corpus(tmp_path / "c"), _finder(tmp_path, finds=False), 1, 3)
    assert out.returncode == 1
    assert "1/1 newest documents have chunks" in out.stdout, "index membership should pass"
    assert "not in top-" in out.stdout


def test_a_degenerate_title_is_skipped_rather_than_failed(tmp_path):
    """The vault really contains notes titled `[[none]]` and `[[TOOL]]` holding
    420 and 103 chunks. They are correctly indexed; their titles are simply not
    queries. Failing on them reported a healthy corpus as broken."""
    corpus = _corpus(tmp_path / "c", title="'[[none]]'")
    out = _run(corpus, _finder(tmp_path, finds=False), 1, 3)
    assert out.returncode == 0, out.stdout
    assert "skipped" in out.stdout


def test_an_empty_commit_after_new_documents_fails(tmp_path):
    """`git add` staged nothing while --allow-empty committed anyway."""
    out = _run(_corpus(tmp_path / "c", commit_files=False), _finder(tmp_path, finds=True), 0, 3)
    assert out.returncode == 1
    assert "new documents were not committed" in out.stdout


def test_a_stem_appearing_as_a_bare_substring_is_not_counted_as_a_hit(tmp_path):
    """`stem in output` reported a hit whenever the stem occurred anywhere -- even
    in the echoed query. The chunk-id shape `/<stem>#` must be required."""
    liar = tmp_path / "liar.sh"
    liar.write_text(f"#!/bin/sh\necho 'mentions {STEM} without a chunk id'\n", encoding="utf-8")
    liar.chmod(0o755)
    out = _run(_corpus(tmp_path / "c"), liar, 1, 3)
    assert out.returncode == 1, "a bare substring must not count as retrieval"


def test_verify_ignores_appledouble_sidecars(tmp_path):
    """AppleDouble sidecar files in the corpus must not be treated as newest
    documents: they are transfer artifacts with no chunks, and a sync that only
    refreshes them would fail the loop every week (observed on the NAS,
    2026-08-24: 4,358 ingested `._<store>-*` files failed the indexed and
    retrievable checks)."""
    corpus = _corpus(tmp_path, generation=3)
    side = tmp_path / "sources" / "x" / "._zzz-newest-sidecar.md"
    side.write_text("\x00\x05\x16\x07binary AppleDouble junk\n", encoding="utf-8")
    # Make the sidecar the newest file so pre-fix newest_docs() picks it.
    import os
    os.utime(side, (2_000_000_000, 2_000_000_000))
    os.utime(tmp_path / "sources" / "x" / f"{STEM}.md", (1_000_000_000, 1_000_000_000))
    git = ["git", "-C", str(tmp_path), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(["git", "-C", str(tmp_path), "add", "sources"], check=True)
    subprocess.run(git + ["commit", "-q", "-m", "sidecar"], check=True)

    # docs_before counts only the real document: the sidecar is new and
    # committed, so the loop must pass with a bumped generation.
    r = _run(corpus, _finder(tmp_path, finds=True), docs_before=1, gen_before=2)
    assert r.returncode == 0, f"verify failed on sidecar-only newest docs:\n{r.stdout}\n{r.stderr}"
    assert "[FAIL]" not in r.stdout


def test_verify_reads_the_active_release_fts_not_the_legacy_file(tmp_path):
    """Since the staged-release cutover the live FTS lives under
    .alexandria/index/releases/<id>/. The verify must probe THAT file: the
    legacy flat .alexandria/index/fts.sqlite is frozen before the first
    release and never gains chunks, so probing it false-FAILs every post-
    cutover run (observed on the NAS 2026-08-24: newest docs had chunks in
    the active release, verify reported 0/5)."""
    corpus = _corpus(tmp_path, generation=3)
    # Remove the legacy fts and place a fresh one inside an active release --
    # exactly the post-cutover layout. The stale legacy file exists too and
    # holds NO rows for the document.
    legacy = tmp_path / ".alexandria" / "index" / "fts.sqlite"
    legacy.unlink()
    rel = tmp_path / ".alexandria" / "index" / "releases" / "20260824T000000-deadbeef"
    rel.mkdir(parents=True)
    con = sqlite3.connect(rel / "fts.sqlite")
    con.execute("CREATE TABLE chunk_metadata (chunk_id TEXT PRIMARY KEY)")
    con.execute("INSERT INTO chunk_metadata VALUES (?)", (f"sources/x/{STEM}#ab12cd34",))
    con.commit()
    con.close()
    (tmp_path / ".alexandria" / "index" / "active.json").write_text(
        json.dumps({"activated_at": "2026-08-24T00:00:00+0000",
                    "release_id": "20260824T000000-deadbeef"}))
    git = ["git", "-C", str(tmp_path), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(git + ["commit", "-q", "-m", "release"], check=True)

    r = _run(corpus, _finder(tmp_path, finds=True), docs_before=1, gen_before=2)
    assert r.returncode == 0, f"verify failed against the active release:\n{r.stdout}\n{r.stderr}"
    assert "[FAIL]" not in r.stdout


def test_verify_fails_loud_when_active_release_fts_is_missing(tmp_path):
    """A release pointer whose fts.sqlite is gone must fail the run, not
    silently fall back to the legacy file (which would mask a broken index)."""
    corpus = _corpus(tmp_path, generation=3)
    rel = tmp_path / ".alexandria" / "index" / "releases" / "20260824T000000-deadbeef"
    rel.mkdir(parents=True)
    (tmp_path / ".alexandria" / "index" / "active.json").write_text(
        json.dumps({"activated_at": "2026-08-24T00:00:00+0000",
                    "release_id": "20260824T000000-deadbeef"}))
    git = ["git", "-C", str(tmp_path), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(git + ["commit", "-q", "-m", "release"], check=True)

    r = _run(corpus, _finder(tmp_path, finds=True), docs_before=1, gen_before=2)
    assert r.returncode == 1
    assert "[FAIL] indexed" in r.stdout
