"""EmbeddingClient: provider call + usage_events row (operation 'embed')."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.embeddings.provider import EmbeddingProvider
from core.llm.pricing import cost_eur
from core.llm.types import Usage
from core.llm.usage import record_usage


class EmbeddingClient:
    def __init__(
        self,
        provider: EmbeddingProvider,
        sessionmaker: async_sessionmaker[AsyncSession],
        eur_per_usd: float,
    ) -> None:
        self._provider = provider
        self._sessionmaker = sessionmaker
        self._eur_per_usd = eur_per_usd

    async def embed(
        self,
        tenant_id: uuid.UUID,
        texts: list[str],
        *,
        conversation_id: uuid.UUID | None = None,
    ) -> list[list[float]]:
        if not texts:
            return []
        result = await self._provider.embed(texts)
        usage = Usage(input_tokens=result.input_tokens)
        await record_usage(
            self._sessionmaker,
            tenant_id=tenant_id,
            operation="embed",
            provider=self._provider.name,
            model=result.model,
            usage=usage,
            cost_eur=cost_eur(result.model, usage, self._eur_per_usd),
            conversation_id=conversation_id,
        )
        return result.vectors
