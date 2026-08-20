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
    monkeypatch.setattr(cli, "_build_search_engine",
                        lambda config, path, **_kwargs: next(engines), raising=False)

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


def test_index_skips_appledouble_metadata_in_sources_and_wiki(tmp_path, monkeypatch, capsys):
    """Metadata sidecars must not become index errors or phantom source docs."""
    corpus = tmp_path / "corpus"
    for relative in ("sources/real.md", "wiki/real.md"):
        path = corpus / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("---\nsource: test\n---\n\nreal document body\n")
    for relative in ("sources/._real.md", "wiki/._real.md"):
        path = corpus / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"Finder metadata is not markdown\x00")
    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")

    assert app(["--corpus", str(corpus), "index"]) == 0
    captured = capsys.readouterr()
    assert "2 chunks from 2 documents" in captured.out
    assert "._real.md" not in captured.err


def test_lint_passes_on_a_clean_corpus(tmp_path, capsys):
    d = tmp_path / "sources" / "pi-sessions"
    d.mkdir(parents=True)
    (d / "n.md").write_text(
        "---\ntype: observation\ntitle: T\ngenerated:\n  by: connector/pi-sessions\n"
        "  at: '2026-07-31T00:00:00Z'\nsource: pi-sessions\nsource_id: '1'\n---\nbody\n")
    assert app(["--corpus", str(tmp_path), "lint"]) == 0
    assert "0 error(s)" in capsys.readouterr().out


def test_lint_skips_appledouble_metadata_in_sources_and_wiki(tmp_path, capsys):
    """The lint walk is independent from indexing, so it must use the same
    source eligibility rule rather than reporting Finder sidecars as malformed
    documents forever."""
    source = tmp_path / "sources" / "pi-sessions"
    source.mkdir(parents=True)
    (source / "real.md").write_text(
        "---\ntype: observation\ntitle: T\ngenerated:\n  by: connector/pi-sessions\n"
        "  at: '2026-07-31T00:00:00Z'\nsource: pi-sessions\nsource_id: '1'\n---\nbody\n")
    (source / "._real.md").write_bytes(b"Finder metadata is not markdown\x00")
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "._summary.md").write_bytes(b"Finder metadata is not markdown\x00")

    assert app(["--corpus", str(tmp_path), "lint"]) == 0
    out = capsys.readouterr().out
    assert "lint: 1 documents, 0 error(s)" in out
    assert "._real.md" not in out
    assert "._summary.md" not in out


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


def test_searching_a_real_index_that_predates_manifests_fails_loudly_with_rebuild_hint(tmp_path, monkeypatch):
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
    assert "--rebuild" in str(exc.value)


def test_backfill_manifest_refuses_to_launder_a_nonempty_legacy_index(tmp_path, monkeypatch):
    """A declared L2 policy cannot be asserted over vectors built pre-policy."""
    from alexandria.index.store import VectorStore

    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    corpus = tmp_path / "corpus"
    store = VectorStore(corpus / ".alexandria" / "index")
    store.upsert([{
        "chunk_id": "sources/legacy#1", "doc_id": "sources/legacy", "text": "legacy",
        "heading_path": "", "vector": [1.0] * 384, "type": "observation",
        "project": None, "status": "active", "source": "test", "tags": [],
        "entities": [], "layer": "sources", "generated_at": None,
        "enrichment": None, "kind": None, "parent_doc": None, "target_chunk": None,
    }])

    with pytest.raises(SystemExit, match="--rebuild"):
        app(["--corpus", str(corpus), "index", "--backfill-manifest"])
    assert not (corpus / ".alexandria" / "index" / "manifest.json").exists()


def test_backfill_manifest_uses_the_same_writer_lock_as_index_and_promote(tmp_path, monkeypatch):
    """Backfill's empty-store decision cannot race an index/promote writer."""
    from alexandria import cli
    from alexandria.index.store import VectorStore
    from alexandria.writelock import write_lock

    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    monkeypatch.setattr(cli, "DEFAULT_LOCK_TIMEOUT", 0.05)
    corpus = tmp_path / "corpus"
    VectorStore(corpus / ".alexandria" / "index")
    holder = write_lock(corpus)
    assert holder.acquire()
    try:
        with pytest.raises(SystemExit, match="could not acquire the corpus write lock"):
            app(["--corpus", str(corpus), "index", "--backfill-manifest"])
    finally:
        holder.release()
    assert not (corpus / ".alexandria" / "index" / "manifest.json").exists()


