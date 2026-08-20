"""P0/P1/P2 answer/eval safety regressions; all retrieval/LLM work stays offline."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from alexandria.audit import grade_note
from alexandria.cache import QueryCache, ResponseCache, answer_pipeline_fingerprint
from alexandria.cli import app
from alexandria.eval.history import Delta
from alexandria.llm import LLMClient, ScriptedClient
from alexandria.synthesis.gather import gather
from alexandria.synthesis.judge import judge_page
from alexandria.synthesis.write import Citation, Claim, SynthesisPage


@dataclass(frozen=True)
class _Result:
    doc_id: str
    chunk_id: str = "sources/wanted#1"
    text: str = "evidence"
    score: float = 1.0


class _EvalEngine:
    embedder = SimpleNamespace(name="hash-24")
    reranker = SimpleNamespace(model_name="fake", half_precision=True)
    config = SimpleNamespace(prefetch=20, top_k=5, rrf_k=60, wiki_boost=1.25)
    store = SimpleNamespace(count=lambda: 1)

    def __init__(self, result_ids):
        self.result_ids = result_ids
        self.search_calls = 0
        self.last_cache_hit = 0

    def search(self, query, *, k=None, **_ignored):
        self.search_calls += 1
        return [_Result(doc_id) for doc_id in self.result_ids]


def _golden_corpus(tmp_path: Path) -> Path:
    corpus = tmp_path / "corpus"
    golden = corpus / ".alexandria" / "golden" / "golden-v1.jsonl"
    target = corpus / "sources" / "wanted.md"
    golden.parent.mkdir(parents=True)
    target.parent.mkdir(parents=True)
    target.write_text("target", encoding="utf-8")
    golden.write_text(json.dumps({
        "id": "retrieval", "query": "find wanted", "must_retrieve": ["sources/wanted"], "k": 5,
    }) + "\n", encoding="utf-8")
    return corpus


def test_eval_bypasses_prepopulated_query_cache_after_retrieval_regresses(tmp_path, monkeypatch):
    """The eval must observe the current engine, not replay yesterday's hit."""
    from alexandria import cli

    corpus = _golden_corpus(tmp_path)
    stale = QueryCache(corpus)
    stale_key = stale.key("find wanted", 5, "eval")
    stale.put(stale_key, [{"doc_id": "sources/wanted"}])
    seen, engines = [], []

    class CacheAwareRegressedEngine(_EvalEngine):
        def __init__(self, query_cache):
            super().__init__([])
            self.query_cache = query_cache

        def search(self, query, *, k=None, **_ignored):
            self.search_calls += 1
            # Current retrieval has regressed to no result. The sole possible
            # hit is exactly the pre-populated QueryCache entry.
            cached = (self.query_cache.get(stale_key)
                      if self.query_cache is not None else None)
            return [_Result(item["doc_id"]) for item in cached] if cached else []

    def build(config, path, *, query_cache=True, client="search"):
        seen.append((query_cache, client))
        engine = CacheAwareRegressedEngine(stale if query_cache else None)
        engines.append(engine)
        return engine

    monkeypatch.setattr(cli, "_build_search_engine", build)
    # A current miss is recorded even without a previous baseline; it must not
    # be turned into a stale cached hit. Mutating cmd_eval back to the default
    # cache-enabled build makes this test read the pre-populated wanted hit.
    assert app(["--corpus", str(corpus), "eval"]) == 0
    assert engines[0].search_calls == 1
    assert seen == [(False, "search")]
    report = json.loads((corpus / ".alexandria" / "eval_runs.jsonl").read_text())
    assert report["results"][0]["hit"] is False


def test_ordinary_search_keeps_query_cache_enabled(tmp_path, monkeypatch):
    from alexandria import cli

    seen = []
    monkeypatch.setattr(cli, "_build_search_engine", lambda *a, **kw: seen.append(kw) or _EvalEngine([]))
    assert cli.cmd_search(SimpleNamespace(**{
        "corpus": str(tmp_path), "query": "q", "k": None, "type": None,
        "project": None, "layer": None, "trace": False, "caller": "test",
    })) == 0
    assert seen == [{"client": "search"}]


def test_response_cache_answer_pipeline_fingerprint_separates_output_knobs(tmp_path):
    cache = ResponseCache(tmp_path)
    common = dict(question="what changed?", model="writer", k=8, prompt_version="v1", generation=3)
    settings = dict(grader_a_model="grade-a", grader_b_model="grade-b",
                    base_url="http://grader", api_key_env="TEST_KEY", retrieval={"search": "v1"},
                    max_follow_up_queries=2, audit_concurrency=4, prompt_version="v1")
    baseline = cache.key(**common, pipeline=answer_pipeline_fingerprint(**settings))
    equal_config = cache.key(**common, pipeline=answer_pipeline_fingerprint(**settings))
    cache.put(baseline, {"text": "cached equal-config answer"})
    assert equal_config == baseline
    assert cache.get(equal_config) == {"text": "cached equal-config answer"}
    for changed in (
        {"max_follow_up_queries": 0},
        {"grader_a_model": "other-a"},
        {"grader_b_model": "other-b"},
        {"audit_concurrency": 1},
        {"retrieval": {"search": "v2"}},
        {"base_url": "http://other-grader"},
        {"prompt_version": "v2"},
    ):
        pipeline = answer_pipeline_fingerprint(**(settings | changed))
        changed_key = cache.key(**common, pipeline=pipeline)
        assert changed_key != baseline
        assert cache.get(changed_key) is None


