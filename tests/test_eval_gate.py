"""The regression gate: fires only on retrieval-relevant changes, skips cleanly when
there's no private corpus (fresh clone / CI), and its watched-path list stays narrow
on purpose -- broadening it is how a cheap gate becomes friction people route around.
"""

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "eval_gate", Path(__file__).resolve().parent.parent / "scripts" / "eval-gate.py")
eval_gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(eval_gate)


def test_unrelated_change_does_not_touch_watched_paths():
    changed = ["README.md", "tests/test_cli.py", "docs/WORK-ORDER-phase1-eval-harness.md"]
    assert not any(f.startswith(eval_gate.WATCHED) for f in changed)


def test_retrieval_change_is_watched():
    for f in ("src/alexandria/index/embedder.py", "src/alexandria/retrieval/search.py",
              "src/alexandria/config.py"):
        assert any(f.startswith(w) for w in eval_gate.WATCHED), f


def test_connector_change_is_not_watched():
    """The gate is scoped to what can move retrieval quality. A connector change
    (e.g. pi_sessions.py) cannot, so it must not pay eval wall-clock time."""
    assert not any("src/alexandria/connectors/pi_sessions.py".startswith(w)
                   for w in eval_gate.WATCHED)


def test_skips_cleanly_when_corpus_is_absent(tmp_path, monkeypatch):
    """A fresh clone or CI box has no private corpus. The gate must skip (exit 0),
    never block on infrastructure that legitimately doesn't exist there."""
    monkeypatch.setattr(eval_gate, "staged_files",
                        lambda: ["src/alexandria/retrieval/search.py"])
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert eval_gate.main() == 0