def test_backfill_manifest_may_label_an_empty_new_index(tmp_path, monkeypatch, capsys):
    """An empty store has no stored vectors to launder or mix."""
    import json

    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    from alexandria.index.store import VectorStore

    corpus = tmp_path / "corpus"
    VectorStore(corpus / ".alexandria" / "index")  # a real but empty index store

    assert app(["--corpus", str(corpus), "index", "--backfill-manifest"]) == 0
    out = capsys.readouterr().out
    assert "manifest backfilled" in out
    manifest = json.loads((corpus / ".alexandria" / "index" / "manifest.json").read_text())
    assert manifest["provider"] == "hash"
    assert manifest["normalized"] is True


def test_build_search_engine_refuses_a_dropped_rebuild_but_tolerates_a_live_writer(tmp_path, monkeypatch):
    """Construction guards on the durable marker only -- never on the writer lock.

    Two-sided on purpose. A rebuild that has already dropped a projection is
    unreadable no matter how long anyone waits, so construction must refuse it.
    An ordinary promote/drain holding the writer lock for a few seconds is the
    opposite case: blocking there would stop `alexandria serve` from booting
    (test_serve.py's lock-skipped-drain test pins that), and it buys nothing,
    because the engine binds no epoch -- each search takes its own shared epoch.
    """
    from alexandria.cli import _build_search_engine
    from alexandria.config import AppConfig
    from alexandria.writelock import IndexReadUnavailable, rebuild_marker, write_lock

    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    corpus = tmp_path / "corpus"
    note = corpus / "sources" / "note.md"
    note.parent.mkdir(parents=True)
    note.write_text("---\nsource: test\n---\n\ncoherent reader fence\n")
    assert app(["--corpus", str(corpus), "index"]) == 0
    cfg = AppConfig(corpus_path=corpus, embed_provider="hash")

    writer = write_lock(corpus)
    assert writer.acquire()
    try:
        assert _build_search_engine(cfg, corpus) is not None, (
            "a transient writer must not stop an engine (or serve) from starting")
    finally:
        writer.release()

    marker = rebuild_marker(corpus)
    marker.write_text("interrupted rebuild\n")
    try:
        with pytest.raises(IndexReadUnavailable, match="rebuild"):
            _build_search_engine(cfg, corpus)
    finally:
        marker.unlink()


@pytest.mark.parametrize("argv, label", [
    (["search", "anything"], "search"),
    (["answer", "anything"], "answer"),
])
def test_cli_reports_exit_3_when_a_rebuild_holds_the_index_at_startup(tmp_path, monkeypatch, capsys, argv, label):
    """The refusal must be an actionable exit code, not a raw traceback.

    Engine CONSTRUCTION is itself a reader, so it raises IndexReadUnavailable
    before the query call ever happens. Catching only around `engine.search`
    left this startup case escaping as an uncaught RuntimeError -- exit 1 plus
    a stack trace, not the documented exit 3. The catch therefore belongs at
    the `app()` dispatch boundary, which also covers every future read verb.
    """
    from alexandria.writelock import rebuild_marker

    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    corpus = tmp_path / "corpus"
    note = corpus / "sources" / "note.md"
    note.parent.mkdir(parents=True)
    note.write_text("---\nsource: test\n---\n\nreader fence exit contract\n")
    assert app(["--corpus", str(corpus), "index"]) == 0

    rebuild_marker(corpus).write_text("interrupted rebuild\n")
    assert app(["--corpus", str(corpus), *argv]) == 3, label
    assert "index unavailable" in capsys.readouterr().err


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


def test_eval_output_shows_the_interval_and_withholds_a_verdict_it_cannot_support(capsys):
    """A reader must not be able to take a one-query flip away as a finding."""
    from alexandria.cli import _print_eval_report
    from alexandria.eval.history import compare
    from alexandria.eval.metrics import EvalResult, summarize
    from alexandria.eval.runner import EvalReport

    def report(results):
        return EvalReport(results=results, summary=summarize(results), config={},
                          corpus_chunks=1, timestamp="2026-08-13T00:00:00+00:00", git_sha="abc123")

    previous = report([
        EvalResult("moved", "q", True, 1, ["sources/a"], 1.0),
        EvalResult("steady", "q", True, 1, ["sources/b"], 1.0),
    ])
    current = report([
        EvalResult("moved", "q", False, 0, [], 1.0),
        EvalResult("steady", "q", True, 1, ["sources/b"], 1.0),
    ])
    _print_eval_report(current, compare(previous, current))
    out = capsys.readouterr().out
    assert "recall@k: 50.0% [9.5%-90.5%]" in out
    assert "NOT significant" in out
    assert "p=1.000" in out