def test_answer_parser_rejects_negative_and_over_bound_controls():
    from alexandria.cli import build_parser
    from alexandria.synthesis.gather import MAX_FOLLOW_UP_QUERIES
    from alexandria.synthesis.judge import MAX_AUDIT_CONCURRENCY

    parser = build_parser()
    for flag, invalid in (
        ("--max-follow-up-queries", "-1"),
        ("--max-follow-up-queries", str(MAX_FOLLOW_UP_QUERIES + 1)),
        ("--audit-concurrency", "-1"),
        ("--audit-concurrency", str(MAX_AUDIT_CONCURRENCY + 1)),
    ):
        with pytest.raises(SystemExit) as rejected:
            parser.parse_args(["answer", "q", flag, invalid])
        assert rejected.value.code == 2


class _GatherEngine:
    def __init__(self):
        self.calls = []

    def search(self, query, *, k=None):
        self.calls.append(query)
        return [_Result("sources/seed", text="evidence")]


def test_gather_rejects_out_of_bound_followups_and_zero_runs_none():
    from alexandria.synthesis.gather import MAX_FOLLOW_UP_QUERIES

    for invalid in (-1, MAX_FOLLOW_UP_QUERIES + 1):
        with pytest.raises(ValueError, match="max_follow_up_queries"):
            gather(_GatherEngine(), "topic", llm=ScriptedClient([]), max_follow_up_queries=invalid)
    engine = _GatherEngine()
    result = gather(engine, "topic", llm=ScriptedClient([json.dumps({"queries": ["followup"]})]),
                    max_follow_up_queries=0)
    assert engine.calls == ["topic"]
    assert result.follow_up_queries == ()


def _page_for_judge():
    from alexandria.synthesis.gather import GatherResult, SourceChunk
    chunk = SourceChunk("sources/a#1", "sources/a", "evidence")
    return (
        GatherResult("topic", (chunk,), (chunk,), (), (), "{}"),
        SynthesisPage("topic", "page", (Claim("c", "claim", (Citation("sources/a", "sources/a#1"),)),),
                      "test", ()),
    )


def test_judge_rejects_invalid_audit_concurrency():
    from alexandria.synthesis.judge import MAX_AUDIT_CONCURRENCY

    gathered, page = _page_for_judge()
    for invalid in (-1, MAX_AUDIT_CONCURRENCY + 1):
        with pytest.raises(ValueError, match="audit_concurrency"):
            judge_page(gathered, page, audit_llm=ScriptedClient([]),
                       coverage_llm_a=ScriptedClient([]), coverage_llm_b=ScriptedClient([]),
                       audit_concurrency=invalid)


def test_scripted_client_top_level_supported_cannot_hide_unsupported_or_fabricated_clause():
    for clause_verdict in ("unsupported", "fabricated"):
        llm = ScriptedClient([json.dumps({
            "verdict": "supported", "reason": "contradictory scripted response",
            "clauses": [{"clause": "bad clause", "verdict": clause_verdict, "reason": "bad"}],
        })])
        with pytest.raises(Exception, match="contradict"):
            grade_note(llm, "evidence", "title", "claim", "note", clauses=True)


def test_top_level_supported_without_clauses_remains_accepted():
    verdict = grade_note(ScriptedClient([json.dumps({"verdict": "supported", "reason": "scripted"})]),
                         "evidence", "title", "claim", "note", clauses=True)
    assert verdict.verdict == "supported"
    assert verdict.clauses == ()


def test_parallel_llm_calls_reserve_start_slots_without_serializing_network(monkeypatch):
    starts = []
    entered = threading.Event()
    release = threading.Event()
    start_lock = threading.Lock()

    def offline_once(self, system, user, temperature=0.0):
        with start_lock:
            starts.append(time.monotonic())
            if len(starts) == 2:
                entered.set()
        release.wait(timeout=1)
        return "ok"

    monkeypatch.setattr(LLMClient, "_once", offline_once)
    client = LLMClient(min_interval=0.04, max_retries=0)
    threads = [threading.Thread(target=client.complete, args=("s", str(i))) for i in range(2)]
    for thread in threads:
        thread.start()
    assert entered.wait(timeout=1), "network calls were serialized instead of overlapping"
    release.set()
    for thread in threads:
        thread.join(timeout=1)
        assert not thread.is_alive()
    assert len(starts) == 2
    assert starts[1] - starts[0] >= 0.035


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "leg-ablation.py"
_spec = importlib.util.spec_from_file_location("leg_ablation_safety", SCRIPT_PATH)
leg_ablation = importlib.util.module_from_spec(_spec)
sys.modules["leg_ablation_safety"] = leg_ablation
_spec.loader.exec_module(leg_ablation)


