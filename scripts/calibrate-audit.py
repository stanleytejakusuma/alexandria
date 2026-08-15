#!/usr/bin/env python3
"""Calibrate audit.py's LLM grader against RAGTruth's human-annotated hallucinations.

WHY THIS EXISTS: audit.py is already in production, already deciding whether
extracted notes are faithful to their source transcripts, and about to become the
entailment-checking mechanism phase 2's synthesis sweep is built on top of. Nobody
has ever measured whether the grader itself is accurate. If it has the well-documented
LLM-judge failure mode -- systematically lenient, missing SUBTLE conflicts far more
than evident ones -- every "faithful" verdict it has ever produced is inflated, and
phase 2 would inherit that blind spot silently.

RAGTruth (github.com/ParticleMedia/RAGTruth) is not a benchmark for RAG-answer
generation -- we have no generation layer to test. It is ground truth for
HALLUCINATION DETECTORS: human-annotated (source, response, hallucination-span)
triples. audit.py IS a hallucination detector. A faithfulness grader is a function on
(claim, evidence) pairs; human-labeled pairs from any corpus are valid calibration
data for it -- unlike a retrieval benchmark, where the corpus itself IS the benchmark
and public data can't substitute for ours.

GROUND-TRUTH MAPPING (a real judgment call, made explicit and tested):
  no labels                          -> "supported"      (strict)
  any Conflict-type label present    -> "fabricated"      (strict -- Conflict IS
                                          audit.py's own definition of fabricated:
                                          contradicts the source)
  only Baseless-type labels          -> "not_supported"   (LENIENT -- RAGTruth's
                                          "no basis in source" doesn't cleanly split
                                          into audit.py's unsupported-vs-fabricated;
                                          either verdict counts as correctly caught,
                                          only "supported" is a miss)

STRATIFIED, not uniform random: Subtle Conflict is 15 of 2,675 test-split, quality=good
items
(0.6%). A uniform sample of a few hundred would likely draw zero, and Subtle-vs-Evident
accuracy is the single most diagnostic split this calibration can report -- exactly the
gap where LLM judges are known to fail silently. Take every rare item that exists.

RAGTruth's data is not fetched into this repo (public but third-party; a git clone
into a gitignored cache dir keeps the engine repo from bloating with someone else's
dataset). Run with --fetch once, then reuse the cache.
"""

from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from alexandria.audit import grade_note  # noqa: E402
from alexandria.llm import LLMClient, LLMError  # noqa: E402

REPO_URL = "https://github.com/ParticleMedia/RAGTruth"
CACHE = Path.home() / ".cache" / "alexandria" / "ragtruth"
CONFLICT = {"Evident Conflict", "Subtle Conflict"}
BASELESS = {"Evident Baseless Info", "Subtle Baseless Info"}

# Default stratified sampling plan. Every Subtle Conflict item that exists (rare, most
# diagnostic); a meaningful slice of everything else; clean gets the largest single
# bucket because false-positive rate on genuinely faithful material is what most
# resembles our own real corpus (mostly faithful notes, not mostly hallucinated ones).
DEFAULT_PLAN = {
    "Subtle Conflict": 16,
    "Subtle Baseless Info": 40,
    "Evident Conflict": 60,
    "Evident Baseless Info": 60,
    "clean": 120,
}


def source_text(source_info) -> str:
    """grade_note requires a plain-text transcript. Only the Summary task's source_info
    is already a string; QA's is {question, passages} and Data2txt's is structured
    data -- both dicts, discovered by running the real pilot sample rather than
    assumed from the one (Summary) example inspected first. Rendered as readable
    text, not raw json.dumps, so the grader reads it the way a human would."""
    if isinstance(source_info, str):
        return source_info
    if isinstance(source_info, dict):
        if "question" in source_info and "passages" in source_info:
            return f"Question: {source_info['question']}\n\n{source_info['passages']}"
        return "\n".join(f"{k}: {v}" for k, v in source_info.items())
    return str(source_info)


def category(response: dict) -> str:
    """File a response under its single most diagnostic label: conflict outranks
    baseless (rarer, more clearly wrong), subtle outranks evident (the harder case)."""
    types = {lb["label_type"] for lb in response.get("labels") or []}
    if not types:
        return "clean"
    for preferred in ("Subtle Conflict", "Evident Conflict",
                      "Subtle Baseless Info", "Evident Baseless Info"):
        if preferred in types:
            return preferred
    return next(iter(types))


def ground_truth(response: dict) -> str:
    types = {lb["label_type"] for lb in response.get("labels") or []}
    if not types:
        return "supported"
    if types & CONFLICT:
        return "fabricated"
    return "not_supported"


def is_correct(expected: str, verdict: str) -> bool:
    if expected == "not_supported":
        return verdict in {"unsupported", "fabricated"}  # lenient, see module docstring
    return verdict == expected


