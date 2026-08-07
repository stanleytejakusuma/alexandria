"""Two clustering passes over a fixed corpus snapshot, one embedding pipeline.

Implements `docs/DECISIONS-phase2-execution-model.md` §"Two distinct
clustering passes": the same embeddings, two independently-tuned thresholds.

- `find_duplicate_clusters` — tight threshold; chunks restating the *same
  fact* (index-time near-duplicate dedup, adopted-but-unbuilt).
- `find_topic_clusters` — loose threshold; chunks on the *same topic*,
  often causally/temporally linked (the full-sweep enumerator's units).

Both are pure functions: (chunks, threshold, embedder) -> clusters. No
writes, no hidden state, no execution-order assumptions -- the full-sweep
fold calls them serially and repeatedly.

Scale strategy (no new dependencies): the full pairwise cosine matrix for
the real corpus (~33k chunks) is computed exactly in row batches with numpy
(already a transitive dependency via lancedb), and the `>= threshold` pairs
feed an incremental union-find as they stream -- peak memory is one batch
row, no LSH recall loss, no blocking heuristics. Determinism: identical
inputs + same numpy yields identical output; all tie-breaks sort by
(chunk_id, text).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from ..index.embedder import Embedder

__all__ = [
    "ChunkLike",
    "DEDUP_THRESHOLD",
    "DedupAction",
    "DuplicateCluster",
    "TOPIC_THRESHOLD",
    "TopicCluster",
    "dedup_actions",
    "find_duplicate_clusters",
    "find_topic_clusters",
]


# Calibrated defaults (scripts/calibrate-clustering.py, 2026-08-07, real
# embedder + real hand-verified ground truth):
#   dedup 0.75 -- precision 1.00 / recall 0.70 on the 20 verified positive
#     pairs (chunk-level max-cosine). Precision-first on purpose: a false
#     duplicate merge collapses distinct facts into one canonical (data loss),
#     while a missed duplicate is caught by the sweep's cross-page redundancy
#     layer and the dedup action space's `skip` is never destructive.
#   topic 0.88 -- full-corpus calibration (66,770 chunks, index-model
#     embeddings): mean best-match Jaccard 0.27 vs the 8 hand-built golden
#     clusters. First-chunk-only probes were rejected as a calibration
#     artifact (0.53 at 0.75 on 600 docs collapsed to 0.03 at scale); the
#     all-chunks sweep moves the optimum up to 0.88.
DEDUP_THRESHOLD = 0.75
TOPIC_THRESHOLD = 0.88


class ChunkLike(Protocol):
    chunk_id: str
    text: str


@dataclass(frozen=True)
class DuplicateCluster:
    member_ids: tuple[str, ...]  # sorted, deterministic
    canonical_id: str  # most complete member (longest text, earliest id tie-break)
    representative_text: str


@dataclass(frozen=True)
class TopicCluster:
    cluster_id: str  # md5 of the sorted member ids -- stable for a fixed member set
    member_ids: tuple[str, ...]  # sorted
    representative_id: str  # member nearest the centroid
    representative_text: str


@dataclass(frozen=True)
class DedupAction:
    action: str  # "store" | "skip" (update/merge are corpus-mutation-layer decisions)
    canonical_id: str | None
    reason: str


_BATCH = 512


def _vectors(chunks: list[ChunkLike], embedder: Embedder) -> np.ndarray:
    v = np.asarray(embedder.embed([c.text for c in chunks]), dtype=np.float32)
    norms = np.linalg.norm(v, axis=1, keepdims=True)
    norms[norms < 1e-8] = 1.0  # zero vectors become null rows (never match)
    return v / norms


def _pairs_above(vectors: np.ndarray, threshold: float):
    """Yield (i, j) with i < j and cosine(i, j) >= threshold, streaming in
    row batches so peak memory is one batch row of the full matrix."""
    n = vectors.shape[0]
    for i0 in range(0, n, _BATCH):
        block = vectors[i0 : i0 + _BATCH]
        sims = block @ vectors.T  # (batch, n)
        for r in range(block.shape[0]):
            i = i0 + r
            row = sims[r]
            for pos in np.flatnonzero(row[i + 1 :] >= threshold):
                yield i, i + 1 + int(pos)


class _UnionFind:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))
        self.size = [1] * n

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]  # path halving
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self.size[ra] < self.size[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        self.size[ra] += self.size[rb]


def _text_of(chunks: list[ChunkLike], i: int) -> str:
    return chunks[i].text


def find_duplicate_clusters(
    chunks: list[ChunkLike], *, threshold: float = DEDUP_THRESHOLD, embedder: Embedder
) -> list[DuplicateCluster]:
    """Tight-threshold pass: groups chunks restating the same fact. Returns
    only multi-member clusters (singletons are untouched by dedup)."""
    if not chunks:
        return []
    vectors = _vectors(chunks, embedder)
    uf = _UnionFind(len(chunks))
    for i, j in _pairs_above(vectors, threshold):
        uf.union(i, j)
    clusters: dict[int, list[int]] = {}
    for i in range(len(chunks)):
        clusters.setdefault(uf.find(i), []).append(i)
    out = []
    for root, members in clusters.items():
        if len(members) < 2:
            continue
        members.sort(key=lambda i: (chunks[i].chunk_id, chunks[i].text))
        canonical = max(members, key=lambda i: (len(chunks[i].text), -members.index(i)))
        out.append(DuplicateCluster(
            member_ids=tuple(chunks[i].chunk_id for i in members),
            canonical_id=chunks[canonical].chunk_id,
            representative_text=chunks[canonical].text,
        ))
    out.sort(key=lambda c: c.member_ids[0])
    return out


def find_topic_clusters(
    chunks: list[ChunkLike], *, threshold: float = TOPIC_THRESHOLD, embedder: Embedder
) -> list[TopicCluster]:
    """Loose-threshold pass: groups chunks on the same topic. Returns the FULL
    partition including singletons -- the sweep's enumeration unit, where a
    singleton is a document that would be excluded with `no_cluster_match`."""
    if not chunks:
        return []
    vectors = _vectors(chunks, embedder)
    uf = _UnionFind(len(chunks))
    for i, j in _pairs_above(vectors, threshold):
        uf.union(i, j)
    clusters: dict[int, list[int]] = {}
    for i in range(len(chunks)):
        clusters.setdefault(uf.find(i), []).append(i)
    out = []
    for members in clusters.values():
        members.sort(key=lambda i: (chunks[i].chunk_id, chunks[i].text))
        ids = tuple(chunks[i].chunk_id for i in members)
        centroid = vectors[members].mean(axis=0)
        centroid = centroid / (np.linalg.norm(centroid) + 1e-8)
        rep = max(members, key=lambda i: float(vectors[i] @ centroid))
        out.append(TopicCluster(
            cluster_id="topic-" + hashlib.md5("|".join(ids).encode()).hexdigest()[:10],
            member_ids=ids,
            representative_id=chunks[rep].chunk_id,
            representative_text=chunks[rep].text,
        ))
    out.sort(key=lambda c: c.cluster_id)
    return out


def dedup_actions(clusters: list[DuplicateCluster]) -> dict[str, DedupAction]:
    """Action space per DECISIONS-multi-actor-posture.md: store | skip.
    The canonical member is stored; every other member is skipped as
    `duplicate_of:<canonical>` (update/merge toward a more complete member are
    corpus-mutation-layer decisions, out of scope for the clustering pass)."""
    actions: dict[str, DedupAction] = {}
    for c in clusters:
        for mid in c.member_ids:
            if mid == c.canonical_id:
                actions[mid] = DedupAction("store", None, "canonical member")
            else:
                actions[mid] = DedupAction(
                    "skip", c.canonical_id, f"duplicate_of:{c.canonical_id}"
                )
    return actions
