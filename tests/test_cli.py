"""The console script is declared in pyproject; a broken entry point ships a broken
`pip install`. These tests keep it honest."""

import pytest
from alexandria.cli import app, build_parser


def test_parser_exposes_the_documented_verbs():
    verbs = build_parser()._subparsers._group_actions[0].choices
    assert {"migrate", "sync", "lint", "index", "search", "eval"} <= set(verbs)


def test_eval_release_gate_fails_when_a_previous_hit_becomes_a_miss(tmp_path, monkeypatch, capsys):
    import json
    from types import SimpleNamespace

    from alexandria import cli

    corpus = tmp_path / "corpus"
    golden = corpus / ".alexandria" / "golden" / "golden-v1.jsonl"
    target = corpus / "sources" / "wanted.md"
    golden.parent.mkdir(parents=True)
    target.parent.mkdir(parents=True)
    target.write_text("target", encoding="utf-8")
    golden.write_text(json.dumps({
        "id": "release-gate", "query": "find it", "must_retrieve": ["sources/wanted"], "k": 5,
    }) + "\n", encoding="utf-8")

    class FakeResult:
        def __init__(self, doc_id):
            self.doc_id = doc_id

    class FakeEngine:
        embedder = SimpleNamespace(name="hash-24")
        reranker = SimpleNamespace(model_name="fake", half_precision=True)
        config = SimpleNamespace(prefetch=20, top_k=5, rrf_k=60, wiki_boost=1.25)
        store = SimpleNamespace(count=lambda: 1)

        def __init__(self, ids):
            self.ids = ids

        def search(self, query, *, k=None):
            return [FakeResult(doc_id) for doc_id in self.ids]

    engines = iter([FakeEngine(["sources/wanted"]), FakeEngine([])])
    monkeypatch.setattr(cli, "_build_search_engine", lambda config, path: next(engines), raising=False)

    assert app(["--corpus", str(corpus), "eval"]) == 0
    assert app(["--corpus", str(corpus), "eval", "--fail-on-regression"]) == 1
    assert "HIT->MISS: release-gate" in capsys.readouterr().out


def test_eval_reports_missing_targets_as_unusable_json_without_running_retrieval(tmp_path, monkeypatch, capsys):
    import json

    from alexandria import cli

    corpus = tmp_path / "corpus"
    golden = corpus / ".alexandria" / "golden" / "golden-v1.jsonl"
    golden.parent.mkdir(parents=True)
    golden.write_text(json.dumps({
        "id": "missing", "query": "q", "must_retrieve": ["sources/deleted"], "k": 5,
    }) + "\n", encoding="utf-8")
    monkeypatch.setattr(cli, "_build_search_engine", lambda config, path: pytest.fail("must not run"),
                        raising=False)

    assert app(["--corpus", str(corpus), "eval", "--json"]) == 2
    assert json.loads(capsys.readouterr().out) == {"target_errors": ["missing"]}


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


def test_parser_exposes_answer_verb():
    verbs = build_parser()._subparsers._group_actions[0].choices
    assert "answer" in verbs


def test_answer_prints_the_emitted_page(tmp_path, monkeypatch, capsys):
    from pathlib import Path
    import alexandria.synthesis.pipeline as synth_pipeline

    page = tmp_path / "emitted" / "what-happened.md"
    page.parent.mkdir()
    page.write_text("---\ntitle: what happened\n---\n\nThe signer crashed. [^1]\n", encoding="utf-8")

    class FakeRepair:
        page = type("P", (), {"claims": []})()
        failed_claim_ids = ()

    class FakeResult:
        emitted = True
        page_path = page
        skip_log_path = None
        repair = FakeRepair()

    monkeypatch.setattr(synth_pipeline, "run_pipeline", lambda *a, **k: FakeResult())
    assert app(["--corpus", str(tmp_path), "answer", "what happened"]) == 0
    out = capsys.readouterr().out
    assert "The signer crashed." in out
    assert "title: what happened" in out


def test_answer_failure_exits_one_with_failed_claims(tmp_path, monkeypatch, capsys):
    import alexandria.synthesis.pipeline as synth_pipeline

    claim = type("C", (), {"id": "c1", "text": "a claim that could not be cited"})()
    page = type("P", (), {"claims": [claim]})()
    repair_obj = type("R", (), {"page": page, "failed_claim_ids": {"c1"}})()

    class FakeResult:
        emitted = False
        page_path = None
        skip_log_path = None
        repair = repair_obj

    monkeypatch.setattr(synth_pipeline, "run_pipeline", lambda *a, **k: FakeResult())
    assert app(["--corpus", str(tmp_path), "answer", "some question"]) == 1
    err = capsys.readouterr().err
    assert "failed its native checks" in err
    assert "c1" in err
