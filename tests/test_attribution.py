"""BACKLOG #8: the CLI audit trail records a derived identity, never a claimed one.

The removed `--user` flag defaulted to `ALEXANDRIA_USER` or "local" and was
written verbatim into `audit/search.jsonl`. A forged identity in an audit trail
is worse than an absent one: absence is visibly absent, while a forgery reads as
evidence. These tests fail if the flag -- or any equivalent passthrough -- comes
back.
"""

import getpass
import json

import pytest

from alexandria.cli import build_parser, cli_identity


def _subparser_dests(name: str) -> set[str]:
    """Every argument dest on one subcommand of the real shipped parser."""
    for action in build_parser()._actions:
        choices = getattr(action, "choices", None)
        if choices and name in choices:
            return {a.dest for a in choices[name]._actions}
    raise AssertionError(f"no {name!r} subcommand in the shipped parser")


@pytest.mark.parametrize("command", ["search", "answer", "sync"])
def test_no_subcommand_accepts_a_caller_supplied_identity(command):
    """The forgery surface is gone from every command that had it."""
    assert "user" not in _subparser_dests(command)


@pytest.mark.parametrize("command", ["search", "answer", "sync"])
def test_passing_the_removed_flag_is_now_a_hard_error(command):
    """A script still passing --user fails loudly instead of being believed.

    argparse exits 2 on an unrecognized argument. The alternative -- silently
    ignoring it -- would leave the caller believing an identity was recorded.
    """
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args([command, "q", "--user", "someone-else"])
    assert exc.value.code == 2


def test_identity_is_derived_from_the_os_not_from_the_environment(monkeypatch):
    """Setting the old env var cannot influence the recorded identity."""
    monkeypatch.setenv("ALEXANDRIA_USER", "impersonated")
    assert cli_identity() == getpass.getuser()
    assert cli_identity() != "impersonated"


def test_identity_never_raises_when_the_os_has_no_passwd_entry(monkeypatch):
    """Some containers have no passwd entry; a query must not die over telemetry."""
    monkeypatch.setattr(getpass, "getuser", lambda: (_ for _ in ()).throw(OSError("no pwd")))
    assert cli_identity() == "unknown"


def test_caller_survives_but_is_labelled_unverified():
    """`--caller` names the invoking tool, not a person, so it stays -- but its
    help text must not describe it as an identity, which is what made the old
    field misleading in the first place."""
    for action in build_parser()._actions:
        choices = getattr(action, "choices", None)
        if choices and "search" in choices:
            caller = next(a for a in choices["search"]._actions if a.dest == "caller")
            assert "UNVERIFIED" in caller.help
            assert "not an identity" in caller.help
            return
    raise AssertionError("no search subcommand")


def test_a_real_search_records_the_derived_identity(tmp_path, monkeypatch):
    """End to end through the actual CLI: what lands in the audit trail is the
    OS user, not anything the caller could have chosen."""
    from alexandria import cli

    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    monkeypatch.setenv("ALEXANDRIA_USER", "impersonated")
    corpus = tmp_path / "corpus"
    (corpus / "sources").mkdir(parents=True)
    (corpus / "sources" / "n.md").write_text(
        "---\ntitle: Note\nsource: test\n---\n\n# Note\n\nA fact about otters.\n"
    )
    assert cli.app(["--corpus", str(corpus), "index"]) == 0
    assert cli.app(["--corpus", str(corpus), "search", "otters", "--k", "1"]) == 0

    rows = [json.loads(line) for line
            in (corpus / ".alexandria" / "audit" / "search.jsonl").read_text().splitlines()]
    assert rows, "search wrote no audit row"
    assert rows[-1]["user"] == getpass.getuser()
    assert rows[-1]["user"] != "impersonated"
