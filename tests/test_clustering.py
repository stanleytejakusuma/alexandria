"""Offline tests for the two clustering passes (WORK-ORDER-phase2-clustering.md).

Uses a tiny BagEmbedder test double (token-overlap cosine, deterministic,
offline) so thresholds are exercisable: identical texts -> cosine 1.0,
paraphrase -> high overlap, distinct facts -> low. Production thresholds are
NOT asserted here -- they come from the real-embedder calibration run
(scripts/calibrate-clustering.py), per the work order's §1/§6.
"""

from __future__ import annotations

import hashlib
import math
import re

from alexandria.synthesis.clustering import (
    ChunkLike,
    dedup_actions,
    find_duplicate_clusters,
    find_topic_clusters,
)


class BagEmbedder:
    """Deterministic token-overlap embedder: each token maps to a fixed random
    unit vector (seeded by the token), a text embeds as its mean, normalized."""

    name_ = "bag-test"

    def __init__(self, dim: int = 64) -> None:
        self._dim = dim

    @property
    def name(self) -> str:
        return self.name_

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]

    def _token_vector(self, token: str) -> list[float]:
        seed = hashlib.sha256(token.encode()).digest()
        vals: list[float] = []
        counter = 0
        while len(vals) < self._dim:
            digest = hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
            for i in range(0, len(digest), 4):
                vals.append((int.from_bytes(digest[i:i + 4], "big") / 2**31) - 1.0)
                if len(vals) == self._dim:
                    break
            counter += 1
        norm = math.sqrt(sum(v * v for v in vals))
        return [v / norm for v in vals]

    def _embed_one(self, text: str) -> list[float]:
        tokens = re.findall(r"[a-z0-9]+", text.lower())
        vec = [0.0] * self._dim
        for t in tokens:
            tv = self._token_vector(t)
            for i in range(self._dim):
                vec[i] += tv[i]
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


EMB = BagEmbedder()


def _chunks(pairs: list[tuple[str, str]]) -> list[ChunkLike]:
    from alexandria.synthesis.gather import SourceChunk
    return [SourceChunk(chunk_id=cid, doc_id=cid, text=text) for cid, text in pairs]


TIGHT = 0.90
LOOSE = 0.35  # test threshold only; production comes from calibration


def test_dedup_finds_identical_and_paraphrase_pairs():
    chunks = _chunks([
        ("a1", "the deployment failed because the api key expired"),
        ("a2", "the deployment failed because the api key expired"),
        ("b1", "watering the garden on an unrelated schedule"),
    ])
    clusters = find_duplicate_clusters(chunks, threshold=TIGHT, embedder=EMB)
    assert len(clusters) == 1
    assert set(clusters[0].member_ids) == {"a1", "a2"}
    assert clusters[0].canonical_id in {"a1", "a2"}


def test_dedup_clusters_high_overlap_paraphrase():
    chunks = _chunks([
        ("p1", "the deployment failed because the api key expired at midnight"),
        ("p2", "the deployment failed because the api key expired just before"),
        ("q1", "the garden watering schedule covers the summer months"),
    ])
    clusters = find_duplicate_clusters(chunks, threshold=0.8, embedder=EMB)
    assert [c.member_ids for c in clusters] == [("p1", "p2")]


def test_dedup_threshold_respects_distinct_facts():
    chunks = _chunks([
        ("x1", "signer crashed while processing a large batch"),
        ("x2", "signer restarted after the crash"),
        ("y1", "garden watering schedule for summer"),
    ])
    assert find_duplicate_clusters(chunks, threshold=TIGHT, embedder=EMB) == []


def test_dedup_is_deterministic_and_pure():
    chunks = _chunks([
        ("p1", "the api key expired at midnight causing the failure"),
        ("p2", "the api key expired at midnight causing the failure"),
        ("q1", "something else entirely unrelated"),
    ])
    first = find_duplicate_clusters(chunks, threshold=TIGHT, embedder=EMB)
    second = find_duplicate_clusters(chunks, threshold=TIGHT, embedder=EMB)
    assert first == second
    assert [c.chunk_id for c in chunks] == ["p1", "p2", "q1"]


def test_dedup_canonical_is_most_complete_member():
    chunks = _chunks([
        ("short", "key expired"),
        ("long", "the deployment failed because the api key expired at midnight"),
    ])
    clusters = find_duplicate_clusters(chunks, threshold=0.55, embedder=EMB)
    assert len(clusters) == 1
    assert clusters[0].canonical_id == "long"


def test_topic_clusters_group_overlapping_and_keep_singletons():
    chunks = _chunks([
        ("t1", "signer keys were rotated after the compromise"),
        ("t2", "the compromise led to rotating signer keys"),
        ("t3", "the garden watering schedule for summer months"),
    ])
    clusters = find_topic_clusters(chunks, threshold=LOOSE, embedder=EMB)
    t1 = next(c for c in clusters if "t1" in c.member_ids)
    t2 = next(c for c in clusters if "t2" in c.member_ids)
    t3 = next(c for c in clusters if "t3" in c.member_ids)
    assert t1.cluster_id == t2.cluster_id
    assert t3.cluster_id != t1.cluster_id
    assert len(t3.member_ids) == 1


def test_topic_cluster_ids_are_deterministic_across_runs():
    chunks = _chunks([
        ("n1", "signer key management failures caused the outage"),
        ("n2", "the outage came from signer key management failures"),
        ("n3", "the vault bundle migration was completed"),
    ])
    a = find_topic_clusters(chunks, threshold=LOOSE, embedder=EMB)
    b = find_topic_clusters(chunks, threshold=LOOSE, embedder=EMB)
    assert [c.cluster_id for c in a] == [c.cluster_id for c in b]
    assert all(sorted(c.member_ids) == list(c.member_ids) for c in a)


def test_topic_representative_is_centroid_nearest():
    chunks = _chunks([
        ("r1", "signer keys were rotated after the compromise"),
        ("r2", "signer keys were rotated after the compromise and vault rekeyed"),
        ("r3", "watering the garden is scheduled for summer"),
    ])
    clusters = find_topic_clusters(chunks, threshold=0.45, embedder=EMB)
    reps = [c.representative_id for c in clusters if "r1" in c.member_ids]
    assert len(reps) == 1
    assert reps[0] in {"r1", "r2"}


def test_dedup_actions_assign_skip_to_duplicates_and_keep_canonical():
    chunks = _chunks([
        ("d1", "the vault bundle migration completed on schedule without data loss"),
        ("d2", "the vault bundle migration completed on schedule without data loss"),
        ("e1", "the fee attribution bug was fixed in the repair pass"),
    ])
    clusters = find_duplicate_clusters(chunks, threshold=TIGHT, embedder=EMB)
    actions = dedup_actions(clusters)
    assert actions["d2"].action == "skip"
    assert actions["d2"].canonical_id == "d1"
    assert actions["d1"].action == "store"
    assert "e1" not in actions


def test_empty_and_single_chunk_inputs_are_safe():
    assert find_duplicate_clusters([], threshold=TIGHT, embedder=EMB) == []
    assert find_topic_clusters([], threshold=LOOSE, embedder=EMB) == []
    single = _chunks([("s1", "just one chunk here")])
    assert find_duplicate_clusters(single, threshold=TIGHT, embedder=EMB) == []
    assert len(find_topic_clusters(single, threshold=LOOSE, embedder=EMB)) == 1
