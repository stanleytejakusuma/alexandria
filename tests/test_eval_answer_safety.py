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
