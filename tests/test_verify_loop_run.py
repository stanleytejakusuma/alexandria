"""Fault injection for the weekly-loop self-check.

A verifier that has never been seen to FAIL is not evidence of anything, so every
condition here is checked by deliberately breaking it. Each case corresponds to a
real 2026-08-11 failure that exit codes and log lines did not catch.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "verify-loop-run.py"


def _corpus(tmp_path: Path, *, generation: int = 3, commit_files: bool = True) -> Path:
    (tmp_path / "sources" / "x").mkdir(parents=True)
    (tmp_path / "wiki").mkdir()
    (tmp_path / ".alexandria" / "index").mkdir(parents=True)
    (tmp_path / "sources" / "x" / "doc-alpha.md").write_text(
        "---\ntitle: Alpha doc\n---\nbody\n", encoding="utf-8")
    (tmp_path / ".alexandria" / "index" / "generation.json").write_text(
        json.dumps({"generation": generation}), encoding="utf-8")
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
    body = 'echo "1. sources/x/doc-alpha#ab12cd34  score=0.99"' if finds else 'echo "no hits"'
    p.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    p.chmod(0o755)
    return p


def _run(corpus: Path, binary: Path, docs_before: int, gen_before: int):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--corpus", str(corpus), "--binary", str(binary),
         "--docs-before", str(docs_before), "--generation-before", str(gen_before)],
        capture_output=True, text=True)


def test_a_healthy_run_passes(tmp_path):
    corpus = _corpus(tmp_path / "c")
    out = _run(corpus, _finder(tmp_path, finds=True), 1, 3)
    assert out.returncode == 0, out.stdout
    assert "LOOP VERIFY: PASS" in out.stdout


def test_documents_disappearing_fails(tmp_path):
    corpus = _corpus(tmp_path / "c")
    out = _run(corpus, _finder(tmp_path, finds=True), 500, 3)
    assert out.returncode == 1
    assert "documents" in out.stdout and "FAIL" in out.stdout


def test_new_documents_with_an_unmoved_index_generation_fails(tmp_path):
    """The three-day freeze: documents on disk, invisible to every query."""
    corpus = _corpus(tmp_path / "c", generation=3)
    out = _run(corpus, _finder(tmp_path, finds=True), 0, 3)
    assert out.returncode == 1
    assert "index generation" in out.stdout
    assert "the index did not move" in out.stdout


def test_an_unretrievable_corpus_fails(tmp_path):
    corpus = _corpus(tmp_path / "c")
    out = _run(corpus, _finder(tmp_path, finds=False), 1, 3)
    assert out.returncode == 1
    assert "0/1 of the newest documents found" in out.stdout


def test_an_empty_commit_after_new_documents_fails(tmp_path):
    """`git add` staged nothing while --allow-empty committed anyway."""
    corpus = _corpus(tmp_path / "c", commit_files=False)
    out = _run(corpus, _finder(tmp_path, finds=True), 0, 3)
    assert out.returncode == 1
    assert "new documents were not committed" in out.stdout


def test_a_stem_appearing_as_a_bare_substring_is_not_counted_as_a_hit(tmp_path):
    """`stem in output` reported a hit whenever the stem occurred anywhere -- even
    in the echoed query. The chunk-id shape `/<stem>#` must be required."""
    corpus = _corpus(tmp_path / "c")
    liar = tmp_path / "liar.sh"
    liar.write_text("#!/bin/sh\necho 'mentions doc-alpha without a chunk id'\n", encoding="utf-8")
    liar.chmod(0o755)
    out = _run(corpus, liar, 1, 3)
    assert out.returncode == 1, "a bare substring must not count as retrieval"
