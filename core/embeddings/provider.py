from dataclasses import dataclass
from typing import Protocol


class EmbeddingError(Exception):
    pass


@dataclass(frozen=True)
class EmbeddingResult:
    vectors: list[list[float]]
    input_tokens: int
    model: str


class EmbeddingProvider(Protocol):
    name: str
    model: str

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        """Embed texts (any count; batched internally). Order of vectors matches texts."""
        ...
