#!/usr/bin/env python3
"""Calibrate the two clustering thresholds against REAL verified ground truth.

WORK-ORDER-phase2-clustering.md §1/§6: dedup thresholds are calibrated on the
hand-verified multi-candidate pairs (7 in golden-v1.jsonl, 8 in
contradiction-pairs-v1.jsonl) with constructed cross-entry negatives; topic
thresholds are calibrated on overlap with the 8 hand-built synthesis clusters.

Fully offline and local: MLX embedder + the existing 1.2GB embedding cache
(cache hits only for corpus chunks). No LLM spend.

Outputs a threshold curve for dedup (precision/recall with Wilson intervals,
small-n caveat) and a threshold curve for topic overlap (mean best-match
Jaccard on document membership), then prints the chosen default thresholds.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
from pathlib import Path

from alexandria.eval.contradiction_golden import load_contradiction_golden
from alexandria.eval.golden import load_golden
from alexandria.eval.synthesis_golden import load_synthesis_golden
from alexandria.index.chunker import chunk_document
from alexandria.index.embedder import CachedEmbedder, MLXEmbedder
from alexandria.synthesis.clustering import _UnionFind, find_topic_clusters

DEFAULT_CORPUS = Path.home() / "alexandria-corpus"
SEED = 7
NEGATIVES_PER_POSITIVE = 2


def _wilson(k: int, n: int) -> tuple[float, float]:
    """Wilson 95% interval for a proportion -- honest bounds on small n."""
    if n == 0:
        return 0.0, 0.0
    p = k / n
    z = 1.96
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def _doc_text(corpus: Path, doc_id: str) -> str | None:
    p = corpus / f"{doc_id}.md"
    if not p.exists():
        return None
    return p.read_text(encoding="utf-8")


def build_pairs(corpus: Path) -> tuple[list[tuple[str, str]], list[str]]:
    """Positive pairs: distinct members of every multi-candidate entry.
    Negatives: seeded random cross-entry pairs (different queries = verified
    distinct facts). Returns (pairs, labels) with labels 'pos'/'neg'.

    Pairs are DOC IDS; the caller chunks and scores them at chunk level
    (max over chunk pairs) -- the dedup pass operates on chunks, and
    full-doc-text cosine is a measurably weaker duplicate signal."""
    golden = load_golden(corpus / ".alexandria" / "golden" / "golden-v1.jsonl")
    contra = load_contradiction_golden(corpus / ".alexandria" / "golden" / "contradiction-pairs-v1.jsonl")

    entries = []
    for entry in list(golden) + list(contra):
        members = list(entry.must_retrieve) if hasattr(entry, "must_retrieve") else []
        if not members:
            members = list(entry.claim_a) + list(entry.claim_b)
        members = [m for m in dict.fromkeys(members) if m]
        if len(members) >= 2:
            entries.append(members)

    existing = []
    missing = 0
    for members in entries:
        kept = [m for m in members if (corpus / f"{m}.md").exists()]
        missing += len(members) - len(kept)
        if len(kept) >= 2:
            existing.append(kept)
    print(f"dedup calibration: {len(existing)} multi-member entries "
          f"({missing} member docs missing from corpus, skipped)")

    positives = [(m[0], m[1]) for m in existing]
    rng = random.Random(SEED)
    negatives = []
    for _ in range(len(positives) * NEGATIVES_PER_POSITIVE):
        a, b = rng.sample(existing, 2)
        negatives.append((a[0], b[0]))
    pairs = positives + negatives
    labels = ["pos"] * len(positives) + ["neg"] * len(negatives)
    return pairs, labels


def _chunk_texts(corpus: Path, doc_id: str) -> list[str]:
    text = (corpus / f"{doc_id}.md").read_text(encoding="utf-8", errors="replace")
    return [c.text for c in chunk_document(doc_id, text)]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def sweep_dedup(embedder, corpus: Path, pairs: list[tuple[str, str]], labels: list[str]) -> None:
    """Chunk-level scoring: each doc's chunks embedded once, pair score = max
    cosine over chunk pairs -- the actual semantics of the dedup pass."""
    docs = sorted({d for pair in pairs for d in pair})
    chunk_map: dict[str, list[str]] = {}
    for d in docs:
        chunk_map[d] = _chunk_texts(corpus, d)
    all_texts = [t for d in docs for t in chunk_map[d]]
    vectors = embedder.embed(all_texts)
    vec_map: dict[str, list[list[float]]] = {}
    offset = 0
    for d in docs:
        n = len(chunk_map[d])
        vec_map[d] = vectors[offset:offset + n]
        offset += n

    scores = []
    for a, b in pairs:
        best = 0.0
        for va in vec_map[a]:
            for vb in vec_map[b]:
                best = max(best, _cosine(va, vb))
        scores.append(best)

    print("\ndedup threshold sweep, chunk-level max-cosine "
          "(pos n=%d, neg n=%d):" % (labels.count("pos"), labels.count("neg")))
    print(f"{'t':>6} {'prec':>6} {'rec':>6} {'F1':>6}  precision 95% CI")
    best = (0.0, None)
    for t in [0.98, 0.96, 0.94, 0.92, 0.90, 0.88, 0.86, 0.84, 0.80, 0.75, 0.70, 0.60]:
        tp = sum(1 for s, l in zip(scores, labels) if s >= t and l == "pos")
        fp = sum(1 for s, l in zip(scores, labels) if s >= t and l == "neg")
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / labels.count("pos")
        f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
        lo, hi = _wilson(tp, tp + fp)
        marker = ""
        if f1 > best[0]:
            best = (f1, t)
            marker = "  <-- best F1"
        print(f"{t:>6.2f} {prec:>6.2f} {rec:>6.2f} {f1:>6.2f}  [{lo:.2f}, {hi:.2f}]{marker}")
    print(f"\nchosen dedup threshold: {best[1]:.2f} (F1 {best[0]:.2f}, "
          f"small-n caveat: {labels.count('pos')} positive pairs)")


def sweep_topic(embedder, corpus: Path, chunks) -> None:
    synth = load_synthesis_golden(corpus / ".alexandria" / "golden" / "synthesis-clusters-v1.jsonl")
    golden_docs = {e.id: set(e.source_docs) for e in synth}
    chunk_by_id = {c.chunk_id: c for c in chunks}

    # Embed ONCE and compute the >= 0.35 pair set ONCE; thresholds are then a
    # filter over the same pairs -- the full re-embed + re-matrix per threshold
    # measured ~15min for the first sweep, which was absurd for a filter.
    import numpy as np
    from alexandria.synthesis.clustering import _pairs_above
    vectors = np.asarray(embedder.embed([c.text for c in chunks]), dtype=np.float32)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms < 1e-8] = 1.0
    vectors = vectors / norms
    all_pairs = list(_pairs_above(vectors, 0.45))
    print(f"topic sweep: {len(chunks)} chunks, {len(all_pairs)} pairs >= 0.35")

    print(f"\ntopic overlap sweep over {len(chunks)} probe chunks (one per doc), "
          f"{len(synth)} known-good clusters")
    print(f"{'t':>6} {'meanJ':>6} {'recalled':>10}")
    best = (0.0, None)
    for t in [0.95, 0.92, 0.90, 0.88, 0.85, 0.82, 0.80, 0.78, 0.75, 0.70, 0.65, 0.60, 0.55, 0.50]:
        uf = _UnionFind(len(chunks))
        for i, j in all_pairs:
            if vectors[i] @ vectors[j] >= t:
                uf.union(i, j)
        clusters: dict[int, list[int]] = {}
        for i in range(len(chunks)):
            clusters.setdefault(uf.find(i), []).append(i)
        matched = []
        for docs in golden_docs.values():
            cbest = 0.0
            for members in clusters.values():
                cdocs = {chunk_by_id[chunks[m].chunk_id].doc_id for m in members}
                inter = len(cdocs & docs)
                union = len(cdocs | docs) or 1
                cbest = max(cbest, inter / union)
            matched.append(cbest)
        mean_j = sum(matched) / len(matched) if matched else 0.0
        recalled = sum(1 for m in matched if m > 0.0)
        marker = ""
        if mean_j > best[0]:
            best = (mean_j, t)
            marker = "  <-- best meanJ"
        print(f"{t:>6.2f} {mean_j:>6.2f} {recalled:>10}/{len(matched)}{marker}")
    print(f"\nchosen topic threshold: {best[1]:.2f} (mean best-match Jaccard {best[0]:.2f}, "
          f"{len(synth)} clusters)")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    p.add_argument("--limit-docs", type=int, default=None,
                   help="cap corpus scan for a quick probe (calibration is exact only with the full corpus)")
    args = p.parse_args(argv)
    corpus = args.corpus

    pairs, labels = build_pairs(corpus)
    if not pairs:
        print("no calibration pairs -- cannot calibrate", file=sys.stderr)
        return 1

    embedder = CachedEmbedder(
        MLXEmbedder(model="Qwen/Qwen3-Embedding-0.6B"),
        corpus / ".alexandria" / "cache" / "embeddings.sqlite")
    sweep_dedup(embedder, corpus, pairs, labels)

    # probe: ALL chunks of the golden source docs + a seeded sample of the
    # rest of the corpus. First-chunk-only probes collapsed at scale (meanJ
    # 0.03 vs 0.53 on a 600-doc probe) -- the real sweep clusters every
    # chunk, so the calibration must too.
    synth = load_synthesis_golden(corpus / ".alexandria" / "golden" / "synthesis-clusters-v1.jsonl")
    golden_doc_ids = {d for e in synth for d in e.source_docs}
    paths = sorted(corpus.rglob("*.md"))
    probe_paths = [p for p in paths if str(p.relative_to(corpus))[:-3] in golden_doc_ids]
    others = [p for p in paths if str(p.relative_to(corpus))[:-3] not in golden_doc_ids]
    rng = random.Random(SEED)
    if args.limit_docs:
        others = rng.sample(others, min(len(others), max(0, args.limit_docs - len(probe_paths))))
    probe_paths += others
    chunks = []
    for path in probe_paths:
        rel = str(path.relative_to(corpus))[:-3]
        doc = path.read_text(encoding="utf-8", errors="replace")
        chunks.extend(chunk_document(rel, doc))
    print(f"\ncorpus probe: {len(chunks)} chunks from {len(probe_paths)} docs "
          f"(golden sources included; {len(paths)} files total on disk)")
    sweep_topic(embedder, corpus, chunks)
    return 0


if __name__ == "__main__":
    sys.exit(main())
