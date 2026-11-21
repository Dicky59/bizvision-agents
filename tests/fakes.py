"""Test doubles shared by ingestion and retrieval tests (no network)."""

import hashlib
from collections.abc import AsyncIterator, Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.db.models import EMBEDDING_DIM
from core.embeddings.client import EmbeddingClient
from core.embeddings.provider import EmbeddingResult
from core.llm.types import LLMEvent, LLMResult, TextDelta, Usage, UsageUpdate


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


class FakeLLMProvider:
    """Scripted LLMProvider: each call pops the next script entry (a str, or a list of stream
    chunks). Records every call; streams count how many chunks were actually consumed."""

    name = "fake"

    def __init__(self, *scripts: str | list[str]) -> None:
        self.scripts: list[str | list[str]] = list(scripts)
        self.calls: list[dict[str, Any]] = []
        self.chunks_consumed = 0

    def _next(self, kind: str, **kw: Any) -> str | list[str]:
        self.calls.append({"kind": kind, **kw})
        if not self.scripts:
            raise AssertionError(f"unexpected extra LLM {kind} call")
        return self.scripts.pop(0)

    async def complete(self, **kw: Any) -> LLMResult:
        script = self._next("complete", **kw)
        text = script if isinstance(script, str) else "".join(script)
        return LLMResult(
            text=text, usage=Usage(input_tokens=100, output_tokens=20), model=kw["model"]
        )

    async def stream(self, **kw: Any) -> AsyncIterator[LLMEvent]:
        script = self._next("stream", **kw)
        chunks = [script] if isinstance(script, str) else script
        yield UsageUpdate(usage=Usage(input_tokens=100, output_tokens=1), final=False)
        for chunk in chunks:
            self.chunks_consumed += 1
            yield TextDelta(chunk)
        yield UsageUpdate(usage=Usage(input_tokens=100, output_tokens=30), final=True)


def tilted_vector(similarity: float) -> list[float]:
    """Unit vector whose cosine similarity to axis_vector(0) is exactly `similarity`."""
    v = axis_vector(0, similarity)
    v[9] = (1 - similarity**2) ** 0.5
    return v
