#!/usr/bin/env python3
"""Phase-3 contest harness: blinded recall@5, Alexandria vs incumbent.

Pre-registered protocol: docs/SPEC-phase3-harness.md (revised 2026-08-07
against the Red review conditions). Stages are checkpointed under --out:
  01-<sys>.jsonl   raw retrieval per query (system A: alexandria, B: incumbent)
  02-blind.jsonl   per-query union, shuffled (fixed seed), ids redacted
  03-graded.jsonl  two-grader relevance verdicts (batched per query)
  04-report.json   recall@5 per system, Wilson CI, strata, verdict, manifest

Verdict rules (spec §1.2/§3): PASS iff alexandria recall > incumbent recall
(tie = |Δ| <= 1 relevant doc -> one pre-registered re-run, then FAIL), floor
>= 0.60, and grader disagreement <= 0.20 of queries. INVALID on manifest
mismatch (--queries file changed), spend over budget, or any rerun not
pre-registered. Any infra failure (gateway timeout, crash) aborts with
INVALID unless --resume re-runs cleanly and the failure is logged.

Cost guard: bounded runs; each grader call grades a whole query's union
(2 LLM calls per query, ~N*2 calls per run).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

DISAGREEMENT_CAP = 0.20
FLOOR = 0.60


# ---------------------------------------------------------------- systems

def alexandria_retrieve(corpus: Path, query: str, k: int) -> list[dict[str, Any]]:
    from alexandria.config import load_config
    from alexandria.retrieval.search import SearchEngine, SearchConfig
    from alexandria.index.bm25 import BM25Index
    from alexandria.index.embedder import CachedEmbedder, HashEmbedder, MLXEmbedder, LocalEmbedder
    from alexandria.index.store import VectorStore
    from alexandria.retrieval.rerank import CrossEncoderReranker
    from alexandria.monitor import QueryLogger

    config = load_config(corpus_override=corpus)
    if config.embed_provider == "hash":
        provider = HashEmbedder()
    elif config.embed_provider == "mlx":
        provider = MLXEmbedder(batch_size=config.embed_batch_size)
    else:
        provider = LocalEmbedder(config.embed_model, config.embed_batch_size)
    embedder = CachedEmbedder(provider, corpus / ".alexandria" / "cache" / "embeddings.sqlite",
                              progress_every=config.index_progress_every)
    engine = SearchEngine(
        embedder,
        VectorStore(corpus / ".alexandria" / "index"),
        BM25Index(corpus / ".alexandria" / "index" / "fts.sqlite"),
        CrossEncoderReranker(config.rerank_model),
        SearchConfig(prefetch=config.rerank_prefetch, top_k=config.rerank_top_k,
                     wiki_boost=config.wiki_boost, rrf_k=config.rrf_k),
        QueryLogger(corpus / ".alexandria" / "queries.sqlite"),
    )
    # dedupe to parent-doc unit (spec: result = parent document)
    seen: set[str] = set()
    docs: list[dict[str, Any]] = []
    for r in engine.search(query, k=k * 4):
        if r.doc_id in seen:
            continue
        seen.add(r.doc_id)
        docs.append({"doc_id": r.doc_id, "text": r.text, "rank": len(docs) + 1})
        if len(docs) >= k:
            break
    return docs


def incumbent_retrieve(query: str, k: int) -> list[dict[str, Any]]:
    shim = REPO / "scripts" / "incumbent-memory.mts"
    if not os.environ.get("INCUMBENT_MEMORY_PKG") or not os.environ.get("INCUMBENT_MEMORY_DIR"):
        raise RuntimeError(
            "set INCUMBENT_MEMORY_PKG (installed incumbent package dir) and "
            "INCUMBENT_MEMORY_DIR (its store dir) to run the incumbent side")
    run = subprocess.run(
        ["npx", "--yes", "tsx", str(shim), "--query", query, "--k", str(k)],
        capture_output=True, text=True, timeout=180,
    )
    if run.returncode != 0:
        raise RuntimeError(f"incumbent shim failed: {run.stderr[:500]}")
    out = []
    for line in run.stdout.splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        out.append({"doc_id": f"inc-{row['id']}", "text": row["content"],
                    "rank": len(out) + 1})
    return out


# ---------------------------------------------------------------- grading

GRADER_SYSTEM = ("You are a retrieval relevance grader. Judge each result: "
                "is it RELEVANT to the query (does it contain the answer/fact "
                "the query demands)? Partial support counts when it answers the "
                "query's operative verb; agnostic/opinion answers are NOT "
                "relevant. Reply with ONLY a JSON object mapping each result "
                "number to yes or no, e.g. {\"1\": \"yes\", \"2\": \"no\"}.")

GRADER_PROMPT = ("QUERY: {query}\n\n"
                 "RESULTS:\n{results}")


def _parse_verdicts(text: str) -> dict[str, bool]:
    start = text.find("{")
    end = text.rfind("}") + 1
    if start < 0 or end <= start:
        raise ValueError(f"grader returned non-JSON: {text[:200]}")
    raw = json.loads(text[start:end])
    return {str(k): str(v).strip().lower().startswith("y") for k, v in raw.items()}


def grade_query(client, query: str, results: list[dict[str, Any]], *, retries=2):
    body = "\n".join(f"{i + 1}. {r['text'][:1200]}" for i, r in enumerate(results))
    prompt = GRADER_PROMPT.format(query=query, results=body)
    last = None
    for _ in range(retries + 1):
        try:
            # temperature=0.1: the gateway's llm.py guard refuses fast-tier
            # models (sol/terra) at temperature=0 (cross-contamination class).
            text = client.complete(GRADER_SYSTEM, prompt, temperature=0.1)
            verdicts = _parse_verdicts(text)
            if set(verdicts) == {str(i + 1) for i in range(len(results))}:
                return verdicts
            last = ValueError(f"missing verdicts: {sorted(verdicts)}")
        except Exception as e:  # noqa: BLE001
            last = e
    raise RuntimeError(f"grading failed for query {query!r}: {last}")


# ---------------------------------------------------------------- scoring

def wilson_ci(hits: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = hits / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - h) / d, (c + h) / d)


def score_run(graded: list[dict[str, Any]]) -> dict[str, Any]:
    a_hit = a_rel = i_hit = i_rel = 0
    disagree_queries = 0
    rows = []
    for g in graded:
        relevant = {r["num"] for r in g["results"] if r["relevant"]}
        a_relevant = {r["num"] for r in g["results"] if r["system"] == "A"} & relevant
        i_relevant = {r["num"] for r in g["results"] if r["system"] == "B"} & relevant
        # per-grader disagreement on this query
        va = {r["num"]: r["grader_a"] for r in g["results"]}
        vb = {r["num"]: r["grader_b"] for r in g["results"]}
        if any(va[n] != vb[n] for n in va if n in vb):
            disagree_queries += 1
        n_rel = len(relevant)
        if n_rel == 0:
            rows.append({"query": g["query"], "stratum": g["stratum"],
                         "relevant": 0, "alexandria": None, "incumbent": None})
            continue
        ar = len(a_relevant) / n_rel
        ir = len(i_relevant) / n_rel
        a_hit += len(a_relevant); a_rel += n_rel
        i_hit += len(i_relevant); i_rel += n_rel
        rows.append({"query": g["query"], "stratum": g["stratum"], "relevant": n_rel,
                     "alexandria": round(ar, 4), "incumbent": round(ir, 4)})

    a_recall = a_hit / a_rel if a_rel else 0.0
    i_recall = i_hit / i_rel if i_rel else 0.0
    disagreement = disagree_queries / len(graded) if graded else 0.0
    diff = a_recall - i_recall
    tie = abs(diff) <= (1 / max(a_rel, i_rel, 1))
    floor_ok = a_recall >= FLOOR
    cap_ok = disagreement <= DISAGREEMENT_CAP
    # spec §3: disagreement over the cap is INVALID (run discarded, no
    # verdict), taking precedence over PASS/FAIL.
    if not cap_ok:
        verdict = "INVALID"
    else:
        verdict = "PASS" if (a_recall > i_recall and not tie and floor_ok) else "FAIL"

    strata: dict[str, dict[str, float]] = {}
    for row in rows:
        if row["alexandria"] is None:
            continue
        s = strata.setdefault(row["stratum"], {"n": 0, "a_sum": 0.0, "i_sum": 0.0})
        s["n"] += 1
        s["a_sum"] += row["alexandria"]
        s["i_sum"] += row["incumbent"]
    per_stratum = {k: {"n": v["n"], "alexandria": round(v["a_sum"] / v["n"], 4),
                       "incumbent": round(v["i_sum"] / v["n"], 4)}
                   for k, v in strata.items()}

    return {
        "verdict": verdict,
        "alexandria_recall": round(a_recall, 4),
        "incumbent_recall": round(i_recall, 4),
        "diff": round(diff, 4),
        "tie": tie,
        "floor_ok": floor_ok,
        "disagreement": round(disagreement, 4),
        "disagreement_cap_ok": cap_ok,
        "wilson_ci_alexandria": [round(x, 4) for x in wilson_ci(a_hit, a_rel)],
        "wilson_ci_incumbent": [round(x, 4) for x in wilson_ci(i_hit, i_rel)],
        "relevant_total": a_rel,
        "per_stratum": per_stratum,
        "per_query": rows,
    }


# ---------------------------------------------------------------- driver

def manifest(queries_path: Path, args) -> dict[str, Any]:
    digest = hashlib.sha256(queries_path.read_bytes()).hexdigest()[:16]
    corpus_sha = ""
    corpus = Path(args.corpus)
    if (corpus / ".git").exists():
        try:
            corpus_sha = subprocess.run(
                ["git", "-C", str(corpus), "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=10).stdout.strip()
        except Exception:  # noqa: BLE001
            corpus_sha = "unavailable"
    return {
        "queries_sha256": digest,
        "queries_file": str(queries_path),
        "seed": args.seed,
        "k": args.k,
        "corpus": str(corpus),
        "corpus_git_sha": corpus_sha,
        "grader_a": args.grader_a_model,
        "grader_b": args.grader_b_model,
        "spec": "docs/SPEC-phase3-harness.md (revised 2026-08-07)",
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--queries", required=True, type=Path, help="frozen query set JSON")
    p.add_argument("--out", required=True, type=Path, help="run output dir")
    p.add_argument("--corpus", default=os.environ.get("ALEXANDRIA_CORPUS", "~/alexandria-corpus"))
    p.add_argument("--k", type=int, default=5)
    p.add_argument("--seed", type=int, default=20260807)
    p.add_argument("--base-url", default=os.environ.get("ALEXANDRIA_BASE_URL", "http://127.0.0.1:20128/v1"),
                    help="gateway base URL (set ALEXANDRIA_BASE_URL to the remote gateway at run time)")
    p.add_argument("--api-key-env", default="ALEXANDRIA_AXIOM_KEY")
    p.add_argument("--grader-a-model", default="openrouter/anthropic/claude-sonnet-5")
    p.add_argument("--grader-b-model", default="deepseek-v4-pro")
    p.add_argument("--dry-run", action="store_true", help="scripted graders, no spend")
    p.add_argument("--resume", action="store_true", help="reuse completed stages in --out")
    args = p.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    if not args.resume:
        for f in out.glob("0*-*.jsonl"):
            f.unlink()
        (out / "04-report.json").unlink(missing_ok=True)

    queries = json.loads(args.queries.read_text())
    assert isinstance(queries, list) and all("query" in q and "stratum" in q for q in queries), \
        "query set must be a list of {query, stratum}"

    man = manifest(args.queries, args)
    (out / "manifest.json").write_text(json.dumps(man, indent=2))

    # stage 1: retrieval
    a_path, i_path = out / "01-alexandria.jsonl", out / "01-incumbent.jsonl"
    if not (args.resume and a_path.exists() and i_path.exists()):
        with a_path.open("w") as fa, i_path.open("w") as fi:
            for q in queries:
                fa.write(json.dumps({"query": q["query"], "stratum": q["stratum"],
                                     "results": alexandria_retrieve(Path(args.corpus).expanduser(), q["query"], args.k)}) + "\n")
                fa.flush()
                fi.write(json.dumps({"query": q["query"], "stratum": q["stratum"],
                                     "results": incumbent_retrieve(q["query"], args.k)}) + "\n")
                fi.flush()
                print(f"retrieved {q['query'][:50]!r}", flush=True)

    # stage 2: blind union (fixed-seed shuffle, ids redacted) — spec §1.2:
    # the union of both systems' top-5 docs per query, presented together
    blind_path = out / "02-blind.jsonl"
    if not (args.resume and blind_path.exists()):
        rng = random.Random(args.seed)
        with blind_path.open("w") as fb:
            with a_path.open() as fa, i_path.open() as fi:
                for la, li in zip(fa, fi):
                    if not la.strip() or not li.strip():
                        continue
                    ra, ri = json.loads(la), json.loads(li)
                    assert ra["query"] == ri["query"], "retrieval files misaligned"
                    results = []
                    for r in ra["results"]:
                        results.append({"num": len(results) + 1, "system": "A",
                                        "text": r["text"][:1200].replace(chr(10), " ")})
                    for r in ri["results"]:
                        results.append({"num": len(results) + 1, "system": "B",
                                        "text": r["text"][:1200].replace(chr(10), " ")})
                    rng.shuffle(results)
                    fb.write(json.dumps({"query": ra["query"], "stratum": ra["stratum"],
                                         "results": results}) + "\n")
        print("blinded", flush=True)

    # stage 3: grading
    graded_path = out / "03-graded.jsonl"
    if not (args.resume and graded_path.exists() and graded_path.stat().st_size > 0):
        from alexandria.llm import LLMClient
        if args.dry_run:
            class Scripted:
                def complete(self, system, user, temperature=0.0):  # relevant iff query's first word appears in the result text
                    q = user.split("QUERY: ", 1)[1].split("\n", 1)[0].lower()
                    keyword = q.split()[0]
                    body = user.split("RESULTS:\n", 1)[1]
                    import re
                    hits = []
                    for line in body.splitlines():
                        m = re.match(r"^(\d+)\.\s+(.*)$", line)
                        if m:
                            hits.append((m.group(1), "yes" if keyword in m.group(2).lower() else "no"))
                    return "{" + ", ".join(f'"{n}": "{v}"' for n, v in hits) + "}"
            grader_a = grader_b = Scripted()
        else:
            grader_a = LLMClient(model=args.grader_a_model, base_url=args.base_url,
                                 api_key_env=args.api_key_env)
            grader_b = LLMClient(model=args.grader_b_model, base_url=args.base_url,
                                 api_key_env=args.api_key_env)
        with blind_path.open() as fb, graded_path.open("w") as fg:
            for line in fb:
                row = json.loads(line)
                va = grade_query(grader_a, row["query"], row["results"])
                vb = grade_query(grader_b, row["query"], row["results"])
                results = []
                for r in row["results"]:
                    n = str(r["num"])
                    results.append({"num": r["num"], "system": r["system"],
                                    "grader_a": va.get(n, False), "grader_b": vb.get(n, False),
                                    "relevant": va.get(n, False) and vb.get(n, False)})
                fg.write(json.dumps({"query": row["query"], "stratum": row["stratum"],
                                     "results": results}) + "\n")
                fg.flush()
        print("graded", flush=True)

    # stage 4: score + report
    graded = [json.loads(l) for l in graded_path.read_text().splitlines() if l.strip()]
    report = score_run(graded)
    report["manifest"] = man
    report["cost_estimate_usd"] = round(2 * len(graded) * 0.10, 2)  # ~2 LLM calls/query
    (out / "04-report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps({k: report[k] for k in ("verdict", "alexandria_recall",
          "incumbent_recall", "diff", "tie", "floor_ok", "disagreement")}, indent=2))
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
