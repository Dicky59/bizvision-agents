"""Reciprocal Rank Fusion."""

from collections.abc import Hashable, Sequence

RRF_K = 60


def fuse[K: Hashable](rankings: Sequence[Sequence[K]], k: int = RRF_K) -> dict[K, float]:
    """Score = sum over rankings of 1 / (k + rank), rank starting at 1. Higher is better."""
    scores: dict[K, float] = {}
    for ranking in rankings:
        for rank, key in enumerate(ranking, start=1):
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
    return scores
