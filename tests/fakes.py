"""Test doubles shared by ingestion and retrieval tests (no network)."""

import hashlib
from collections.abc import Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.db.models import EMBEDDING_DIM
from core.embeddings.client import EmbeddingClient
from core.embeddings.provider import EmbeddingResult


def hash_vector(text: str) -> list[float]:
    """Deterministic unit-ish vector: the same text always maps to the same one-hot axis."""
    axis = int(hashlib.sha256(text.encode()).hexdigest(), 16) % EMBEDDING_DIM
    v = [0.0] * EMBEDDING_DIM
    v[axis] = 1.0
    return v


class FakeEmbeddingProvider:
    """EmbeddingProvider that records every call; `vector_for` can be swapped per test."""

    name = "fake"
    model = "text-embedding-3-small"  # a priced model, so usage_events get a real cost

    def __init__(self, vector_for: Callable[[str], list[float]] = hash_vector) -> None:
        self.vector_for = vector_for
        self.calls: list[list[str]] = []
        self.fail_with: Exception | None = None

    @property
    def texts_embedded(self) -> int:
        return sum(len(c) for c in self.calls)

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        if self.fail_with is not None:
            raise self.fail_with
        self.calls.append(list(texts))
        return EmbeddingResult(
            vectors=[self.vector_for(t) for t in texts],
            input_tokens=sum(max(1, len(t) // 4) for t in texts),
            model=self.model,
        )


def make_embedder(
    sessionmaker: async_sessionmaker[AsyncSession],
    provider: FakeEmbeddingProvider | None = None,
) -> tuple[EmbeddingClient, FakeEmbeddingProvider]:
    provider = provider or FakeEmbeddingProvider()
    return EmbeddingClient(provider, sessionmaker, eur_per_usd=0.92), provider


_TOPIC_AXES: list[tuple[int, tuple[str, ...]]] = [
    (0, ("hinta", "hinnat", "maksaa", "paljon", "price", "much", "cost")),
    (1, ("palvelu", "service")),
    (2, ("yhteys", "yhteyttä", "contact")),
]


def axis_vector(axis: int, value: float = 1.0) -> list[float]:
    v = [0.0] * EMBEDDING_DIM
    v[axis] = value
    return v


def topic_vector(text: str) -> list[float]:
    """Keyword -> axis embedding: same topic = cosine 1.0, different topic = 0.0 (exact)."""
    lowered = text.lower()
    for axis, words in _TOPIC_AXES:
        if any(w in lowered for w in words):
            return axis_vector(axis)
    return axis_vector(100 + int(hashlib.sha256(text.encode()).hexdigest(), 16) % 1000)


def tilted_vector(similarity: float) -> list[float]:
    """Unit vector whose cosine similarity to axis_vector(0) is exactly `similarity`."""
    v = axis_vector(0, similarity)
    v[9] = (1 - similarity**2) ** 0.5
    return v