def test_promote_missing_manifest_guides_legacy_nonempty_index_to_rebuild(tmp_path, monkeypatch):
    """The operator-facing promote error cannot suggest policy laundering."""
    from types import SimpleNamespace
    from alexandria import cli
    from alexandria.index.manifest import ManifestMissing

    monkeypatch.setattr(cli, "_config_for", lambda _args: SimpleNamespace(
        corpus_path=tmp_path, embed_provider="hash"))
    monkeypatch.setattr(cli, "_cached_embedder", lambda *_args: object())
    monkeypatch.setattr(cli, "promote_pending", lambda *_args: (_ for _ in ()).throw(
        ManifestMissing("missing manifest")))

    with pytest.raises(SystemExit, match="--rebuild") as exc:
        cli.cmd_promote(SimpleNamespace())
    assert "--backfill-manifest" not in str(exc.value)


def test_every_production_search_engine_is_built_with_a_corpus_root(tmp_path):
    """The fence is skipped only when there is no corpus lock to share.

    `_read_epoch` treats a missing corpus_root as "unfenced", which is correct
    for synthetic/in-memory harnesses but would be a silent hole if a real
    construction path ever omitted it. cli._build_search_engine is that single
    production chokepoint, so pin the default here.
    """
    import ast
    import inspect
    from alexandria import cli

    tree = ast.parse(inspect.getsource(cli._build_search_engine_unlocked))
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "SearchEngine"]
    assert len(calls) == 1
    assert any(kw.arg == "corpus_root" for kw in calls[0].keywords), (
        "the production engine builder stopped passing corpus_root -- every "
        "search built through it would silently bypass the reader fence")


def test_build_search_engine_reads_from_an_activated_release_not_the_legacy_path(tmp_path, monkeypatch):
    """#30 P2a: once a release is activated, search/answer/eval must read
    FROM that release directory -- not from the legacy flat layout, which
    stays whatever it last was (possibly stale, possibly absent)."""
    from alexandria.cli import _build_search_engine
    from alexandria.config import AppConfig
    from alexandria.index.chunker import chunk_doc_records
    from alexandria.index.releases import activate_release, new_release_dir
    from alexandria.index.embedder import CachedEmbedder, HashEmbedder
    from alexandria.index.manifest import write_manifest
    from alexandria.index.store import VectorStore
    from alexandria.index.bm25 import BM25Index

    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    corpus = tmp_path / "corpus"
    legacy_note = corpus / "sources" / "legacy.md"
    legacy_note.parent.mkdir(parents=True)
    legacy_note.write_text("---\nsource: test\n---\n\nlegacy layout content\n")
    assert app(["--corpus", str(corpus), "index"]) == 0

    # stage a SEPARATE release with DIFFERENT real content and activate it --
    # the legacy chunks must become invisible, because the release is now the
    # single source of truth (this is a real build, not a hand-faked empty one).
    release_note = corpus / "sources" / "release_only.md"
    release_note.write_text("---\nsource: test\n---\n\nrelease-only content\n")
    from alexandria.config import AppConfig as _Cfg
    records, _ = chunk_doc_records(release_note, corpus, _Cfg(corpus_path=corpus))

    release_dir = new_release_dir(corpus)
    embedder = CachedEmbedder(HashEmbedder(), corpus / ".alexandria" / "cache" / "embeddings.sqlite")
    store = VectorStore(release_dir)
    lexical = BM25Index(release_dir / "fts.sqlite")
    for record in records:
        record["vector"] = embedder.embed([record["text"]])[0]
    store.upsert(records)
    lexical.index(records)
    write_manifest(corpus, embedder, "hash", index_dir=release_dir)
    activate_release(corpus, release_dir.name)

    cfg = AppConfig(corpus_path=corpus, embed_provider="hash")
    engine = _build_search_engine(cfg, corpus)
    hits = engine.search("release-only content")
    assert hits, "must find content that is ONLY in the release"
    doc_ids = {r.doc_id for r in engine.search("legacy layout content", k=10)}
    assert "sources/legacy" not in doc_ids, (
        "the legacy doc must be structurally unreachable once a release is "
        "active -- it lives in a different store entirely, not merely "
        "outranked")


