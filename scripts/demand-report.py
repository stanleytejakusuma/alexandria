#!/usr/bin/env python3
"""Demand report: is Alexandria's low query volume a demand or supply problem?

Answers the four questions from docs/DECISION-capture-cadence-2026-08-14.md step 0:
  1. Volume by client over time, separating real usage from eval/test infrastructure.
  2. Age of retrieved documents at query time (the freshness falsifier metric).
  3. Failed/empty retrievals among real queries.
  4. Latency actually experienced, cold vs warm.

READ-ONLY. Opens ~/alexandria-corpus/.alexandria/queries.sqlite with mode=ro and never
writes to the corpus. Re-runnable weekly to watch the trend (see docs/DEMAND-REPORT-*.md
for the methodology writeup and the first run's numbers).

Classification methodology (see docs/DEMAND-REPORT-*.md "Methodology" for the full
rationale): a query row is classified as one of:
  - eval_infra     confirmed automated eval/test traffic
  - synthetic_probe confirmed canary/health-check/calibration traffic
  - genuine         confirmed or high-confidence real human/agent usage
  - uncertain       could not be confidently classified either way

The two "confirmed" buckets are established by direct evidence (exact text match to a
committed golden/eval query set, or an audit-log caller identity known to be
non-interactive). Everything else falls to burst-timing heuristics, which are
probabilistic and explained in the doc -- do not treat "genuine" counts from the
heuristic path as precise; treat them as an informed estimate bounded by the
"uncertain" bucket.
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

DEFAULT_CORPUS = Path("~/alexandria-corpus").expanduser()

# Client values that are eval/calibration harnesses by construction (see
# src/alexandria/eval/negative.py, scripts/contest-recall.py private counterpart --
# both windows are single-purpose, minutes-long calibration runs).
CONFIRMED_EVAL_CLIENTS = {"separation", "negative-probe"}

# Text fingerprints of canary/health-check probes observed hitting the live `serve`
# daemon (client='serve') anonymously. These never carry real information content.
SYNTHETIC_TEXT_PATTERNS = [
    re.compile(r"\bcanary\b", re.I),
    re.compile(r"\bobscure phrase\b", re.I),
    re.compile(r"\bnovel probe\b", re.I),
    re.compile(r"zebra quantum ledger", re.I),
    re.compile(r"^\[\[.*\]\]$"),
    re.compile(r"^Session: [0-9a-fA-F-]{36}$"),
    re.compile(r"^ssh-fallback-hardening$"),
]

# Audit-log caller identities (see src/alexandria/auditlog.py, --caller flag) that are
# confirmed non-interactive: consumer-audit = a self-test sweep of the audit pathway
# itself (8 queries in a single 4-minute window on one date, including a deliberately
# absurd control query).
#
# `local-anonymous` is deliberately NOT in this set. serve.py:45 assigns it as a fixed
# identity for *any* TCP caller, so it carries no information about who called -- a
# canary probe and a real question from the pi extension are stamped identically.
# Treating it as synthetic made "no genuine query ever reached the daemon" true by
# construction rather than by measurement, and discarded 10 real queries. Daemon rows
# are therefore classified on their text content (is_synthetic_text) like any other row.
SYNTHETIC_CALLERS = {"consumer-audit"}
# pi-extension = the AlexandriaSearch/AlexandriaContext/AlexandriaAnswer tool bindings
# in ~/.pi/agent/extensions/alexandria.ts (confirmed by reading that file: it sets
# ALEXANDRIA_CALLER=pi-extension on every CLI-exec fallback call). This is exactly the
# "agent retrieves proactively" surface the invocation proposal is about.
GENUINE_CALLERS = {"pi-extension", "cli"}

BURST_GAP_SECONDS = 2.0  # gaps below this, for unattributed cli rows, match the
# measured burst signature of confirmed eval traffic (median 0.54s, 69-76% < 2s)
# almost exactly -- see docs/DEMAND-REPORT-*.md for the comparison numbers.

# A maximal run of same-client rows with < BATCH_GAP_SECONDS between consecutive
# timestamps, of length >= BATCH_MIN_SIZE, is a replayed benchmark batch: spot-checked
# example is 71 queries fired within one wall-clock second (cache_hit=1, sub-millisecond
# latency each) -- structurally impossible for a human or a single-shot 10-70s-cold CLI
# invocation, since real invocations of this tool take single-digit-to-tens of seconds
# per query (see latency section). This check runs BEFORE the golden-set/caller checks
# and overrides them, because it caught batches (~89-question replayed benchmark sets)
# that do not appear in any golden/negative/contest jsonl file checked here -- i.e. a
# different or newer eval query set this script could not otherwise identify by content.
BATCH_GAP_SECONDS = 5.0
BATCH_MIN_SIZE = 5

GOLDEN_FILES = [
    ".alexandria/golden/golden-v1.jsonl",
    ".alexandria/golden/negative-v1.jsonl",
    ".alexandria/golden/contradiction-pairs-v1.jsonl",
    ".alexandria/contest/contest-run1-blind.jsonl",
]

FRONTMATTER_AT_RE = re.compile(r"^\s*at:\s*'?\"?([0-9]{4}-[0-9]{2}-[0-9]{2}[^'\"\n]*)'?\"?\s*$")


def load_queries(db_path: Path) -> list[dict]:
    uri = f"file:{db_path}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT query_id, ts, q, filters, tier, retrieved_ids, scores, "
            "latency_ms, cache_hit, client FROM queries ORDER BY ts"
        ).fetchall()
    finally:
        con.close()
    out = []
    for r in rows:
        d = dict(r)
        d["ts_dt"] = datetime.datetime.fromisoformat(d["ts"])
        try:
            d["retrieved_ids"] = json.loads(d["retrieved_ids"])
        except (json.JSONDecodeError, TypeError):
            d["retrieved_ids"] = []
        try:
            d["scores"] = json.loads(d["scores"])
        except (json.JSONDecodeError, TypeError):
            d["scores"] = []
        out.append(d)
    return out


def load_golden_queries(corpus: Path) -> set[str]:
    qs: set[str] = set()
    for rel in GOLDEN_FILES:
        p = corpus / rel
        if not p.exists():
            continue
        with open(p) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(d.get("query"), str):
                    qs.add(d["query"])
    return qs


def load_audit_callers(audit_path: Path) -> list[tuple[datetime.datetime, str, str]]:
    """Returns list of (ts, query_text, caller) from the audit search log."""
    if not audit_path.exists():
        return []
    out = []
    with open(audit_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = d.get("ts")
            q = d.get("query")
            caller = d.get("caller", "cli")
            if ts is None or q is None:
                continue
            try:
                dt = datetime.datetime.fromisoformat(ts)
            except ValueError:
                continue
            out.append((dt, q, caller))
    return out


def match_audit_callers(queries: list[dict], audit_rows: list) -> dict[str, str]:
    """Greedy nearest-timestamp match of audit rows onto query_id, tolerance 120s.

    Returns {query_id: caller}. Each audit row claims at most one query row, and
    each query row is claimed by at most one audit row (first-match-wins by
    chronological audit order), matching the reconciliation done by hand while
    building this script (verified 0 unmatched of 97 audit rows against the
    2026-08-14 snapshot).
    """
    by_text: dict[str, list[dict]] = defaultdict(list)
    for row in queries:
        by_text[row["q"]].append(row)

    claimed: set[str] = set()
    result: dict[str, str] = {}
    for dt, q, caller in audit_rows:
        candidates = by_text.get(q, [])
        best = None
        best_delta = None
        for row in candidates:
            if row["query_id"] in claimed:
                continue
            delta = abs((row["ts_dt"] - dt).total_seconds())
            if delta < 120 and (best_delta is None or delta < best_delta):
                best, best_delta = row, delta
        if best is not None:
            claimed.add(best["query_id"])
            result[best["query_id"]] = caller
    return result


def is_synthetic_text(q: str) -> bool:
    return any(p.search(q) for p in SYNTHETIC_TEXT_PATTERNS)


def find_batch_replay_ids(queries: list[dict]) -> set[str]:
    """Detect replayed-benchmark burst windows: maximal runs of same-client rows
    with < BATCH_GAP_SECONDS between consecutive timestamps, of length >= BATCH_MIN_SIZE.
    Returns the set of query_ids inside any such window (see BATCH_GAP_SECONDS docstring
    for why this is a safe, high-precision automation signal).
    """
    by_client: dict[str, list[dict]] = defaultdict(list)
    for row in queries:
        by_client[row["client"]].append(row)
    batch_ids: set[str] = set()
    for rows in by_client.values():
        rows_sorted = sorted(rows, key=lambda r: r["ts_dt"])
        window: list[dict] = [rows_sorted[0]] if rows_sorted else []
        for prev_row, row in zip(rows_sorted, rows_sorted[1:]):
            gap = (row["ts_dt"] - prev_row["ts_dt"]).total_seconds()
            if gap < BATCH_GAP_SECONDS:
                window.append(row)
            else:
                if len(window) >= BATCH_MIN_SIZE:
                    batch_ids.update(r["query_id"] for r in window)
                window = [row]
        if len(window) >= BATCH_MIN_SIZE:
            batch_ids.update(r["query_id"] for r in window)
    return batch_ids


def classify(queries: list[dict], golden: set[str], callers: dict[str, str]) -> dict[str, str]:
    """Returns {query_id: label} where label in eval_infra/synthetic_probe/genuine/uncertain."""
    labels: dict[str, str] = {}
    batch_ids = find_batch_replay_ids(queries)

    # Pass 1: gap-to-previous-same-client, needed for the burst heuristic on
    # unattributed 'cli' rows.
    by_client: dict[str, list[dict]] = defaultdict(list)
    for row in queries:
        by_client[row["client"]].append(row)
    prev_gap: dict[str, float | None] = {}
    for client, rows in by_client.items():
        rows_sorted = sorted(rows, key=lambda r: r["ts_dt"])
        prev_ts = None
        for row in rows_sorted:
            if prev_ts is None:
                prev_gap[row["query_id"]] = None
            else:
                prev_gap[row["query_id"]] = (row["ts_dt"] - prev_ts).total_seconds()
            prev_ts = row["ts_dt"]

    for row in queries:
        qid = row["query_id"]
        client = row["client"]
        q = row["q"]

        if qid in batch_ids:
            labels[qid] = "eval_infra"
            continue
        if client in CONFIRMED_EVAL_CLIENTS:
            labels[qid] = "eval_infra"
            continue
        if q in golden:
            labels[qid] = "eval_infra"
            continue

        caller = callers.get(qid)
        if caller in SYNTHETIC_CALLERS:
            labels[qid] = "synthetic_probe"
            continue
        if is_synthetic_text(q):
            labels[qid] = "synthetic_probe"
            continue
        if caller in GENUINE_CALLERS:
            labels[qid] = "genuine"
            continue

        if client == "cli":
            gap = prev_gap.get(qid)
            if gap is not None and gap < BURST_GAP_SECONDS:
                labels[qid] = "eval_infra"  # burst signature matches confirmed eval traffic
            else:
                labels[qid] = "uncertain"
            continue

        if client in ("search", "serve", "answer"):
            labels[qid] = "genuine"
            continue

        labels[qid] = "uncertain"

    return labels


def resolve_doc_date(corpus: Path, source_ref: str, cache: dict[str, datetime.datetime | None]) -> datetime.datetime | None:
    rel = source_ref.split("#", 1)[0]
    if rel in cache:
        return cache[rel]
    candidates = [corpus / f"{rel}.md", corpus / rel]
    result = None
    for path in candidates:
        if not path.exists():
            continue
        try:
            with open(path) as f:
                lines = [next(f) for _ in range(20)]
        except (StopIteration, OSError):
            try:
                lines = path.read_text().splitlines(keepends=True)[:20]
            except OSError:
                lines = []
        in_generated = False
        for line in lines:
            if line.strip() == "generated:":
                in_generated = True
                continue
            if in_generated:
                m = FRONTMATTER_AT_RE.match(line)
                if m:
                    raw = m.group(1)
                    try:
                        result = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
                        if result.tzinfo is None:
                            result = result.replace(tzinfo=datetime.timezone.utc)
                    except ValueError:
                        result = None
                    break
                if not line.startswith(" "):
                    break
        if result is not None:
            break
    cache[rel] = result
    return result


def age_bucket(delta_hours: float) -> str:
    if delta_hours < 48:
        return "<48h"
    if delta_hours < 24 * 7:
        return "48h-1wk"
    if delta_hours < 24 * 30:
        return "1wk-1mo"
    return ">1mo"


def fmt_dist(values: list[float]) -> str:
    if not values:
        return "n=0"
    values = sorted(values)
    n = len(values)
    def pct(p):
        idx = min(n - 1, int(round(p * (n - 1))))
        return values[idx]
    return (f"n={n} min={pct(0):.1f} p50={pct(0.5):.1f} p90={pct(0.9):.1f} "
            f"max={pct(1.0):.1f}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    ap.add_argument("--json-out", type=Path, default=None, help="optional machine-readable summary path")
    args = ap.parse_args()

    corpus = args.corpus
    db_path = corpus / ".alexandria" / "queries.sqlite"
    audit_path = corpus / ".alexandria" / "audit" / "search.jsonl"

    if not db_path.exists():
        print(f"ERROR: {db_path} not found", file=sys.stderr)
        return 1

    queries = load_queries(db_path)
    golden = load_golden_queries(corpus)
    audit_rows = load_audit_callers(audit_path)
    callers = match_audit_callers(queries, audit_rows)
    labels = classify(queries, golden, callers)

    label_counts = Counter(labels.values())
    client_label = Counter((r["client"], labels[r["query_id"]]) for r in queries)

    print("=" * 70)
    print("ALEXANDRIA DEMAND REPORT")
    print(f"corpus: {corpus}")
    print(f"queries.sqlite: {len(queries)} rows, "
          f"{queries[0]['ts'][:10]} .. {queries[-1]['ts'][:10]}" if queries else "no rows")
    print(f"audit search.jsonl: {len(audit_rows)} rows matched: {len(callers)}")
    print("=" * 70)

    print("\n--- 1. VOLUME BY CLASSIFICATION ---")
    total = len(queries)
    for label in ("eval_infra", "synthetic_probe", "genuine", "uncertain"):
        n = label_counts.get(label, 0)
        print(f"  {label:16s} {n:5d}  ({n / total * 100:5.1f}%)")
    print("\n  by (client, classification):")
    for (client, label), n in sorted(client_label.items()):
        print(f"    {client:14s} {label:16s} {n:5d}")

    print("\n--- 1b. GENUINE VOLUME BY DAY ---")
    genuine_ids = {qid for qid, lab in labels.items() if lab == "genuine"}
    genuine_rows = [r for r in queries if r["query_id"] in genuine_ids]
    by_day = Counter(r["ts"][:10] for r in genuine_rows)
    for day in sorted(by_day):
        print(f"    {day}  {by_day[day]}")
    uncertain_ids = {qid for qid, lab in labels.items() if lab == "uncertain"}
    uncertain_rows = [r for r in queries if r["query_id"] in uncertain_ids]
    by_day_u = Counter(r["ts"][:10] for r in uncertain_rows)
    if by_day_u:
        print("  (uncertain, for comparison):")
        for day in sorted(by_day_u):
            print(f"    {day}  {by_day_u[day]}")

    print("\n--- 2. AGE OF RETRIEVED DOCUMENTS AT QUERY TIME (genuine queries only) ---")
    date_cache: dict[str, datetime.datetime | None] = {}
    ages_hours: list[float] = []
    unresolved = 0
    total_ids = 0
    for row in genuine_rows:
        for rid in row["retrieved_ids"]:
            total_ids += 1
            doc_dt = resolve_doc_date(corpus, rid, date_cache)
            if doc_dt is None:
                unresolved += 1
                continue
            delta = (row["ts_dt"] - doc_dt).total_seconds() / 3600.0
            if delta < 0:
                continue  # clock skew / same-day generated-at granularity; skip rather than mislead
            ages_hours.append(delta)
    print(f"  retrieved doc references: {total_ids}, resolved dates: {len(ages_hours)}, "
          f"unresolved: {unresolved}")
    print(f"  age distribution (hours): {fmt_dist(ages_hours)}")
    bucket_counts = Counter(age_bucket(h) for h in ages_hours)
    for b in ("<48h", "48h-1wk", "1wk-1mo", ">1mo"):
        n = bucket_counts.get(b, 0)
        pct = n / len(ages_hours) * 100 if ages_hours else 0
        print(f"    {b:10s} {n:4d}  ({pct:5.1f}%)")

    print("\n--- 3. FAILED / EMPTY RETRIEVALS (genuine queries only) ---")
    empty = [r for r in genuine_rows if not r["retrieved_ids"]]
    weak_threshold = None
    all_top_scores = [max(r["scores"]) for r in genuine_rows if r["scores"]]
    if all_top_scores:
        all_top_scores_sorted = sorted(all_top_scores)
        weak_threshold = all_top_scores_sorted[max(0, len(all_top_scores_sorted) // 10 - 1)]
    weak = [r for r in genuine_rows if r["scores"] and weak_threshold is not None
            and max(r["scores"]) <= weak_threshold]
    print(f"  empty retrieval (0 results): {len(empty)} / {len(genuine_rows)}")
    if weak_threshold is not None:
        print(f"  weak top-score (<= bottom decile, {weak_threshold:.3f}): {len(weak)} / {len(genuine_rows)}")
    if empty:
        print("  empty-retrieval dates (query text withheld from report -- see raw db):")
        for r in empty:
            print(f"    {r['ts'][:16]}  client={r['client']}")

    print("\n--- 4. LATENCY (ms), cold vs warm, by client ---")
    for client in sorted({r["client"] for r in queries}):
        rows = [r for r in queries if r["client"] == client]
        cold = [r["latency_ms"] for r in rows if not r["cache_hit"]]
        warm = [r["latency_ms"] for r in rows if r["cache_hit"]]
        print(f"  {client}:")
        print(f"    cold: {fmt_dist(cold)}")
        print(f"    warm: {fmt_dist(warm)}")
    print("  genuine-only:")
    cold_g = [r["latency_ms"] for r in genuine_rows if not r["cache_hit"]]
    warm_g = [r["latency_ms"] for r in genuine_rows if r["cache_hit"]]
    print(f"    cold: {fmt_dist(cold_g)}")
    print(f"    warm: {fmt_dist(warm_g)}")

    if args.json_out:
        summary = {
            "corpus": str(corpus),
            "total_rows": total,
            "date_range": [queries[0]["ts"], queries[-1]["ts"]] if queries else None,
            "label_counts": dict(label_counts),
            "client_label_counts": {f"{c}/{l}": n for (c, l), n in client_label.items()},
            "genuine_by_day": dict(by_day),
            "age_distribution_hours": {
                "n": len(ages_hours),
                "buckets": dict(bucket_counts),
            } if ages_hours else None,
            "failed_retrievals": {"empty": len(empty), "weak": len(weak) if weak_threshold else None},
            "latency_genuine_ms": {
                "cold": fmt_dist(cold_g),
                "warm": fmt_dist(warm_g),
            },
        }
        args.json_out.write_text(json.dumps(summary, indent=2, default=str))
        print(f"\nJSON summary written to {args.json_out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
