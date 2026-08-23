"""C5 freshness (spec C5, gate G5): a frozen corpus produces a failing check.

Quality metrics do not detect liveness failures -- recall@k on a fixed
golden set stays green forever on a frozen corpus (the 2026-08-11 weekly-loop
incident). These tests pin the staleness gate: content freshness (newest
indexable source mtime) and index freshness (generation.json finished_at),
failing loudly past a threshold.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from alexandria import cli
from alexandria.staleness import (
    FRESHNESS_DEFAULT_MAX_AGE_DAYS,
    check,
    newest_index_finished_at,
    newest_source_mtime,
)


def _make_corpus(tmp_path: Path) -> Path:
    corpus = tmp_path / "corpus"
    (corpus / "sources").mkdir(parents=True)
    (corpus / "wiki").mkdir()
    return corpus


def _write_doc(corpus: Path, rel: str, text: str = "content\n") -> Path:
    p = corpus / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def _age(path: Path, days: float) -> None:
    old = time.time() - days * 24 * 3600
    os.utime(path, (old, old))


def _write_generation(corpus: Path, finished_at: str) -> None:
    p = corpus / ".alexandria" / "index" / "generation.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"generation": 1, "finished_at": finished_at}))


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def test_g5_a_frozen_corpus_fails_the_freshness_check(tmp_path: Path):
    """Gate C5: freezing a test corpus past the threshold produces a
    failing check -- the exact failure class of the 2026-08-11 incident."""
    corpus = _make_corpus(tmp_path)
    doc = _write_doc(corpus, "sources/a.md")
    _age(doc, FRESHNESS_DEFAULT_MAX_AGE_DAYS + 3)
    _write_generation(corpus, _now_iso())

    report = check(corpus)
    assert report.ok is False
    assert any("newest document" in r for r in report.reasons)
    assert report.content_age_seconds > FRESHNESS_DEFAULT_MAX_AGE_DAYS * 24 * 3600


def test_a_fresh_corpus_passes(tmp_path: Path):
    corpus = _make_corpus(tmp_path)
    _write_doc(corpus, "sources/a.md")  # mtime = now
    _write_generation(corpus, _now_iso())
    assert check(corpus).ok is True


def test_the_threshold_is_configurable(tmp_path: Path):
    """Tiny threshold trips on fresh content; the same content passes the
    default threshold -- proves the comparison is real, not a tautology."""
    corpus = _make_corpus(tmp_path)
    doc = _write_doc(corpus, "sources/a.md")
    _write_generation(corpus, _now_iso())
    _age(doc, 2 / 86400)  # content is 2 seconds old
    assert check(corpus, max_age_seconds=1.0).ok is False  # 1s threshold trips
    assert check(corpus).ok is True  # default threshold passes


def test_an_empty_corpus_is_not_stale(tmp_path: Path):
    """No indexable content -> nothing to measure -> healthy, with a reason."""
    corpus = _make_corpus(tmp_path)
    report = check(corpus)
    assert report.ok is True
    assert any("no indexable documents" in r for r in report.reasons)


def test_content_arriving_but_index_never_finished_fails(tmp_path: Path):
    """The other liveness half: content exists but generation.json has no
    finished_at -- new documents may be unsearchable and recall cannot see it."""
    corpus = _make_corpus(tmp_path)
    _write_doc(corpus, "sources/a.md")
    report = check(corpus)
    assert report.ok is False
    assert any("never finished" in r for r in report.reasons)


def test_a_stale_index_finish_fails(tmp_path: Path):
    """Content fresh but the last index finish is past the threshold: content
    is arriving yet unindexed."""
    corpus = _make_corpus(tmp_path)
    _write_doc(corpus, "sources/a.md")
    old = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - (FRESHNESS_DEFAULT_MAX_AGE_DAYS + 2) * 86400))
    _write_generation(corpus, old)
    report = check(corpus)
    assert report.ok is False
    assert any("index last finished" in r for r in report.reasons)


def test_cli_staleness_exits_nonzero_on_a_frozen_corpus(tmp_path: Path, capsys):
    """The loud part: `alexandria staleness` exits nonzero and names the
    stale signal on stderr, so the weekly loop and an operator both see it."""
    corpus = _make_corpus(tmp_path)
    doc = _write_doc(corpus, "sources/a.md")
    _age(doc, FRESHNESS_DEFAULT_MAX_AGE_DAYS + 3)
    _write_generation(corpus, _now_iso())

    rc = cli.app(["--corpus", str(corpus), "staleness"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "STALE" in err
    assert "newest document" in err

    capsys.readouterr()
    rc = cli.app(["--corpus", str(corpus), "staleness", "--max-age-days", "1"])
    assert rc == 1


def test_cli_staleness_exits_zero_on_a_fresh_corpus(tmp_path: Path, capsys):
    corpus = _make_corpus(tmp_path)
    _write_doc(corpus, "sources/a.md")
    _write_generation(corpus, _now_iso())
    assert cli.app(["--corpus", str(corpus), "staleness"]) == 0
    assert "fresh" in capsys.readouterr().out


def test_newest_source_mtime_ignores_non_indexable_paths(tmp_path: Path):
    """The same is_indexable_source predicate as the indexer: quarantined and
    AppleDouble files never count as fresh content."""
    corpus = _make_corpus(tmp_path)
    _write_doc(corpus, "sources/_unparsed/quarantine.md", "old")
    _age(corpus / "sources" / "_unparsed" / "quarantine.md", 90)
    real = _write_doc(corpus, "sources/good.md")
    assert newest_source_mtime(corpus) == pytest.approx(real.stat().st_mtime)