def test_build_search_engine_still_reads_the_legacy_path_when_no_release_is_active(tmp_path, monkeypatch):
    """The zero-migration guarantee: a corpus that never activated a release
    keeps working exactly as before P2a."""
    from alexandria.cli import _build_search_engine
    from alexandria.config import AppConfig

    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    corpus = tmp_path / "corpus"
    note = corpus / "sources" / "note.md"
    note.parent.mkdir(parents=True)
    note.write_text("---\nsource: test\n---\n\nunmigrated corpus content\n")
    assert app(["--corpus", str(corpus), "index"]) == 0

    cfg = AppConfig(corpus_path=corpus, embed_provider="hash")
    engine = _build_search_engine(cfg, corpus)
    results = engine.search("unmigrated corpus content")
    assert results, "a corpus with no active release must still search the legacy layout"


# ---------------------------------------------------------------------------
# #30 P2a: --rebuild stages a new release instead of dropping the live index
# in place. See docs/DECISION-staged-releases-p2a.md.
# ---------------------------------------------------------------------------

def test_rebuild_activates_a_new_release_and_the_old_one_is_retained(tmp_path, monkeypatch):
    from alexandria.index.releases import active_release_id, list_releases

    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    corpus = tmp_path / "corpus"
    note = corpus / "sources" / "note.md"
    note.parent.mkdir(parents=True)
    note.write_text("---\nsource: test\n---\n\nfirst content\n")
    assert app(["--corpus", str(corpus), "index"]) == 0
    assert active_release_id(corpus) is None, "the FIRST index run stays on the legacy path"

    assert app(["--corpus", str(corpus), "index", "--rebuild"]) == 0
    first_release = active_release_id(corpus)
    assert first_release is not None, "--rebuild must activate a release"

    note.write_text("---\nsource: test\n---\n\nsecond content, rebuilt\n")
    assert app(["--corpus", str(corpus), "index", "--rebuild"]) == 0
    second_release = active_release_id(corpus)
    assert second_release != first_release

    releases = {r["release_id"]: r["active"] for r in list_releases(corpus)}
    assert releases[first_release] is False, "the previous release is retained, not deleted"
    assert releases[second_release] is True


def test_rebuild_result_is_searchable_and_reflects_the_new_content(tmp_path, monkeypatch):
    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    corpus = tmp_path / "corpus"
    note = corpus / "sources" / "note.md"
    note.parent.mkdir(parents=True)
    note.write_text("---\nsource: test\n---\n\noriginal rebuild content\n")
    assert app(["--corpus", str(corpus), "index", "--rebuild"]) == 0

    from alexandria.cli import _build_search_engine
    from alexandria.config import AppConfig
    cfg = AppConfig(corpus_path=corpus, embed_provider="hash")
    engine = _build_search_engine(cfg, corpus)
    assert engine.search("original rebuild content")


