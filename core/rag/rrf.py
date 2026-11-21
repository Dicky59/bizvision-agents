"""Reciprocal Rank Fusion."""

from collections.abc import Hashable, Sequence

RRF_K = 60


def fuse[K: Hashable](
    rankings: Sequence[Sequence[K]],
    k: int = RRF_K,
    weights: Sequence[float] | None = None,
) -> dict[K, float]:
    """Score = sum over rankings of weight / (k + rank), rank starting at 1 (weight defaults to
    1 for every ranking). Higher is better."""
    if weights is not None and len(weights) != len(rankings):
        raise ValueError("weights must match rankings")
    scores: dict[K, float] = {}
    for i, ranking in enumerate(rankings):
        w = 1.0 if weights is None else weights[i]
        for rank, key in enumerate(ranking, start=1):
            scores[key] = scores.get(key, 0.0) + w / (k + rank)
    return scores
