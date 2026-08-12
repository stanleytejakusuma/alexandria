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


def test_searching_a_corpus_that_was_never_indexed_fails_loudly(tmp_path):
    """Measured on a real host 2026-08-11: `search` against a corpus directory
    that did not exist returned EXIT=0 and printed nothing, because building the
    engine created the index directory on the way past. Zero hits from a missing
    corpus is indistinguishable from zero hits from a corpus that genuinely lacks
    the answer, so a mis-provisioned consumer reports confident false negatives.
    """
    with pytest.raises(SystemExit) as exc:
        app(["--corpus", str(tmp_path), "search", "anything"])
    assert "never indexed" in str(exc.value)
    assert not (tmp_path / ".alexandria" / "index" / "chunks.lance").exists(), \
        "the guard must refuse BEFORE the store materialises an empty index"


def test_searching_a_real_index_that_predates_manifests_fails_loudly_with_backfill_hint(tmp_path, monkeypatch):
    """Gate F4: a real `index` run before this session's own manifest column
    existed is exactly the state of the shipped 2.2GB index -- must refuse,
    not silently treat 'absent' as 'compatible'."""
    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    corpus = tmp_path / "corpus"
    note = corpus / "sources" / "pi" / "note.md"
    note.parent.mkdir(parents=True)
    note.write_text(
        "---\ntype: observation\ntitle: Retry\nproject: core\nsource: pi\ntags: []\n"
        "entities: []\ngenerated:\n  at: '2026-08-01T00:00:00Z'\n---\n# Retry\n\ntext\n"
    )
    assert app(["--corpus", str(corpus), "index", "--limit", "1"]) == 0
    (corpus / ".alexandria" / "index" / "manifest.json").unlink()

    with pytest.raises(SystemExit) as exc:
        app(["--corpus", str(corpus), "search", "anything"])
    assert "no manifest" in str(exc.value)
    assert "--backfill-manifest" in str(exc.value)


def test_backfill_manifest_writes_a_manifest_without_reindexing(tmp_path, monkeypatch, capsys):
    import json

    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    corpus = tmp_path / "corpus"
    (corpus / ".alexandria" / "index" / "chunks.lance").mkdir(parents=True)

    assert app(["--corpus", str(corpus), "index", "--backfill-manifest"]) == 0
    out = capsys.readouterr().out
    assert "manifest backfilled" in out
    manifest = json.loads((corpus / ".alexandria" / "index" / "manifest.json").read_text())
    assert manifest["provider"] == "hash"
    assert manifest["normalized"] is True


def test_switching_embed_provider_between_index_runs_fails_loudly_instead_of_mixing_vectors(tmp_path, monkeypatch):
    """The exact bug the spec names in §3.3: flipping ALEXANDRIA_EMBED_PROVIDER
    between an indexed corpus and a later query must not silently proceed --
    that would mix incomparable vector spaces in one column."""
    import json

    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    corpus = tmp_path / "corpus"
    note = corpus / "sources" / "pi" / "note.md"
    note.parent.mkdir(parents=True)
    note.write_text(
        "---\ntype: observation\ntitle: Retry\nproject: core\nsource: pi\ntags: []\n"
        "entities: []\ngenerated:\n  at: '2026-08-01T00:00:00Z'\n---\n# Retry\n\ntext\n"
    )
    assert app(["--corpus", str(corpus), "index", "--limit", "1"]) == 0
    manifest_path = corpus / ".alexandria" / "index" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["provider"] = "mlx"  # simulate a run that used a different provider
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(SystemExit) as exc:
        app(["--corpus", str(corpus), "search", "anything"])
    assert "provider" in str(exc.value)
    assert "index --rebuild" in str(exc.value)


