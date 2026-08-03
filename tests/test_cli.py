"""The console script is declared in pyproject; a broken entry point ships a broken
`pip install`. These tests keep it honest."""

import pytest
from alexandria.cli import app, build_parser


def test_parser_exposes_the_documented_verbs():
    verbs = build_parser()._subparsers._group_actions[0].choices
    assert {"migrate", "sync", "lint"} <= set(verbs)


def test_lint_passes_on_a_clean_corpus(tmp_path, capsys):
    d = tmp_path / "sources" / "pi-sessions"
    d.mkdir(parents=True)
    (d / "n.md").write_text(
        "---\ntype: observation\ntitle: T\ngenerated:\n  by: connector/pi-sessions\n"
        "  at: '2026-07-31T00:00:00Z'\nsource: pi-sessions\nsource_id: '1'\n---\nbody\n")
    assert app(["--corpus", str(tmp_path), "lint"]) == 0
    assert "0 error(s)" in capsys.readouterr().out


def test_lint_fails_on_a_schema_violation(tmp_path, capsys):
    d = tmp_path / "sources" / "pi-sessions"
    d.mkdir(parents=True)
    (d / "bad.md").write_text("---\ntype: observation\ntitle: T\n---\nbody\n")
    assert app(["--corpus", str(tmp_path), "lint"]) == 1
    assert "missing_required" in capsys.readouterr().out


def test_unknown_connector_is_rejected(tmp_path):
    assert app(["--corpus", str(tmp_path), "sync", "nope"]) == 2
