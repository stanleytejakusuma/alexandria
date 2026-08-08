"""Elementary pipeline audit logs (v1).

Platform-independent outlet logs for the Alexandria pipeline itself:
every `answer` call and every connector `sync` run appends one JSONL row
under <corpus>/.alexandria/audit/. `alexandria audit` summarizes them.

This is the elementary layer the dashboard's audit tab will render later;
it exists now so the data accumulates from the first day of real use.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

__all__ = ["AuditLogger", "audit_log_dir", "audit_summary"]


def audit_log_dir(corpus: Path) -> Path:
    d = corpus / ".alexandria" / "audit"
    d.mkdir(parents=True, exist_ok=True)
    return d


class AuditLogger:
    """Appends one JSONL row per event. Never raises on write failure
    (auditing must not take down the audited pipeline)."""

    def __init__(self, corpus: Path):
        self.dir = audit_log_dir(corpus)
        self.errors: list[str] = []

    def _append(self, name: str, row: dict) -> None:
        try:
            with open(self.dir / f"{name}.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, sort_keys=True) + "\n")
        except OSError as exc:
            self.errors.append(f"{name}: {exc}")

    def answer(self, *, query: str, total_ms: int, emitted: bool,
               model: str, n_claims: int = 0, failed_claims: list[str] | None = None,
               error: str = "", stages: dict[str, int] | None = None,
               caller: str = "cli", user: str = "local",
               trace: dict | None = None) -> None:
        self._append("answers", {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "kind": "answer", "query": query, "total_ms": total_ms,
            "emitted": emitted, "model": model, "n_claims": n_claims,
            "failed_claims": failed_claims or [], "error": error,
            "stages": stages or {}, "caller": caller, "user": user,
            "trace": trace or {},
        })

    def search(self, *, query: str, k: int, latency_ms: int,
               hits: int, caller: str = "cli", user: str = "local") -> None:
        """One search invocation row (retrieval-only calls)."""
        self._append("search", {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "kind": "search", "query": query, "k": k,
            "latency_ms": latency_ms, "hits": hits,
            "caller": caller, "user": user,
        })

    def sync(self, *, connector: str, duration_ms: int, discovered: int,
             normalized: int, committed: int, skipped: int,
             errors: list[str] | None = None,
             caller: str = "cli", user: str = "local") -> None:
        self._append("sync", {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "kind": "sync", "connector": connector, "duration_ms": duration_ms,
            "discovered": discovered, "normalized": normalized,
            "committed": committed, "skipped": skipped, "errors": errors or [],
            "caller": caller, "user": user,
        })


def audit_summary(corpus: Path, last: int = 200) -> str:
    """Compact human-readable summary of recent audit rows."""
    d = audit_log_dir(corpus)
    lines: list[str] = []
    totals: dict[str, int] = {}
    for name in ("answers", "search", "sync"):
        path = d / f"{name}.jsonl"
        if not path.exists():
            continue
        rows = [json.loads(l) for l in path.read_text(
            encoding="utf-8").splitlines() if l.strip()][-last:]
        if not rows:
            continue
        lines.append(f"{name}: {len(rows)} recent")
        for r in rows[-8:]:
            who = r.get("caller", "cli")
            totals[who] = totals.get(who, 0) + 1
            if r["kind"] == "answer":
                st = r.get("stages") or {}
                stages = (f"  retr={st.get('retrieve')}ms aug={st.get('augment')}ms "
                          f"gen={st.get('generate')}ms") if st else ""
                lines.append(
                    f"  {r['ts']} [{who}] answer {r['total_ms']}ms "
                    f"emitted={r['emitted']} claims={r['n_claims']} "
                    f"model={r['model']} q={r['query'][:40]!r}"
                    + stages
                    + (f" err={r['error'][:60]}" if r["error"] else ""))
            elif r["kind"] == "search":
                lines.append(
                    f"  {r['ts']} [{who}] search k={r['k']} {r['latency_ms']}ms "
                    f"hits={r['hits']} q={r['query'][:40]!r}")
            else:
                lines.append(
                    f"  {r['ts']} [{who}] sync {r['connector']} {r['duration_ms']}ms "
                    f"disc={r['discovered']} commit={r['committed']} "
                    f"skip={r['skipped']} errs={len(r['errors'])}")
    if totals:
        lines.append(
            "invocations by caller: " + ", ".join(
                f"{k}={v}" for k, v in sorted(totals.items())))
    return "\n".join(lines) or "(no audit rows yet)"
