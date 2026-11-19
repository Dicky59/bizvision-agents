"""Real-API tests. Skipped by default; run with `uv run pytest -m live`.

They write usage_events into the throwaway tenant from the `tenant_id` fixture, in the
bv_test database - never the dev database.
"""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import get_settings
from core.db.models import EMBEDDING_DIM, UsageEvent
from core.embeddings.client import EmbeddingClient
from core.embeddings.openai_provider import OpenAIEmbeddingProvider
from core.llm.anthropic_provider import AnthropicProvider
from core.llm.budget import BudgetGuard
from core.llm.client import LLMClient
from core.llm.types import Message

pytestmark = pytest.mark.live
SM = async_sessionmaker[AsyncSession]


async def rows(sm: SM, tid: uuid.UUID) -> list[UsageEvent]:
    async with sm() as s:
        return list(
            (await s.execute(select(UsageEvent).where(UsageEvent.tenant_id == tid))).scalars()
        )


async def test_live_anthropic_complete_and_stream(sessionmaker: SM, tenant_id: uuid.UUID) -> None:
    settings = get_settings()
    if not settings.anthropic_api_key:
        pytest.skip("ANTHROPIC_API_KEY not set")
    client = LLMClient(
        AnthropicProvider(settings.anthropic_api_key),
        sessionmaker,
        BudgetGuard(sessionmaker),
        settings.eur_per_usd,
    )
    kwargs = {
        "tenant_id": tenant_id,
        "monthly_budget_eur": 30,
        "operation": "chat",
        "messages": [Message("user", "Sano hei yhdellä sanalla.")],
        "system": "Vastaa lyhyesti suomeksi.",
        "model": "claude-haiku-5-5",
        "max_tokens": 50,
        "temperature": 0.2,
    }
    res = await client.complete(**kwargs)
    assert res.text.strip()
    streamed = "".join([d.text async for d in client.stream(**kwargs)])
    assert streamed.strip()

    complete_row, stream_row = await rows(sessionmaker, tenant_id)
    for row in (complete_row, stream_row):
        assert row.input_tokens > 0 and row.output_tokens > 0
        assert float(row.cost_eur) > 0
        assert row.estimated is False


async def test_live_openai_embeddings(sessionmaker: SM, tenant_id: uuid.UUID) -> None:
    settings = get_settings()
    if not settings.openai_api_key:
        pytest.skip("OPENAI_API_KEY not set")
    client = EmbeddingClient(
        OpenAIEmbeddingProvider(settings.openai_api_key), sessionmaker, settings.eur_per_usd
    )
    vectors = await client.embed(tenant_id, ["Mitä palveluita tarjoatte?"])
    assert len(vectors) == 1 and len(vectors[0]) == EMBEDDING_DIM
    (row,) = await rows(sessionmaker, tenant_id)
    assert row.operation == "embed" and row.input_tokens > 0 and float(row.cost_eur) > 0