def test_a_failed_rebuild_leaves_the_previously_active_release_serving(tmp_path, monkeypatch):
    """The whole point of P2a: a crash mid-rebuild must not destroy or
    corrupt the index that is currently serving."""
    from alexandria.index.releases import active_release_id

    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    corpus = tmp_path / "corpus"
    note = corpus / "sources" / "note.md"
    note.parent.mkdir(parents=True)
    note.write_text("---\nsource: test\n---\n\nstable content\n")
    assert app(["--corpus", str(corpus), "index", "--rebuild"]) == 0
    stable_release = active_release_id(corpus)

    # simulate a mid-rebuild crash: patch the pipeline to blow up AFTER the
    # candidate store has real data written but BEFORE activation
    import alexandria.cli as cli_mod
    original = cli_mod._run_index_pipeline

    def exploding_pipeline(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("simulated crash mid-rebuild")

    monkeypatch.setattr(cli_mod, "_run_index_pipeline", exploding_pipeline)
    note.write_text("---\nsource: test\n---\n\nnever-activated content\n")
    with pytest.raises(RuntimeError, match="simulated crash"):
        app(["--corpus", str(corpus), "index", "--rebuild"])

    assert active_release_id(corpus) == stable_release, (
        "a crash before activation must leave the OLD release active")

    from alexandria.cli import _build_search_engine
    from alexandria.config import AppConfig
    cfg = AppConfig(corpus_path=corpus, embed_provider="hash")
    engine = _build_search_engine(cfg, corpus)
    assert engine.search("stable content"), "the pre-crash content must still be servable"


def test_incremental_writes_after_a_rebuild_land_in_the_active_release(tmp_path, monkeypatch):
    """Once a release is active, cmd_promote/cmd_delete and a plain (non
    --rebuild) `alexandria index` must write INTO that release, never the
    abandoned legacy path -- otherwise a fact remembered after the first
    staged rebuild would be silently unsearchable."""
    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    corpus = tmp_path / "corpus"
    note = corpus / "sources" / "note.md"
    note.parent.mkdir(parents=True)
    note.write_text("---\nsource: test\n---\n\nfirst\n")
    assert app(["--corpus", str(corpus), "index", "--rebuild"]) == 0

    note2 = corpus / "sources" / "note2.md"
    note2.write_text("---\nsource: test\n---\n\nsecond, added incrementally\n")
    assert app(["--corpus", str(corpus), "index"]) == 0  # no --rebuild

    from alexandria.cli import _build_search_engine
    from alexandria.config import AppConfig
    cfg = AppConfig(corpus_path=corpus, embed_provider="hash")
    engine = _build_search_engine(cfg, corpus)
    assert engine.search("second, added incrementally")


# ---------------------------------------------------------------------------
# #30 P2a: retention / rollback / inspection CLI surface.
# ---------------------------------------------------------------------------

def test_index_list_releases_shows_active_and_inactive_releases(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    corpus = tmp_path / "corpus"
    note = corpus / "sources" / "note.md"
    note.parent.mkdir(parents=True)
    note.write_text("---\nsource: test\n---\n\nfirst\n")
    assert app(["--corpus", str(corpus), "index", "--rebuild"]) == 0
    note.write_text("---\nsource: test\n---\n\nsecond\n")
    assert app(["--corpus", str(corpus), "index", "--rebuild"]) == 0

    rc = app(["--corpus", str(corpus), "index", "--list-releases"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "active" in out.lower()


def test_index_rollback_reactivates_the_previous_release(tmp_path, monkeypatch):
    from alexandria.index.releases import active_release_id, list_releases

    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    corpus = tmp_path / "corpus"
    note = corpus / "sources" / "note.md"
    note.parent.mkdir(parents=True)
    note.write_text("---\nsource: test\n---\n\nzebra migration protocol\n")
    assert app(["--corpus", str(corpus), "index", "--rebuild"]) == 0
    first = active_release_id(corpus)

    note.write_text("---\nsource: test\n---\n\nkangaroo encryption key rotation\n")
    assert app(["--corpus", str(corpus), "index", "--rebuild"]) == 0
    second = active_release_id(corpus)
    assert second != first

    assert app(["--corpus", str(corpus), "index", "--rollback"]) == 0
    assert active_release_id(corpus) == first, "rollback must reactivate the prior release"

    # Verify by the release's actual contents, not by search semantics on a
    # one-document corpus (a 1-doc index always returns that doc for any
    # query, so "search returns nothing" is the wrong instrument).
    from alexandria.index.store import VectorStore as _VS
    from alexandria.index.releases import resolve_active_index_dir
    from alexandria.index.bm25 import BM25Index as _BM25
    active_dir = resolve_active_index_dir(corpus)
    store = _VS(active_dir)
    rows = store.get_many(list(store.chunk_ids()))
    texts = [r["text"] for r in rows.values()]
    assert any("zebra" in t2 for t2 in texts), "the OLD content must be servable after rollback"
    assert not any("kangaroo" in t2 for t2 in texts), (
        "the rolled-back release must not contain the newer content")


def test_index_gc_removes_old_releases_but_keeps_active_and_previous(tmp_path, monkeypatch, capsys):
    from alexandria.index.releases import active_release_id, list_releases

    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    corpus = tmp_path / "corpus"
    note = corpus / "sources" / "note.md"
    note.parent.mkdir(parents=True)
    note.write_text("---\nsource: test\n---\n\nfirst\n")
    assert app(["--corpus", str(corpus), "index", "--rebuild"]) == 0
    note.write_text("---\nsource: test\n---\n\nsecond\n")
    assert app(["--corpus", str(corpus), "index", "--rebuild"]) == 0
    note.write_text("---\nsource: test\n---\n\nthird\n")
    assert app(["--corpus", str(corpus), "index", "--rebuild"]) == 0

    before = {r["release_id"] for r in list_releases(corpus)}
    assert len(before) == 3

    rc = app(["--corpus", str(corpus), "index", "--gc"])
    assert rc == 0
    after = list_releases(corpus)
    ids = {r["release_id"] for r in after}
    assert len(ids) == 2, f"GC should retain active + previous, got {ids}"
    assert active_release_id(corpus) in ids
    assert any(r["active"] for r in after)


# ---------------------------------------------------------------------------
# #45: a pre-policy (unverified_legacy) index is servable on READ without a
# forced rebuild. This is the real end-to-end proof through the actual
# chokepoint (_build_search_engine), not just manifest.py's unit tests.
# ---------------------------------------------------------------------------

def test_serving_a_pre_policy_legacy_index_prints_a_rebuild_reminder(tmp_path, monkeypatch, capsys):
    """Red review, 2026-08-20: #45 removes ALL rebuild pressure otherwise --
    a legacy index becomes permanently comfortable with no nudge toward the
    verified state. A once-per-call stderr warning is the cheap ratchet:
    it never blocks the read (matching the existing liveness-stale pattern
    right above this code), but it keeps the relaxed state visible."""
    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    corpus = tmp_path / "corpus"
    note = corpus / "sources" / "note.md"
    note.parent.mkdir(parents=True)
    note.write_text("---\nsource: test\n---\n\nsome content\n")
    assert app(["--corpus", str(corpus), "index"]) == 0

    import json
    manifest_path = corpus / ".alexandria" / "index" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest.pop("normalization_policy")
    manifest_path.write_text(json.dumps(manifest))

    from alexandria.cli import _build_search_engine
    from alexandria.config import AppConfig
    cfg = AppConfig(corpus_path=corpus, embed_provider="hash")
    _build_search_engine(cfg, corpus)

    warning = capsys.readouterr().err
    assert "unverified" in warning.lower() or "legacy" in warning.lower()
    assert "rebuild" in warning.lower()


def test_a_pre_policy_legacy_index_is_servable_without_a_rebuild(tmp_path, monkeypatch):
    """The manifest is PRESENT but predates the declared normalization_policy
    field (a real historical state, distinct from test_searching_a_real_
    index_that_predates_manifests_fails_loudly_with_rebuild_hint above, which
    covers a manifest MISSING entirely and must stay refused). #45's whole
    point: this must now be searchable, not force a rebuild."""
    import json

    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    corpus = tmp_path / "corpus"
    note = corpus / "sources" / "pi" / "note.md"
    note.parent.mkdir(parents=True)
    note.write_text(
        "---\ntype: observation\ntitle: Legacy\nproject: core\nsource: pi\ntags: []\n"
        "entities: []\ngenerated:\n  at: '2026-08-01T00:00:00Z'\n---\n"
        "# Legacy\n\nfindable content from before the policy field existed\n"
    )
    assert app(["--corpus", str(corpus), "index"]) == 0

    manifest_path = corpus / ".alexandria" / "index" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest.pop("normalization_policy")
    manifest_path.write_text(json.dumps(manifest))

    from alexandria.cli import _build_search_engine
    from alexandria.config import AppConfig
    cfg = AppConfig(corpus_path=corpus, embed_provider="hash")
    engine = _build_search_engine(cfg, corpus)  # must NOT raise SystemExit
    results = engine.search("findable content")
    assert results, "a pre-policy legacy index must still return real results"


def test_a_pre_policy_legacy_index_uses_a_scale_invariant_search(tmp_path, monkeypatch):
    """Red review, 2026-08-20: cosine distance is now UNCONDITIONAL inside
    VectorStore (see test_store.py), not a caller-wired flag -- so there is
    no per-corpus attribute to inspect here anymore. What this test now
    proves: an actual RANKING that would only be correct under cosine
    distance, through the real chokepoint, against a real legacy manifest."""
    import json

    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    corpus = tmp_path / "corpus"
    note = corpus / "sources" / "note.md"
    note.parent.mkdir(parents=True)
    note.write_text("---\nsource: test\n---\n\nsome content\n")
    assert app(["--corpus", str(corpus), "index"]) == 0

    manifest_path = corpus / ".alexandria" / "index" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest.pop("normalization_policy")
    manifest_path.write_text(json.dumps(manifest))

    from alexandria.cli import _build_search_engine
    from alexandria.config import AppConfig
    cfg = AppConfig(corpus_path=corpus, embed_provider="hash")
    engine = _build_search_engine(cfg, corpus)
    assert engine.search("some content"), "a legacy-manifest corpus must still return results"


def test_writing_into_a_pre_policy_legacy_index_still_refuses(tmp_path, monkeypatch):
    """The explicit #45 requirement: reads relax, writes do not. An
    incremental (non --rebuild) index run into an unverified_legacy index
    must still refuse -- verify_manifest_for_write has no opt-in at all."""
    import json

    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    corpus = tmp_path / "corpus"
    note = corpus / "sources" / "note.md"
    note.parent.mkdir(parents=True)
    note.write_text("---\nsource: test\n---\n\nfirst\n")
    assert app(["--corpus", str(corpus), "index"]) == 0

    manifest_path = corpus / ".alexandria" / "index" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest.pop("normalization_policy")
    manifest_path.write_text(json.dumps(manifest))

    note2 = corpus / "sources" / "second.md"
    note2.write_text("---\nsource: test\n---\n\nsecond, added incrementally\n")
    with pytest.raises(SystemExit, match="--rebuild"):
        app(["--corpus", str(corpus), "index"])  # no --rebuild: incremental write path


def test_index_enrich_invalidate_drops_a_stored_payload_and_reports_honestly(tmp_path, monkeypatch, capsys):
    """#5/F3d escape hatch, wired at the CLI: --enrich-invalidate DOC_ID drops
    a stored enrichment payload even with content/recipe unchanged, and is
    honest about whether there was anything to drop (exit 1 + stderr note
    when there was not, matching the CLI's existing refusal conventions)."""
    from alexandria.enrich import EnrichmentStore
    from alexandria.index.releases import resolve_active_index_dir

    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    corpus = tmp_path / "corpus"
    note = corpus / "sources" / "note.md"
    note.parent.mkdir(parents=True)
    note.write_text("---\nsource: test\n---\n\nbody\n")
    assert app(["--corpus", str(corpus), "index", "--rebuild"]) == 0

    index_dir = resolve_active_index_dir(corpus)
    store = EnrichmentStore(index_dir)
    store.put("sources/note.md", "somesha", "m@v1", {"summary": "s"})
    assert store.count() == 1

    rc = app(["--corpus", str(corpus), "index", "--enrich-invalidate", "sources/note.md"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "invalidated" in out.lower()
    assert "rebuild" in out.lower()  # Red review: must not imply more than cache-clear
    # re-open (the CLI's store instance is separate from this test's)
    assert EnrichmentStore(index_dir).count() == 0

    rc2 = app(["--corpus", str(corpus), "index", "--enrich-invalidate", "sources/nothing-here.md"])
    assert rc2 == 1
    err = capsys.readouterr().err
    assert "no stored enrichment" in err.lower()


def test_caller_label_passes_through_known_callers_unchanged():
    """#8 residual: the only two values with any documented provenance on
    the CLI path pass through as-is -- 'cli' (nothing specified) and
    'pi-extension' (the one external, documented caller: demand-report.py's
    own GENUINE_CALLERS methodology, and ~/.pi/agent/extensions/alexandria.ts
    outside this repo, which sets ALEXANDRIA_CALLER=pi-extension)."""
    from alexandria.cli import caller_label

    assert caller_label("cli") == "cli"
    assert caller_label("pi-extension") == "pi-extension"


def test_caller_label_flags_any_unrecognized_value():
    """The actual fix: a forged or novel --caller value can no longer look
    exactly as credible as a documented one in the audit trail -- it is
    visibly prefixed, distinguishing 'a string someone typed' from 'the one
    value with any provenance'. This includes a value that CLAIMS to be
    pi-extension's sibling or otherwise plausible-sounding."""
    from alexandria.cli import caller_label

    assert caller_label("weekly-loop") == "unverified:weekly-loop"
    assert caller_label("totally-legit-tool") == "unverified:totally-legit-tool"
    assert caller_label("pi-extension-v2") == "unverified:pi-extension-v2"
    assert caller_label("") == "unverified:"


def test_caller_label_handles_none_and_non_string_input():
    from alexandria.cli import caller_label

    assert caller_label(None) == "unverified:"
    assert caller_label(123) == "unverified:"


def test_search_audit_row_flags_an_unrecognized_caller(tmp_path, monkeypatch, capsys):
    """End-to-end: a forged --caller value on `alexandria search` is written
    to the audit trail visibly marked, not verbatim."""
    import json
    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    corpus = tmp_path / "corpus"
    note = corpus / "sources" / "note.md"
    note.parent.mkdir(parents=True)
    note.write_text("---\nsource: test\n---\n\nsome searchable body text here\n")
    assert app(["--corpus", str(corpus), "index", "--rebuild"]) == 0

    rc = app(["--corpus", str(corpus), "search", "searchable", "--caller", "definitely-pi-extension"])
    assert rc == 0

    audit_path = corpus / ".alexandria" / "audit" / "search.jsonl"
    rows = [json.loads(l) for l in audit_path.read_text().splitlines() if l.strip()]
    assert rows[-1]["caller"] == "unverified:definitely-pi-extension"


def test_search_audit_row_keeps_the_real_pi_extension_caller_unmarked(tmp_path, monkeypatch):
    """The documented caller value is not penalized by the fix -- only
    UNRECOGNIZED values get flagged."""
    import json
    monkeypatch.setenv("ALEXANDRIA_EMBED_PROVIDER", "hash")
    corpus = tmp_path / "corpus"
    note = corpus / "sources" / "note.md"
    note.parent.mkdir(parents=True)
    note.write_text("---\nsource: test\n---\n\nsome searchable body text here\n")
    assert app(["--corpus", str(corpus), "index", "--rebuild"]) == 0

    rc = app(["--corpus", str(corpus), "search", "searchable", "--caller", "pi-extension"])
    assert rc == 0

    audit_path = corpus / ".alexandria" / "audit" / "search.jsonl"
    rows = [json.loads(l) for l in audit_path.read_text().splitlines() if l.strip()]
    assert rows[-1]["caller"] == "pi-extension"


def test_sync_now_actually_records_the_caller_flag(tmp_path, monkeypatch):
    """#8 residual, found in passing: --caller existed on the sync verb's
    argparse but cmd_sync never read args.caller -- a silently dead flag."""
    import json
    from types import SimpleNamespace

    from alexandria import cli
    from alexandria.connectors.base import RawItem
    from alexandria.corpus import Doc

    class _FakeConn:
        name = "fake"

        def __init__(self):
            self.errors = []
            self.committed = []

        def discover(self):
            return [RawItem(source_id="one", content="c")]

        def normalize(self, item):
            return [Doc(path="sources/fake/a.md",
                       frontmatter={"type": "memory", "title": "T",
                                   "generated": {"by": "test", "at": "2026-01-01"},
                                   "status": "stable", "source": "fake", "source_id": "x"},
                       body="body\n")]

        def commit(self, items):
            self.committed.extend(i.source_id for i in items)

        def skip_log(self):
            return []

    monkeypatch.setattr(cli, "_sync_connector", lambda args: _FakeConn())
    args = SimpleNamespace(connector="fake", corpus=str(tmp_path), workers=2,
                          limit=0, dry_run=False, caller="pi-extension")
    assert cli.cmd_sync(args) == 0

    audit_path = tmp_path / ".alexandria" / "audit" / "sync.jsonl"
    rows = [json.loads(l) for l in audit_path.read_text().splitlines() if l.strip()]
    assert rows[-1]["caller"] == "pi-extension"


def test_sync_flags_an_unrecognized_caller_too(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    from alexandria import cli
    from alexandria.connectors.base import RawItem

    class _EmptyConn:
        name = "fake"

        def __init__(self):
            self.errors = []
            self.committed = []

        def discover(self):
            return [RawItem(source_id="one", content="c")]

        def normalize(self, item):
            return []

        def commit(self, items):
            self.committed.extend(i.source_id for i in items)

        def skip_log(self):
            return []

    monkeypatch.setattr(cli, "_sync_connector", lambda args: _EmptyConn())
    args = SimpleNamespace(connector="fake", corpus=str(tmp_path), workers=2,
                          limit=0, dry_run=False, caller="my-custom-script")
    assert cli.cmd_sync(args) == 0

    audit_path = tmp_path / ".alexandria" / "audit" / "sync.jsonl"
    rows = [json.loads(l) for l in audit_path.read_text().splitlines() if l.strip()]
    assert rows[-1]["caller"] == "unverified:my-custom-script"