def stratified_sample(items: list[dict], plan: dict[str, int], seed: int = 0) -> list[dict]:
    buckets: dict[str, list[dict]] = {}
    for item in items:
        buckets.setdefault(category(item), []).append(item)
    rng = random.Random(seed)
    out: list[dict] = []
    for cat, n in plan.items():
        pool = buckets.get(cat, [])
        pool_sorted = sorted(pool, key=lambda x: x.get("id", ""))  # deterministic order
        out.extend(rng.sample(pool_sorted, min(n, len(pool_sorted))))
    return out


def ensure_data() -> tuple[Path, Path]:
    if not CACHE.exists():
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--depth", "1", REPO_URL, str(CACHE)], check=True)
    return CACHE / "dataset" / "source_info.jsonl", CACHE / "dataset" / "response.jsonl"


def load(source_path: Path, response_path: Path) -> tuple[dict, list[dict]]:
    sources = {d["source_id"]: d for d in (json.loads(l) for l in source_path.open())}
    responses = [json.loads(l) for l in response_path.open()
                 if json.loads(l).get("split") == "test"
                 and json.loads(l).get("quality") == "good"]
    return sources, responses


def run(args) -> int:
    source_path, response_path = ensure_data()
    sources, responses = load(source_path, response_path)
    print(f"loaded {len(responses)} test-split, quality=good responses", file=sys.stderr)

    sample = stratified_sample(responses, DEFAULT_PLAN, seed=args.seed)
    print(f"stratified sample: {len(sample)} items -- "
          f"{Counter(category(r) for r in sample)}", file=sys.stderr)

    grader = LLMClient(model=args.model, timeout=180, max_retries=4,
                       base_delay=3.0, min_interval=0.5)

    results: list[tuple[dict, str, str | None, str | None]] = []  # (item, expected, verdict, error)

    def grade(item: dict) -> tuple[dict, str, str | None, str | None]:
        src = sources.get(item["source_id"], {})
        transcript = source_text(src.get("source_info", ""))
        try:
            v = grade_note(grader, transcript, f"RAGTruth {item.get('task_type', '')} response",
                           item.get("response", ""), item["id"])
            return item, ground_truth(item), v.verdict, None
        except Exception as exc:  # one malformed item must not crash the batch
            return item, ground_truth(item), None, f"{type(exc).__name__}: {str(exc)[:180]}"

    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(grade, item): item for item in sample}
        for fut in as_completed(futures):
            results.append(fut.result())
            done += 1
            if done % 20 == 0 or done == len(sample):
                print(f"  {done}/{len(sample)}", file=sys.stderr, flush=True)

    report(results)
    return 0


def report(results: list[tuple[dict, str, str | None, str | None]]) -> None:
    errors = [r for r in results if r[3] is not None]
    graded = [r for r in results if r[3] is None]

    print("\n" + "=" * 66)
    print("audit.py calibration against RAGTruth (real ground truth)")
    print("=" * 66)
    print(f"  graded: {len(graded)}   errors: {len(errors)}")

    by_cat: dict[str, list[bool]] = {}
    for item, expected, verdict, _ in graded:
        by_cat.setdefault(category(item), []).append(is_correct(expected, verdict))

    print(f"\n  {'category':<24} {'n':>5} {'accuracy':>10}")
    print("  " + "-" * 42)
    for cat in ("clean", "Evident Conflict", "Subtle Conflict",
               "Evident Baseless Info", "Subtle Baseless Info"):
        bools = by_cat.get(cat, [])
        if not bools:
            continue
        acc = sum(bools) / len(bools)
        print(f"  {cat:<24} {len(bools):>5} {acc*100:>9.1f}%")

    for pair in (("Evident Conflict", "Subtle Conflict"),
                ("Evident Baseless Info", "Subtle Baseless Info")):
        ev, sub = by_cat.get(pair[0]), by_cat.get(pair[1])
        if ev and sub:
            gap = (sum(ev) / len(ev) - sum(sub) / len(sub)) * 100
            flag = "  <-- grader weaker on the SUBTLE case" if gap > 5 else ""
            print(f"\n  {pair[0]} vs {pair[1]} accuracy gap: {gap:+.1f}pp{flag}")

    false_positive = 1 - (sum(by_cat.get("clean", [True])) / max(len(by_cat.get("clean", [])), 1))
    print(f"\n  false-positive rate on genuinely clean material: {false_positive*100:.1f}%")
    if errors:
        print(f"\n  {len(errors)} grader error(s), first 5:")
        for item, _, _, err in errors[:5]:
            print(f"    {item['id']}: {err}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--model", default="deepseek-v4-flash")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--seed", type=int, default=0)
    return p


if __name__ == "__main__":
    sys.exit(run(build_parser().parse_args()))
