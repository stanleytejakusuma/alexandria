"""Caller-visible progress for the long-running answer command."""
from __future__ import annotations

from alexandria import cli


def test_cmd_answer_reports_progress_before_running_the_pipeline(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_build_search_engine", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        cli,
        "run_answer",
        lambda *args, **kwargs: cli.AnswerOutcome(True, "Verified answer.", 1, "id"),
    )

    assert cli.cmd_answer(cli.build_parser().parse_args(
        ["--corpus", str(tmp_path), "answer", "why is this slow?"]
    )) == 0

    captured = capsys.readouterr()
    assert "initializing retrieval" in captured.err.lower()
    assert "synthesis started" in captured.err.lower()
    assert "900s" in captured.err
    assert captured.out == "Verified answer.\n"
