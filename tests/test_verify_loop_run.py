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
