"""Deterministic reciprocal-rank fusion and explicit layer boosting."""

from __future__ import annotations

from collections.abc import Mapping

__all__ = ["apply_layer_boost", "rrf"]


def rrf(rankings: list[list[str]], k: int = 60) -> dict[str, float]:
    """Fuse ranked ids with standard reciprocal-rank fusion.

    Duplicate ids within one ranking are ignored after their first position. Dict
    insertion order makes equal-score ties stable in the order sources supplied them.
    """
    if k < 0:
        raise ValueError("rrf k must be non-negative")
    scores: dict[str, float] = {}
    for ranking in rankings:
        seen: set[str] = set()
        for rank, chunk_id in enumerate(ranking, start=1):
            if chunk_id in seen:
                continue
            seen.add(chunk_id)
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank)
    return scores


def apply_layer_boost(scores: Mapping[str, float], layers: Mapping[str, str], *,
                      wiki_boost: float) -> dict[str, float]:
    """Boost wiki candidates while retaining every candidate from every layer."""
    if wiki_boost <= 0:
        raise ValueError("wiki boost must be positive")
    return {
        chunk_id: score * wiki_boost if layers.get(chunk_id) == "wiki" else score
        for chunk_id, score in scores.items()
    }