def _delta(recall, mrr, p):
    return Delta(recall_at_k=recall, mrr=mrr, hit_to_miss=[], miss_to_hit=["x"] * 6, p_value=p)


def test_leg_ablation_mixed_sign_only_fails_for_significant_positive_recall(monkeypatch):
    monkeypatch.setattr(leg_ablation, "compare", lambda _before, _after: _delta(-0.1, 0.2, 0.01))
    failures, observations = leg_ablation.dead_weight_verdict(None, {"dense": None})
    assert failures == []
    assert "MRR" in observations["dense"]["note"]


def test_weekly_loop_notifies_a_red_leg_ablation_without_changing_its_verify_exit(tmp_path):
    """Offline shell integration: red ablation alerts, maintenance remains nonfatal."""
    home = tmp_path / "home"
    repo = home / "codebase" / "alexandria"
    fake_bin = tmp_path / "bin"
    corpus = tmp_path / "corpus"
    notifier_log = tmp_path / "notifier.log"
    (repo / ".venv" / "bin").mkdir(parents=True)
    (repo / "scripts").mkdir()
    fake_bin.mkdir()
    (corpus / "sources").mkdir(parents=True)
    (corpus / "wiki").mkdir()
    (corpus / ".alexandria" / "index").mkdir(parents=True)
    (corpus / ".alexandria" / "index" / "generation.json").write_text('{"generation": 1}')

    def executable(name: str, body: str) -> Path:
        target = fake_bin / name
        target.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        target.chmod(0o755)
        return target

    # No external process is called: keychain returns a fake token; every engine
    # invocation is a no-op except the named ablation, which exits red; verifier
    # returns green so the final exit demonstrates the deliberate contract.
    executable("security", "printf fake-token\n")
    executable("git", "exit 0")
    executable("python3", "printf '%s\n' 1")
    notifier = executable("notifier", f"printf '%s\n' \"$*\" >> {notifier_log}")
    (repo / ".venv" / "bin" / "python").write_text(
        "#!/bin/sh\ncase \"$*\" in *leg-ablation.py*) exit 1;; esac\nexit 0\n", encoding="utf-8")
    (repo / ".venv" / "bin" / "python").chmod(0o755)
    script = SCRIPT_PATH.parent / "run-weekly-loop.sh"
    env = os.environ | {
        "HOME": str(home), "ALEXANDRIA_CORPUS": str(corpus),
        "ALEXANDRIA_BASE_URL": "offline", "ALEXANDRIA_KEYCHAIN_SERVICE": "offline",
        "ALEXANDRIA_NOTIFIER": str(notifier), "PATH": f"{fake_bin}:{os.environ['PATH']}",
    }
    completed = subprocess.run(["bash", str(script)], env=env, text=True, capture_output=True)
    assert completed.returncode == 0, completed.stderr
    digest = (corpus / ".alexandria" / "loop" / "weekly-digest.md").read_text()
    assert "[FAIL] leg-ablation exited 1" in digest
    alert = notifier_log.read_text()
    assert "weekly leg-ablation FAILED" in alert
    assert "alexandria-weekly-leg-ablation" in alert



def test_run_answer_threads_prompt_version_into_its_pipeline_cache_fingerprint():
    """A future refactor must not leave prompt version only in the legacy key."""
    import ast
    import inspect
    from alexandria import cli

    tree = ast.parse(inspect.getsource(cli.run_answer))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and getattr(node.func, "id", None) == "answer_pipeline_fingerprint"]
    assert len(calls) == 1
    supplied = {keyword.arg: keyword.value.id for keyword in calls[0].keywords
                if isinstance(keyword.value, ast.Name)}
    assert supplied["prompt_version"] == "prompt_version"


def test_answer_cache_key_is_computed_once_and_reused_for_get_and_put():
    """Red round 2, condition 3: a re-read at PUT would poison the cache.

    If the key (which embeds the corpus generation) were recomputed after
    synthesis, this interleaving poisons it: retrieval runs against generation
    G, an external rebuild completes during a slow LLM synthesis, and the PUT
    then files G-epoch evidence under the G+1 key -- replayed to every later
    caller for the TTL. The defence is structural: read the generation once,
    build the key once, reuse that exact name at both get() and put().
    """
    import ast
    import inspect
    from alexandria import cli

    tree = ast.parse(inspect.getsource(cli.run_answer))
    assigns = [n for n in ast.walk(tree)
               if isinstance(n, ast.Assign)
               and any(getattr(t, "id", None) == "rkey" for t in n.targets)]
    assert len(assigns) == 1, "the response-cache key must be built exactly once"

    # Match BOTH bare-name and attribute forms: counting only `read_index_generation(...)`
    # would let a second sample sneak in as `cache.read_index_generation(...)`.
    def _is_gen_read(node):
        if not isinstance(node, ast.Call):
            return False
        fn = node.func
        return (getattr(fn, "id", None) == "read_index_generation"
                or getattr(fn, "attr", None) == "read_index_generation")

    gen_reads = [n for n in ast.walk(tree) if _is_gen_read(n)]
    assert len(gen_reads) == 1, "the generation must be sampled exactly once"

    # Count is not enough -- pin ORDER too. The whole point is that the sample
    # precedes retrieval, so a rebuild finishing mid-synthesis cannot file
    # G-epoch evidence under a G+1 key.
    pipeline_calls = [n for n in ast.walk(tree)
                      if isinstance(n, ast.Call)
                      and getattr(n.func, "id", None) == "run_pipeline"]
    assert len(pipeline_calls) == 1
    assert gen_reads[0].lineno < pipeline_calls[0].lineno, (
        "the generation is sampled AFTER retrieval -- reopens the PUT-poisoning window")

    calls = {n.func.attr: n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and getattr(n.func.value, "id", None) == "response_cache"}
    for name in ("get", "put"):
        assert name in calls, f"response_cache.{name} disappeared"
        assert any(getattr(a, "id", None) == "rkey" for a in calls[name].args), (
            f"response_cache.{name} no longer uses the single precomputed key")


