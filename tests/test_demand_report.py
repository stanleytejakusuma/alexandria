"""Tests for scripts/demand-report.py's classification logic.

Loaded by path (the script lives under scripts/, not src/alexandria/, and is not
a package) since it's a standalone report tool, not engine code. See
scripts/demand-report.py's module docstring for what it does and why.
"""
import datetime
import importlib.util
import sys
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "demand-report.py"
_spec = importlib.util.spec_from_file_location("demand_report", SCRIPT_PATH)
demand_report = importlib.util.module_from_spec(_spec)
sys.modules["demand_report"] = demand_report
_spec.loader.exec_module(demand_report)


def _row(qid, ts, q, client="cli"):
    return {
        "query_id": qid,
        "ts": ts,
        "ts_dt": datetime.datetime.fromisoformat(ts),
        "q": q,
        "client": client,
        "retrieved_ids": [],
        "scores": [],
        "latency_ms": 100.0,
        "cache_hit": 1,
    }


def test_find_batch_replay_ids_flags_tight_burst_of_five_plus():
    # 6 rows, 1s apart -> a burst of >= BATCH_MIN_SIZE(5) with gaps < BATCH_GAP_SECONDS(5.0)
    rows = [
        _row(f"burst{i}", f"2026-08-12T17:57:34.{i:02d}0000+00:00", f"q{i}")
        for i in range(6)
    ]
    batch_ids = demand_report.find_batch_replay_ids(rows)
    assert batch_ids == {r["query_id"] for r in rows}


def test_find_batch_replay_ids_does_not_flag_human_paced_cluster():
    # 5 rows, ~10 minutes apart each -> below BATCH_MIN_SIZE-worthy tight spacing,
    # matches a real repeated-question cluster pattern found in the corpus
    # (human-paced iterative research, gaps of tens of minutes, not seconds).
    times = [
        "2026-08-11T12:56:34+00:00",
        "2026-08-11T13:01:00+00:00",
        "2026-08-11T13:20:35+00:00",
        "2026-08-11T13:26:49+00:00",
        "2026-08-11T13:34:24+00:00",
    ]
    rows = [_row(f"human{i}", ts, "same repeated question") for i, ts in enumerate(times)]
    batch_ids = demand_report.find_batch_replay_ids(rows)
    assert batch_ids == set()


def test_find_batch_replay_ids_below_min_size_not_flagged():
    # 4 rows, 1s apart -> tight timing but below BATCH_MIN_SIZE(5), must NOT be flagged.
    rows = [
        _row(f"small{i}", f"2026-08-12T09:00:0{i}+00:00", f"q{i}")
        for i in range(4)
    ]
    batch_ids = demand_report.find_batch_replay_ids(rows)
    assert batch_ids == set()


def test_find_batch_replay_ids_per_client_isolation():
    # Two clients interleaved in time; a burst on one client must not consume rows
    # from the other client even if globally close in time.
    cli_rows = [
        _row(f"cli{i}", f"2026-08-12T10:00:0{i}+00:00", f"q{i}", client="cli")
        for i in range(6)
    ]
    search_rows = [_row("search0", "2026-08-12T10:00:02+00:00", "unrelated", client="search")]
    batch_ids = demand_report.find_batch_replay_ids(cli_rows + search_rows)
    assert batch_ids == {r["query_id"] for r in cli_rows}
    assert "search0" not in batch_ids


def test_classify_batch_replay_overrides_pi_extension_caller():
    # Even a row whose audit-matched caller looks genuine (pi-extension) must be
    # classified eval_infra if it's part of a confirmed tight-burst replay window --
    # this is the priority-ordering guarantee documented in classify()'s docstring.
    rows = [
        _row(f"burst{i}", f"2026-08-12T17:57:34.{i:02d}0000+00:00", f"q{i}")
        for i in range(6)
    ]
    callers = {"burst0": "pi-extension"}
    labels = demand_report.classify(rows, golden=set(), callers=callers)
    assert labels["burst0"] == "eval_infra"
    assert all(v == "eval_infra" for v in labels.values())


def test_classify_genuine_cli_caller_outside_burst():
    rows = [_row("g1", "2026-08-11T11:14:13+00:00", "a real one-off question")]
    labels = demand_report.classify(rows, golden=set(), callers={"g1": "cli"})
    assert labels["g1"] == "genuine"


def test_daemon_row_with_real_content_is_not_labelled_synthetic():
    """A `serve` row whose text carries real information content must not be
    discarded as a probe merely because the daemon stamped it `local-anonymous`.

    serve.py:45 assigns LOCAL_ANONYMOUS as a *fixed identity for any TCP caller*,
    so that caller value carries no discriminative information. Treating it as a
    SYNTHETIC_CALLER made "no genuine query ever reached the daemon" true by
    construction, and dropped 10 real queries -- including one
    ("Prime Agent system prompt") that appears twice 2.5 minutes apart: once via
    the extension's CLI fallback (caller=pi-extension, counted genuine) and once
    via its HTTP primary path (client=serve, discarded). Same human, same intent.

    alexandria.ts is HTTP-first with CLI as fallback, so client=serve is the
    extension's *primary* surface -- precisely where genuine usage lands.
    """
    row = _row(
        "d1",
        "2026-08-14T01:50:00+00:00",
        "why did pi-king dashboard render garbage characters in every row",
        client="serve",
    )
    labels = demand_report.classify([row], golden=set(), callers={"d1": "local-anonymous"})
    assert labels["d1"] == "genuine"


def test_daemon_row_with_probe_text_is_still_synthetic():
    """The converse guard: fingerprinted canary text on the daemon stays synthetic,
    so the fix above widens the genuine bucket by content, not indiscriminately.
    """
    row = _row(
        "d2",
        "2026-08-13T13:06:00+00:00",
        "obscure phrase 15942 zebra quantum ledger",
        client="serve",
    )
    labels = demand_report.classify([row], golden=set(), callers={"d2": "local-anonymous"})
    assert labels["d2"] == "synthetic_probe"


if __name__ == "__main__":
    import subprocess
    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
