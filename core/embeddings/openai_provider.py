from typing import Any

import httpx2
import openai

from core.db.models import EMBEDDING_DIM
from core.embeddings.provider import EmbeddingError, EmbeddingResult
from core.retry import retry_async

BATCH_SIZE = 100
DEFAULT_MODEL = "text-embedding-3-small"
CONNECTION_ERRORS = (openai.APIConnectionError, openai.APITimeoutError)


class OpenAIEmbeddingProvider:
    name = "openai"

    def __init__(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_MODEL,
        dimensions: int = EMBEDDING_DIM,
        timeout_s: float = 60.0,
        http_client: httpx2.AsyncClient | None = None,
        sleep: Any = None,
    ) -> None:
        self.model = model
        self._dimensions = dimensions
        # max_retries=0: retry policy is ours (core.retry), shared with the LLM provider.
        self._client = openai.AsyncOpenAI(
            api_key=api_key, timeout=timeout_s, max_retries=0, http_client=http_client
        )
        self._retry_kwargs: dict[str, Any] = {"sleep": sleep} if sleep else {}

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        vectors: list[list[float]] = []
        tokens = 0
        for i in range(0, len(texts), BATCH_SIZE):
            batch = texts[i : i + BATCH_SIZE]
            resp = await retry_async(
                lambda b=batch: self._client.embeddings.create(
                    model=self.model, input=b, dimensions=self._dimensions
                ),
                connection_errors=CONNECTION_ERRORS,
                **self._retry_kwargs,
            )
            data = sorted(resp.data, key=lambda d: d.index)
            if len(data) != len(batch):
                raise EmbeddingError(f"expected {len(batch)} embeddings, got {len(data)}")
            for d in data:
                if len(d.embedding) != EMBEDDING_DIM:
                    raise EmbeddingError(
                        f"embedding has {len(d.embedding)} dims, schema expects {EMBEDDING_DIM}"
                    )
                vectors.append(d.embedding)
            tokens += resp.usage.prompt_tokens
        return EmbeddingResult(vectors=vectors, input_tokens=tokens, model=self.model)