def test_run_answer_gives_every_stage_ONE_shared_request_deadline():
    """#47: the per-call cap only composes if all three clients share a budget.

    If a refactor built any client without the deadline -- or built a fresh
    deadline per client -- a dead gateway would again cost N budgets instead of
    one, silently restoring the ~1.8h worst case that motivated this. Pinned
    structurally: exactly one RequestDeadline is constructed, and every
    LLMClient in run_answer receives that same name.
    """
    import ast
    import inspect
    from alexandria import cli

    tree = ast.parse(inspect.getsource(cli.run_answer))

    deadlines = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call)
                 and getattr(n.func, "id", None) == "RequestDeadline"]
    assert len(deadlines) == 1, "the answer must build exactly one shared deadline"

    clients = [n for n in ast.walk(tree)
               if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "LLMClient"]
    assert len(clients) == 3, "writer + two graders"
    for call in clients:
        passed = [kw for kw in call.keywords if kw.arg == "deadline"]
        assert passed, "an LLM client was built without the shared request deadline"
        assert getattr(passed[0].value, "id", None) == "deadline", (
            "a client got its own deadline instead of the shared one")


def test_the_default_answer_budget_is_finite_and_generous_enough_for_a_healthy_answer():
    """An infinite default is the bug; a too-tight one breaks slow-but-alive."""
    from alexandria.cli import DEFAULT_ANSWER_TIMEOUT

    assert DEFAULT_ANSWER_TIMEOUT is not None
    # A healthy cold answer measured ~200s, and the bridge saw >300s under
    # concurrent writes. Anything at/below that would fail real answers.
    assert DEFAULT_ANSWER_TIMEOUT > 300
    assert DEFAULT_ANSWER_TIMEOUT <= 1800


