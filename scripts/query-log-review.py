#!/usr/bin/env python3
"""Weekly query-log review — the usage-driven learning loop's instrument.

Reads the corpus query log (.alexandria/queries.sqlite) and reports the
signals that drive Alexandria's self-improvement WITHOUT retraining:

  - query volume/coverage: what got asked, by which client
  - zero-hit queries: the retrieval gaps (the organic "missed it" label)
  - cluster jumps: how many distinct corpus areas each query's results
    span (memory-to-memory navigation quality; 1 area on a compound
    question is a weak answer, N areas on a narrow question is noise)
  - latency/cache: retrieval cost health

Usage:  .venv/bin/python scripts/query-log-review.py [--corpus DIR] [--since DAYS]
Exit 0 always; prints a compact report + suggested next actions.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--corpus", default=os.environ.get("ALEXANDRIA_CORPUS", "~/alexandria-corpus"))
    p.add_argument("--since", type=int, default=7, help="review window in days (default 7)")
    args = p.parse_args()

    db = Path(args.corpus).expanduser() / ".alexandria" / "queries.sqlite"
    if not db.exists():
        print(f"no query log at {db}")
        return 0
    con = sqlite3.connect(db)
    since = (datetime.now(timezone.utc) - timedelta(days=args.since)).isoformat()
    rows = con.execute(
        "SELECT ts, q, tier, retrieved_ids, latency_ms, cache_hit, client "
        "FROM queries WHERE ts >= ? ORDER BY ts", (since,)).fetchall()
    con.close()
    if not rows:
        print(f"no queries in the last {args.since} day(s)")
        return 0

    total = len(rows)
    zero_hit = [r for r in rows if not r[3]]
    clients = Counter(r[6] for r in rows)
    tiers = Counter(r[2] for r in rows)
    lat = [r[4] for r in rows if r[4] is not None]
    cache = sum(1 for r in rows if r[5]) / total

    # cluster jumps: distinct top-level corpus areas among retrieved docs
    area_hits: dict[str, set[str]] = defaultdict(set)
    jumps: list[tuple[str, int]] = []
    for ts, q, _tier, ids, *_rest in rows:
        if not ids:
            continue
        try:
            doc_ids = json.loads(ids) if ids.startswith("[") else ids.split(",")
        except json.JSONDecodeError:
            continue
        areas = set()
        for d in doc_ids:
            parts = d.strip("/").split("/")
            areas.add(parts[0] if len(parts) > 1 else "(root)")
            area_hits[parts[0] if len(parts) > 1 else "(root)"].add(d)
        jumps.append((q[:60], len(areas)))

    print(f"== query log review (last {args.since}d): {total} queries "
          f"since {rows[0][0][:10]}")
    print(f"clients: {dict(clients)}")
    print(f"tiers: {dict(tiers)}")
    print(f"zero-hit (gaps): {len(zero_hit)} ({len(zero_hit)/total:.0%})")
    print(f"avg latency: {sum(lat)/len(lat):.0f}ms  cache hits: {cache:.0%}")
    if jumps:
        avg_jumps = sum(j for _, j in jumps) / len(jumps)
        print(f"cluster jumps per query: avg {avg_jumps:.1f} areas "
              f"(max {max(j for _, j in jumps)})")
        one_area = [q for q, j in jumps if j == 1]
        if one_area:
            print("single-area queries (possible weak navigation):")
            for q in one_area[:5]:
                print(f"  - {q}")
    if area_hits:
        print("areas hit:")
        for area, docs in sorted(area_hits.items(), key=lambda kv: -len(kv[1]))[:8]:
            print(f"  {area}: {len(docs)} distinct docs")
    if zero_hit:
        print("zero-hit queries (gap triggers — consider wiki re-synthesis "
              "for the topic, or better queries):")
        for _, q, tier, *_ in zero_hit[:8]:
            print(f"  - [{tier}] {q[:80]}")
    print("suggested next actions: re-synthesize wiki pages for repeated "
          "zero-hit topics; review single-area answers on compound queries; "
          "tune hybrid weights only when a pattern persists >= 2 weeks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
