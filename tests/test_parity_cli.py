from __future__ import annotations

from alexandria.cli import build_parser


def test_parity_command_accepts_explicit_remote_corpus() -> None:
    args = build_parser().parse_args([
        "--corpus", "/local/corpus",
        "parity",
        "--remote-host", "nas",
        "--remote-corpus", "/remote/corpus",
    ])

    assert args.command == "parity"
    assert args.remote_host == "nas"
    assert args.remote_corpus == "/remote/corpus"
    assert args.func.__name__ == "cmd_parity"


def test_parity_command_accepts_a_non_executable_reconcile_plan() -> None:
    args = build_parser().parse_args([
        "--corpus", "/local/corpus",
        "parity",
        "--remote-host", "nas",
        "--remote-corpus", "/remote/corpus",
        "--reconcile-plan",
    ])

    assert args.reconcile_plan is True
