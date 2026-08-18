"""Tests for scripts/leg-ablation.py's gating decision (BACKLOG #47/#48)."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from alexandria.eval.history import Delta

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "leg-ablation.py"
_spec = importlib.util.spec_from_file_location("leg_ablation", SCRIPT_PATH)
leg_ablation = importlib.util.module_from_spec(_spec)
sys.modules["leg_ablation"] = leg_ablation
_spec.loader.exec_module(leg_ablation)


def _delta(recall, mrr, p, *, miss_to_hit=(), hit_to_miss=()):
    return Delta(recall_at_k=recall, mrr=mrr, hit_to_miss=list(hit_to_miss),
                 miss_to_hit=list(miss_to_hit), p_value=p)


def test_removing_a_leg_that_significantly_improves_is_dead_weight(monkeypatch):
    monkeypatch.setattr(leg_ablation, "compare",
                        lambda prev, cur: _delta(0.12, 0.30, 0.01, miss_to_hit=["a"] * 6))
    failures, obs = leg_ablation.dead_weight_verdict(None, {"dense": None})
    assert failures, "a significant improvement must be flagged as dead weight"
    assert "dense" in failures[0]


def test_improvement_without_significance_is_reported_not_gated(monkeypatch):
    monkeypatch.setattr(leg_ablation, "compare",
                        lambda prev, cur: _delta(0.05, 0.10, 0.25, miss_to_hit=["a"]))
    failures, obs = leg_ablation.dead_weight_verdict(None, {"dense": None})
    assert failures == []
    assert obs["dense"]["note"].startswith("improved but not significant")


def test_a_leg_that_hurts_is_not_dead_weight(monkeypatch):
    monkeypatch.setattr(leg_ablation, "compare",
                        lambda prev, cur: _delta(-0.10, -0.05, 0.01, hit_to_miss=["a"] * 6))
    failures, obs = leg_ablation.dead_weight_verdict(None, {"lexical": None})
    assert failures == []
    assert "note" not in obs["lexical"]


def test_no_change_is_not_dead_weight(monkeypatch):
    monkeypatch.setattr(leg_ablation, "compare",
                        lambda prev, cur: _delta(0.0, 0.0, 1.0))
    failures, _ = leg_ablation.dead_weight_verdict(None, {"dense": None})
    assert failures == []


def test_ablation_builds_its_query_embedder_with_a_read_only_cache(tmp_path, monkeypatch):
    """The script's engine construction must opt in; disabling result caching alone
    does not stop ``CachedEmbedder`` from inserting a query miss."""
    corpus = tmp_path / "corpus"
    golden = corpus / ".alexandria" / "golden" / "golden-v1.jsonl"
    golden.parent.mkdir(parents=True)
    golden.write_text('{"id":"q","query":"q","must_retrieve":["sources/a"],"k":1}\n')
    (corpus / ".alexandria" / "index").mkdir()

    monkeypatch.setattr(leg_ablation, "load_golden", lambda _: [])
    monkeypatch.setattr(leg_ablation, "verify_targets", lambda entries, root: [])
    monkeypatch.setattr(leg_ablation, "load_config", lambda **_: object())
    built: dict = {}

    class Engine:
        logger = None
        embedder = object()
        bm25 = None

    def build(config, path, **kwargs):
        built.update(kwargs)
        return Engine()

    monkeypatch.setattr(leg_ablation, "_build_search_engine", build)

    class Report:
        class summary:
            @staticmethod
            def to_dict():
                return {}

    monkeypatch.setattr(leg_ablation, "_score", lambda engine, entries: Report())
    monkeypatch.setattr(leg_ablation, "dead_weight_verdict", lambda baseline, variants: ([], {}))

    assert leg_ablation.main(["--corpus", str(corpus), "--json"]) == 0
    assert built == {
        "query_cache": False,
        "embedding_cache_read_only": True,
        "client": "leg-ablation",
    }
