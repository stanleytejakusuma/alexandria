"""The console script is declared in pyproject; a broken entry point ships a broken
`pip install`. These tests keep it honest."""

import pytest
from alexandria.cli import app, build_parser


def test_parser_exposes_the_documented_verbs():
    verbs = build_parser()._subparsers._group_actions[0].choices
    assert {"migrate", "sync", "lint", "index", "search"} <= set(verbs)


def test_index_and_search_use_offline_provider_and_show_trace(tmp_path, monkeypatch, capsys):
    corpus = tmp_path / "corpus"
    note = corpus / "sources" / "pi" / "note.md"
    note.parent.mkdir(parents=True)
    note.write_text(
        "---\ntype: observation\ntitle: Retry\nproject: core\nsource: pi\ntags: [retrieval]\n"
        "entities: [sweep]\ngenerated:\n  at: '2026-08-01T00:00:00Z'\n---\n"
        "# Retry\n\nThe sweep retries a page that fails lint.\n"
    )
    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")

    assert app(["--corpus", str(corpus), "index", "--limit", "1"]) == 0
    assert app(["--corpus", str(corpus), "search", "sweep page lint", "--trace"]) == 0
    assert "metadata_filter" in capsys.readouterr().out


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


def test_sync_writes_notes_and_is_fail_safe(tmp_path, monkeypatch):
    """Concurrency must not break the fail-safe: a burst whose distillation fails
    stays unconsumed, while its siblings still land."""
    import json
    from alexandria import cli

    sess = tmp_path / "sessions" / "--home-user-proj--"
    sess.mkdir(parents=True)
    for n in range(3):
        events = [{"type": "session", "id": f"s{n}"}] + [
            {"type": "message", "timestamp": "2026-07-29T10:0%d:00Z" % i,
             "message": {"role": "user",
                         "content": [{"type": "text", "text": "A substantive question. " * 8}]}}
            for i in range(2)]
        (sess / f"2026-07-29T10-00-0{n}-000Z_s{n}.jsonl").write_text(
            "\n".join(json.dumps(e) for e in events) + "\n")

    good = json.dumps({"observations": [{"title": "A finding", "narrative": "n",
                                         "facts": ["f"], "entities": ["e"], "tags": []}]})
    from alexandria.llm import ScriptedClient

    class RoundRobin(ScriptedClient):
        def complete(self, system, user, temperature=0.0):
            self.calls.append((system, user))
            return good if len(self.calls) % 2 else "not json"

    monkeypatch.setattr(cli, "LLMClient", lambda **kw: RoundRobin())
    rc = cli.app(["--corpus", str(tmp_path / "corpus"), "sync", "pi-sessions",
                  "--sessions-dir", str(tmp_path / "sessions"), "--workers", "2"])
    assert rc == 0
    written = list((tmp_path / "corpus" / "sources").rglob("*.md"))
    assert written, "successful distillations must still land"
    state = json.loads((tmp_path / "corpus" / ".alexandria" / "state"
                        / "pi-sessions.json").read_text())
    assert len(state["bursts"]) == len(written)   # only successes consumed
