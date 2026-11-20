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