def _stub_index(corpus):
    """Satisfy the "corpus was actually indexed" precondition, including a
    matching manifest (gate F4 -- see index/manifest.py).

    These tests stub `run_pipeline`, so the engine is never used for retrieval --
    but building it still asserts an index AND a manifest exist. Before the
    index assertion existed, an unindexed corpus silently produced an EMPTY
    index and zero hits; the manifest assertion closes the same class of gap
    for embedding-model identity. Callers must also set
    ALEXANDRIA_EMBED_PROVIDER=hash (matching the manifest written here) so
    _build_search_engine's own embedder construction doesn't try to load a
    real local/MLX model just to verify.
    """
    from alexandria import liveness
    from alexandria.index.embedder import CachedEmbedder, HashEmbedder
    from alexandria.index.manifest import write_manifest
    (corpus / ".alexandria" / "index" / "chunks.lance").mkdir(parents=True, exist_ok=True)
    embedder = CachedEmbedder(HashEmbedder(), corpus / ".alexandria" / "cache" / "embeddings.sqlite")
    write_manifest(corpus, embedder, "hash")
    liveness.record_success(corpus, promoted_count=0, generation=0)


def test_answer_prints_the_emitted_page(tmp_path, monkeypatch, capsys):
    from pathlib import Path
    import alexandria.synthesis.pipeline as synth_pipeline

    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    _stub_index(tmp_path)
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
    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    _stub_index(tmp_path)
    import alexandria.synthesis.pipeline as synth_pipeline

    claim = type("C", (), {"id": "c1", "text": "a claim that could not be cited"})()
    page = type("P", (), {"claims": [claim]})()
    verdict = type("V", (), {"failed_claim_ids": {"c1"}})()
    repair_obj = type("R", (), {"page": page, "verdict": verdict})()

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


def test_wiki_site_verb_renders(tmp_path, capsys):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "p.md").write_text("# P\n\nbody\n", encoding="utf-8")
    out = tmp_path / "site"
    assert app(["--corpus", str(tmp_path), "wiki-site", "--wiki", str(wiki), "--out", str(out)]) == 0
    assert (out / "index.html").exists()
    assert "1 page(s)" in (out / "index.html").read_text(encoding="utf-8")


def test_wiki_site_missing_wiki_dir_fails(tmp_path):
    assert app(["--corpus", str(tmp_path), "wiki-site", "--wiki", str(tmp_path / "nope"),
                "--out", str(tmp_path / "site")]) == 2


def test_eval_refuses_to_measure_an_index_left_partial_by_a_rebuild(tmp_path, capsys):
    """SPEC C6: a measurement must assert its preconditions.

    A killed rebuild once left the table at 90,304 of 124,751 chunks; the eval
    happily measured it, reported a -4.1% "regression" that was pure artifact,
    and appended it to eval_runs.jsonl where it became the baseline for every
    later gate. Refusing is the only safe behaviour: a partial index is not a
    state the corpus was ever in.
    """
    from alexandria.cli import build_parser, cmd_eval, _rebuild_marker

    marker = _rebuild_marker(tmp_path)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("rebuild of 124751 chunks started\n")

    args = build_parser().parse_args(["--corpus", str(tmp_path), "eval"])
    assert cmd_eval(args) == 2
    assert "refusing to measure a partial index" in capsys.readouterr().err


def test_allow_partial_index_overrides_the_refusal(tmp_path, capsys):
    """The guard is a safety catch, not a wall -- deliberate measurement of a
    partial index stays possible, it just cannot happen by accident."""
    import contextlib
    from alexandria.cli import build_parser, cmd_eval, _rebuild_marker

    marker = _rebuild_marker(tmp_path)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("rebuild in progress\n")

    args = build_parser().parse_args(
        ["--corpus", str(tmp_path), "eval", "--allow-partial-index"])
    # It will fail further down for want of a golden set in an empty corpus;
    # what this asserts is that it got PAST the guard rather than stopping on it.
    with contextlib.suppress(Exception):
        cmd_eval(args)
    assert "refusing to measure a partial index" not in capsys.readouterr().err