def test_all_three_answer_clients_share_the_SAME_deadline_object(monkeypatch, tmp_path):
    """Identity, not spelling -- Red: the AST test pins the wrong thing.

    The AST guard asserts three LLMClient calls each pass a name spelled
    `deadline`. It breaks on a benign refactor (extract construction into a
    helper) and passes on an adversarial one (rebinding `deadline` between
    constructions). The invariant that actually matters is OBJECT IDENTITY: if
    each client got its own budget, N chained stages would again cost N budgets
    and the ~1.8h worst case would silently return.
    """
    from alexandria import cli

    built = []

    class SpyClient:
        def __init__(self, **kwargs):
            self.deadline = kwargs.get("deadline")
            built.append(self)

    from alexandria import llm as llm_mod
    from alexandria.synthesis import pipeline as pipeline_mod

    # Both names are imported INSIDE run_answer, so patch them at their source
    # modules -- patching cli.LLMClient would silently miss the local import.
    monkeypatch.setattr(llm_mod, "LLMClient", SpyClient)
    # run_pipeline is imported inside run_answer, so patch it at its source.
    monkeypatch.setattr(pipeline_mod, "run_pipeline",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stop after wiring")))

    class FakeEngine:
        embedder = type("E", (), {"name": "hash-24"})()
        reranker = type("R", (), {"model_name": "fake", "half_precision": True})()
        config = type("C", (), {"prefetch": 8, "top_k": 5, "rrf_k": 60, "wiki_boost": 1.25})()
        logger = type("L", (), {"log_usage": lambda self, **kw: None})()

    with pytest.raises(RuntimeError, match="stop after wiring"):
        cli.run_answer(
            cli.AppConfig(corpus_path=tmp_path), tmp_path, "q",
            engine=FakeEngine(), k=5, llm_model="m",
            grader_a_model="a", grader_b_model="b",
            base_url=None, api_key_env=None, prompt_version="v1",
        )

    assert len(built) == 3, "writer + two graders"
    deadlines = [c.deadline for c in built]
    assert all(d is not None for d in deadlines), "a client was built with no budget"
    assert deadlines[0] is deadlines[1] is deadlines[2], (
        "clients got separate budgets -- N stages would cost N budgets again")


def test_a_budget_exhausted_answer_never_reaches_the_response_cache(monkeypatch, tmp_path):
    """Red round 2 #3: the no-poisoning invariant was traced, never pinned.

    If a budget-exhausted run ever wrote to the response cache, a truncated or
    unjudged answer would replay verbatim for the whole TTL after the gateway
    recovered. It holds today only because BudgetExhausted propagates out of
    run_answer before `put` -- and the scope fix edits exactly that path, so it
    needs a regression test rather than an argument.
    """
    from alexandria import cli
    from alexandria.llm import BudgetExhausted
    from alexandria.synthesis import pipeline as pipeline_mod

    puts = []
    monkeypatch.setattr(cli.ResponseCache, "put",
                        lambda self, key, value: puts.append(key), raising=False)
    monkeypatch.setattr(pipeline_mod, "run_pipeline",
                        lambda *a, **k: (_ for _ in ()).throw(
                            BudgetExhausted("request budget exhausted (900s)", scope="request")))

    class FakeEngine:
        embedder = type("E", (), {"name": "hash-24"})()
        reranker = type("R", (), {"model_name": "fake", "half_precision": True})()
        config = type("C", (), {"prefetch": 8, "top_k": 5, "rrf_k": 60, "wiki_boost": 1.25})()
        logger = type("L", (), {"log_usage": lambda self, **kw: None})()

    with pytest.raises(BudgetExhausted):
        cli.run_answer(
            cli.AppConfig(corpus_path=tmp_path), tmp_path, "q",
            engine=FakeEngine(), k=5, llm_model="m",
            grader_a_model="a", grader_b_model="b",
            base_url=None, api_key_env=None, prompt_version="v1",
        )

    assert puts == [], "a budget-exhausted answer was written to the response cache"


def test_a_SALVAGED_answer_is_returned_but_never_emitted_or_cached(monkeypatch, tmp_path):
    """#48 / Red: a salvaged draft must not replay as a full answer.

    The whole tenancy exemption for answer_timeout now rests on this line: a
    low-budget unverified draft, cached under a key that omits the budget,
    would replay to a default-budget request as if it were a verified answer.
    So the rule is structural: salvage returns text, but never reaches put(),
    and emitted stays false so no consumer conflates it with a real answer.
    """
    from alexandria import cli
    from alexandria.synthesis.pipeline import PipelineResult
    from alexandria.synthesis.write import SynthesisPage

    puts = []
    monkeypatch.setattr(cli.ResponseCache, "put",
                        lambda self, key, value: puts.append(key), raising=False)
    monkeypatch.setattr(cli, "_answer_retrieval_fingerprint",
                        lambda engine: {"fake": True}, raising=False)

    draft = SynthesisPage(topic_query="q", text="Unaudited draft text.",
                          claims=(), author="synthesis-sweep@writer@v1", skip_log=())
    result = PipelineResult(
        gathered=None, repair=None, emitted=False, page_path=None, skip_log_path=None,
        timings_ms={}, budget_exhausted=True, salvaged_page=draft)
    import alexandria.synthesis.pipeline as pipeline_mod

    # run_pipeline is imported INSIDE run_answer, so patch at its source module.
    monkeypatch.setattr(pipeline_mod, "run_pipeline",
                        lambda *a, **k: result, raising=False)

    class FakeEngine:
        embedder = type("E", (), {"name": "hash-24"})()
        reranker = type("R", (), {"model_name": "fake", "half_precision": True})()
        config = type("C", (), {"prefetch": 8, "top_k": 5, "rrf_k": 60, "wiki_boost": 1.25})()
        logger = type("L", (), {"log_usage": lambda self, **kw: None})()

    out = cli.run_answer(
        cli.AppConfig(corpus_path=tmp_path), tmp_path, "q",
        engine=FakeEngine(), k=5, llm_model="m",
        grader_a_model="a", grader_b_model="b",
        base_url=None, api_key_env=None, prompt_version="v1",
    )

    assert out.emitted is False, (
        "salvage must NOT reuse the success signal -- a consumer checking "
        "emitted would treat an unverified draft as a real answer")
    assert out.salvaged is True
    assert out.text is not None and "Unaudited draft text." in out.text
    assert puts == [], "a salvaged, unaudited draft was written to the response cache"


def test_cli_reports_SALVAGE_with_a_nonzero_exit_while_still_printing_the_draft(tmp_path, monkeypatch, capsys):
    """Red round 3: exit code must not look like success, even though text prints."""
    from alexandria import cli
    from alexandria.synthesis.pipeline import PipelineResult
    from alexandria.synthesis.write import SynthesisPage

    import alexandria.synthesis.pipeline as pipeline_mod

    draft = SynthesisPage(topic_query="q", text="Draft text.", claims=(),
                          author="synthesis-sweep@writer@v1", skip_log=())
    result = PipelineResult(
        gathered=None, repair=None, emitted=False, page_path=None, skip_log_path=None,
        timings_ms={}, budget_exhausted=True, salvaged_page=draft)
    monkeypatch.setattr(pipeline_mod, "run_pipeline", lambda *a, **k: result, raising=False)
    monkeypatch.setattr(cli, "_build_search_engine",
                        lambda *a, **k: type("E", (), {
                            "embedder": type("E", (), {"name": "x"})(),
                            "reranker": type("R", (), {"model_name": "x", "half_precision": True})(),
                            "config": type("C", (), {"prefetch": 8, "top_k": 5, "rrf_k": 60,
                                                     "wiki_boost": 1.25})(),
                            "logger": type("L", (), {"log_usage": lambda self, **kw: None})(),
                            "search": lambda *a, **k: []})(), raising=False)
    monkeypatch.setattr(cli, "read_index_generation", lambda *a: 1, raising=False)

    code = cli.cmd_answer(cli.build_parser().parse_args(
        ["--corpus", str(tmp_path), "answer", "q"]))

    assert code == 4, "salvage must exit nonzero so status-only callers see partial"
    captured = capsys.readouterr()
    assert "verification incomplete" in captured.err
    assert "Draft text." in captured.out


def test_serve_reports_SALVAGE_as_503_with_emitted_false(tmp_path, monkeypatch):
    """Red round 3: the HTTP status must not look like a verified 200 answer."""
    from alexandria import serve as serve_mod
    from alexandria.cli import AnswerOutcome

    ctx = type("Ctx", (), {
        "config": None, "corpus": tmp_path, "engine": None,
        "locked_engine": None, "embedder": None, "store": None, "lexical": None,
        "engine_lock": None, "started_monotonic": 0.0,
        "llm_defaults": {"base_url": None, "api_key_env": None, "llm_model": "m",
                         "grader_a_model": "a", "grader_b_model": "b",
                         "prompt_version": "v1", "answer_timeout": ""},
    })()
    from alexandria import cli as cli_mod

    # serve's _handle_answer does `from .cli import run_answer`, so patch the
    # SOURCE module, not serve_mod.run_answer -- the local-import trap again.
    monkeypatch.setattr(cli_mod, "run_answer", lambda *a, **k: AnswerOutcome(
        False, "Draft text.", 1, "id-1", error="budget exhausted: unaudited draft",
        salvaged=True), raising=False)

    status, raw, _ = serve_mod.dispatch(ctx, "test", "POST", "/answer",
                                        b'{"question":"q"}')
    payload = json.loads(raw)["error"]

    assert status == 503, "salvage must be a non-2xx so HTTP-status callers see partial"
    assert payload["emitted"] is False
    assert payload["salvaged"] is True
    assert payload["text"] == "Draft text."


def test_citation_records_built_from_a_full_pipeline_are_durably_logged(monkeypatch, tmp_path):
    """#9/C1: the end-to-end proof -- a real gather -> write -> judge pipeline
    result produces durable (query_id, claim_id, doc_id, chunk_id, rank,
    claim_verdict, source_round) tuples written into answers.jsonl, not
    discarded like the spec's own audit found (cli.py's response_cache.put
    only ever stored {text, n_claims}).

    Red review 2026-08-20 revision: query_id/rank/source_round now come from
    GatherResult.chunk_provenance (captured synchronously inside gather() at
    each search call), not from ambient engine state read after the fact --
    the original design joined citations to the wrong QueryLogger row in the
    normal multi-search case. This fixture builds a GatherResult with TWO
    DIFFERENT round provenance entries specifically to prove that."""
    import json as _json

    from alexandria import cli
    from alexandria.audit import AuditResult, Verdict
    from alexandria.synthesis.gather import ChunkProvenance, GatherResult, SourceChunk
    from alexandria.synthesis.judge import JudgeVerdict
    from alexandria.synthesis.pipeline import PipelineResult
    from alexandria.synthesis.repair import RepairResult
    from alexandria.synthesis.write import Citation, Claim, SynthesisPage

    round_one = (SourceChunk("sources/a#1", "sources/a", "Evidence A.", 0.9),)
    round_two = (SourceChunk("sources/b#1", "sources/b", "Evidence B.", 0.5),)
    provenance = {
        "sources/a#1": ChunkProvenance(query_id="seed-query-id", source_round="round_one", rank=1),
        "sources/b#1": ChunkProvenance(query_id="followup-query-id", source_round="round_two", rank=1),
    }
    gathered = GatherResult(
        topic_query="q", chunks=(*round_one, *round_two),
        round_one=round_one, round_two=round_two,
        follow_up_queries=("follow-up q",), gap_response='{"queries": ["follow-up q"]}',
        chunk_provenance=provenance, seed_query_id="seed-query-id")

    page = SynthesisPage(
        topic_query="q", text="Published page.",
        claims=(
            Claim("c1", "Supported claim.", (Citation("sources/a", "sources/a#1"),)),
            Claim("c2", "Unsupported claim.", (Citation("sources/b", "sources/b#1"),)),
        ),
        author="synthesis-sweep@m@v1", skip_log=())

    audit = AuditResult(verdicts=[
        Verdict(note_id="c1", verdict="supported", reason="matches"),
        Verdict(note_id="c2", verdict="unsupported", reason="not found"),
    ])
    verdict = JudgeVerdict(
        page=page, chunk_accounted=True, entailment_passed=False, coverage_passed=True,
        audit=audit, coverage=(), failed_claim_ids=("c2",),
        failing_skip_ids=(), borderline_skip_ids=(), errors=())
    repair = RepairResult(page=page, verdict=verdict, iterations=0, errors=())

    page_path = tmp_path / "wiki" / "q.md"
    page_path.parent.mkdir(parents=True)
    page_path.write_text("Published page.\n")
    result = PipelineResult(gathered=gathered, repair=repair, emitted=False,
                            page_path=page_path, skip_log_path=None, timings_ms={})

    import alexandria.synthesis.pipeline as pipeline_mod
    monkeypatch.setattr(pipeline_mod, "run_pipeline", lambda *a, **k: result, raising=False)
    monkeypatch.setattr(cli, "_answer_retrieval_fingerprint",
                        lambda engine: {"fake": True}, raising=False)

    class FakeEngine:
        embedder = type("E", (), {"name": "hash-24"})()
        reranker = type("R", (), {"model_name": "fake", "half_precision": True})()
        config = type("C", (), {"prefetch": 8, "top_k": 5, "rrf_k": 60, "wiki_boost": 1.25})()
        logger = type("L", (), {"log_usage": lambda self, **kw: None})()

    cli.run_answer(
        cli.AppConfig(corpus_path=tmp_path), tmp_path, "q",
        engine=FakeEngine(), k=5, llm_model="m",
        grader_a_model="a", grader_b_model="b",
        base_url=None, api_key_env=None, prompt_version="v1")

    rows = [_json.loads(l) for l in
           (tmp_path / ".alexandria" / "audit" / "answers.jsonl").read_text().splitlines()]
    assert len(rows) == 1
    row = rows[0]
    # ROW-LEVEL query_id is the SEED's id, always -- not whichever search ran last.
    assert row["query_id"] == "seed-query-id"
    citations = {(c["claim_id"], c["chunk_id"]): c for c in row["citations"]}
    assert len(citations) == 2

    c1 = citations[("c1", "sources/a#1")]
    assert c1["doc_id"] == "sources/a"
    assert c1["claim_verdict"] == "supported"
    assert c1["source_round"] == "round_one"
    assert c1["rank"] == 1
    # PER-CITATION query_id: the round_one chunk is joined to the SEED search.
    assert c1["query_id"] == "seed-query-id"

    c2 = citations[("c2", "sources/b#1")]
    assert c2["claim_verdict"] == "unsupported"  # the NEGATIVE signal (requirement 4)
    assert c2["source_round"] == "round_two"
    assert c2["rank"] == 1
    # THE CORE FIX: the round_two chunk is joined to ITS OWN follow-up search's
    # query_id, distinct from the seed's -- proving the join no longer collapses
    # every citation onto whichever search happened to run last.
    assert c2["query_id"] == "followup-query-id"
    assert c2["query_id"] != c1["query_id"]


def test_citation_records_stay_empty_when_no_gathered_or_no_verdict_exists():
    """_citation_records must degrade to [] rather than raise -- citation
    linkage is a durable SIGNAL, never a correctness gate an answer's
    emission depends on."""
    from alexandria.cli import _citation_records

    assert _citation_records(None, object()) == []
    assert _citation_records(object(), None) == []


def test_citation_missing_provenance_is_unknown_never_silently_seed(monkeypatch, tmp_path):
    """Red review 2026-08-20 (finding #4): a citation whose chunk_id has NO
    provenance entry (e.g. a writer-fabricated chunk_id that never came from
    any search, the exact case that also yields a 'fabricated' claim_verdict)
    must be labeled 'unknown', never silently 'seed' -- mislabeling a
    hallucination as retrieval provenance would poison training data."""
    from alexandria.audit import AuditResult, Verdict
    from alexandria.cli import _citation_records
    from alexandria.synthesis.gather import GatherResult
    from alexandria.synthesis.judge import JudgeVerdict
    from alexandria.synthesis.write import Citation, Claim, SynthesisPage

    gathered = GatherResult(topic_query="q", chunks=(), round_one=(), round_two=(),
                            follow_up_queries=(), gap_response="{}", chunk_provenance={})
    page = SynthesisPage(
        topic_query="q", text="p",
        claims=(Claim("c1", "Fabricated claim.",
                      (Citation("sources/ghost", "sources/ghost#99"),)),),
        author="a", skip_log=())
    audit = AuditResult(verdicts=[Verdict(note_id="c1", verdict="fabricated", reason="invented")])
    verdict = JudgeVerdict(page=page, chunk_accounted=True, entailment_passed=False,
                           coverage_passed=True, audit=audit, coverage=(),
                           failed_claim_ids=("c1",), failing_skip_ids=(),
                           borderline_skip_ids=(), errors=())

    records = _citation_records(gathered, verdict)
    assert len(records) == 1
    assert records[0]["source_round"] == "unknown"
    assert records[0]["query_id"] is None
    assert records[0]["rank"] is None
    assert records[0]["claim_verdict"] == "fabricated"


def test_gather_captures_provenance_synchronously_per_search_not_after_the_fact():
    """Red review 2026-08-20 (findings #1/#2, the blocking ones): gather()
    itself must read engine.last_query_id RIGHT AFTER each search() call,
    proven by an engine whose last_query_id CHANGES between calls -- a fake
    engine that increments a counter on every search() call, so if gather()
    read the id only once (or late), round_one and round_two chunks would
    share the same wrong id instead of each getting their own."""
    from alexandria.llm import ScriptedClient
    from alexandria.synthesis.gather import gather

    class ChangingIdEngine:
        def __init__(self):
            self.calls = 0
            self.last_query_id = None

        def search(self, query, *, k=None):
            self.calls += 1
            self.last_query_id = f"query-id-{self.calls}"
            return [type("R", (), {"chunk_id": f"sources/{query}#1", "doc_id": f"sources/{query}",
                                   "text": f"text for {query}", "score": 1.0})()]

    engine = ChangingIdEngine()
    llm = ScriptedClient([json.dumps({"queries": ["follow-up"]})])
    gathered = gather(engine, "seed-topic", llm=llm, seed_k=5, max_follow_up_queries=1)

    assert gathered.seed_query_id == "query-id-1"
    seed_chunk_id = "sources/seed-topic#1"
    followup_chunk_id = "sources/follow-up#1"
    assert gathered.chunk_provenance[seed_chunk_id].query_id == "query-id-1"
    assert gathered.chunk_provenance[followup_chunk_id].query_id == "query-id-2"
    # THE decisive proof: the two chunks got DIFFERENT query_ids, which is
    # only possible if the id was read immediately after each search() call,
    # not once at the end when last_query_id would hold only "query-id-2".
    assert (gathered.chunk_provenance[seed_chunk_id].query_id
           != gathered.chunk_provenance[followup_chunk_id].query_id)


def test_gather_rank_is_per_search_not_a_running_index_across_follow_ups():
    """Red review 2026-08-20 (finding #5): round_two can span MULTIPLE
    follow-up queries; rank must be 1-based WITHIN each individual search's
    own result list, never a running index across all of round_two
    concatenated (which would mint a fake, non-comparable rank for every
    chunk after the first follow-up query)."""
    from alexandria.llm import ScriptedClient
    from alexandria.synthesis.gather import gather

    class TwoResultsPerQueryEngine:
        def __init__(self):
            self.last_query_id = None
            self.n = 0

        def search(self, query, *, k=None):
            self.n += 1
            self.last_query_id = f"qid-{self.n}"
            return [
                type("R", (), {"chunk_id": f"sources/{query}-{i}#1", "doc_id": f"sources/{query}-{i}",
                              "text": "t", "score": 1.0})()
                for i in range(2)
            ]

    engine = TwoResultsPerQueryEngine()
    # gap detector asks for TWO follow-ups, each returning 2 results.
    llm = ScriptedClient([json.dumps({"queries": ["fu1", "fu2"]})])
    gathered = gather(engine, "seed", llm=llm, seed_k=5, max_follow_up_queries=2)

    # fu2's results must be rank 1,2 -- NOT rank 3,4 (which a running index
    # across fu1+fu2 concatenated would have produced).
    assert gathered.chunk_provenance["sources/fu2-0#1"].rank == 1
    assert gathered.chunk_provenance["sources/fu2-1#1"].rank == 2
    assert gathered.chunk_provenance["sources/fu1-0#1"].rank == 1
    # And each follow-up query got its OWN query_id, not a shared one.
    assert (gathered.chunk_provenance["sources/fu1-0#1"].query_id
           != gathered.chunk_provenance["sources/fu2-0#1"].query_id)


def test_cache_hit_answer_row_carries_a_real_back_pointer_not_an_assertion(monkeypatch, tmp_path):
    """Red review 2026-08-20 (finding #3): "linkage already exists in the
    original row" was previously an unenforced, unverifiable comment. Now the
    response cache stores the original answer_id, and a cache-hit's audit row
    carries it as a real, mechanically-followable back-pointer."""
    from alexandria import cli
    from alexandria.cache import ResponseCache

    corpus = tmp_path
    response_cache = ResponseCache(corpus)
    fake_key = "fake-rkey-123"
    response_cache.put(fake_key, {"text": "Cached answer.", "n_claims": 1,
                                  "answer_id": "original-answer-id-xyz"})

    monkeypatch.setattr(cli.ResponseCache, "key", lambda self, *a, **k: fake_key)

    class FakeEngine:
        embedder = type("E", (), {"name": "hash-24"})()
        reranker = type("R", (), {"model_name": "fake", "half_precision": True})()
        config = type("C", (), {"prefetch": 8, "top_k": 5, "rrf_k": 60, "wiki_boost": 1.25})()
        logger = type("L", (), {"log_usage": lambda self, **kw: None})()

    outcome = cli.run_answer(
        cli.AppConfig(corpus_path=corpus), corpus, "q",
        engine=FakeEngine(), k=5, llm_model="m",
        grader_a_model="a", grader_b_model="b",
        base_url=None, api_key_env=None, prompt_version="v1")
    assert outcome.cached is True

    rows = [json.loads(l) for l in
           (corpus / ".alexandria" / "audit" / "answers.jsonl").read_text().splitlines()]
    assert rows[-1]["trace"]["source_answer_id"] == "original-answer-id-xyz"
