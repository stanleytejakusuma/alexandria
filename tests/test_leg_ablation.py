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
