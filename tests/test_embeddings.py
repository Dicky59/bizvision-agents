import json
import uuid

import httpx2
import openai
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.db.models import EMBEDDING_DIM, UsageEvent
from core.embeddings.client import EmbeddingClient
from core.embeddings.openai_provider import OpenAIEmbeddingProvider
from core.embeddings.provider import EmbeddingError

SM = async_sessionmaker[AsyncSession]


def vec(i: int, dim: int = EMBEDDING_DIM) -> list[float]:
    return [float(i)] + [0.0] * (dim - 1)


class FakeOpenAI:
    """Handler for httpx2.MockTransport that echoes one vector per input, shuffled order."""

    def __init__(self, dim: int = EMBEDDING_DIM, fail_first: int = 0) -> None:
        self.dim = dim
        self.batches: list[list[str]] = []
        self.fail_first = fail_first
        self.sleeps: list[float] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        if self.fail_first > 0:
            self.fail_first -= 1
            return httpx2.Response(529, json={"error": {"message": "overloaded"}})
        body = json.loads(request.content)
        texts: list[str] = body["input"]
        self.batches.append(texts)
        data = [
            {"object": "embedding", "index": i, "embedding": vec(int(t), self.dim)}
            for i, t in enumerate(texts)
        ]
        data.reverse()  # provider must restore order by index
        return httpx2.Response(
            200,
            json={
                "object": "list",
                "data": data,
                "model": body["model"],
                "usage": {"prompt_tokens": 7 * len(texts), "total_tokens": 7 * len(texts)},
            },
        )

    async def sleep(self, s: float) -> None:
        self.sleeps.append(s)

    def provider(self) -> OpenAIEmbeddingProvider:
        http = httpx2.AsyncClient(transport=httpx2.MockTransport(self))
        return OpenAIEmbeddingProvider("k", http_client=http, sleep=self.sleep)


async def test_batches_of_100_and_order_preserved() -> None:
    fake = FakeOpenAI()
    texts = [str(i) for i in range(250)]
    result = await fake.provider().embed(texts)
    assert [len(b) for b in fake.batches] == [100, 100, 50]
    assert [v[0] for v in result.vectors] == [float(i) for i in range(250)]
    assert result.input_tokens == 7 * 250


async def test_dimension_mismatch_raises() -> None:
    with pytest.raises(EmbeddingError, match="dims"):
        await FakeOpenAI(dim=512).provider().embed(["1"])


async def test_retries_overloaded() -> None:
    fake = FakeOpenAI(fail_first=1)
    result = await fake.provider().embed(["3"])
    assert result.vectors[0][0] == 3.0 and len(fake.sleeps) == 1


async def test_client_errors_propagate_without_retry() -> None:
    calls: list[int] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(1)
        return httpx2.Response(401, json={"error": {"message": "bad key"}})

    http = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    with pytest.raises(openai.AuthenticationError):
        await OpenAIEmbeddingProvider("k", http_client=http).embed(["1"])
    assert calls == [1]


async def test_embed_records_usage(sessionmaker: SM, tenant_id: uuid.UUID) -> None:
    client = EmbeddingClient(FakeOpenAI().provider(), sessionmaker, eur_per_usd=0.92)
    vectors = await client.embed(tenant_id, ["1", "2", "3"])
    assert len(vectors) == 3
    async with sessionmaker() as s:
        rows = list(
            (await s.execute(select(UsageEvent).where(UsageEvent.tenant_id == tenant_id))).scalars()
        )
    (row,) = rows
    assert (row.operation, row.provider, row.model) == ("embed", "openai", "text-embedding-3-small")
    assert (row.input_tokens, row.output_tokens) == (21, 0)
    assert float(row.cost_eur) == pytest.approx(21 * 0.02 / 1e6 * 0.92, abs=1e-6)


async def test_empty_input_makes_no_call(sessionmaker: SM, tenant_id: uuid.UUID) -> None:
    fake = FakeOpenAI()
    assert await EmbeddingClient(fake.provider(), sessionmaker, 0.92).embed(tenant_id, []) == []
    assert fake.batches == []


async def test_tiny_embedding_cost_survives_db_round_trip(
    sessionmaker: SM, tenant_id: uuid.UUID
) -> None:
    """10 tokens of text-embedding-3-small cost ~1.84e-7 EUR; must not round to 0 (cost_eur is
    Numeric(18, 10))."""
    from core.llm.pricing import cost_eur
    from core.llm.types import Usage
    from core.llm.usage import record_usage

    cost = cost_eur("text-embedding-3-small", Usage(input_tokens=10), 0.92)
    assert float(cost) == pytest.approx(10 * 0.02 / 1e6 * 0.92)
    await record_usage(
        sessionmaker,
        tenant_id=tenant_id,
        operation="embed",
        provider="openai",
        model="text-embedding-3-small",
        usage=Usage(input_tokens=10),
        cost_eur=cost,
    )
    async with sessionmaker() as s:
        row = (
            await s.execute(select(UsageEvent).where(UsageEvent.tenant_id == tenant_id))
        ).scalar_one()
    assert float(row.cost_eur) > 0
    assert float(row.cost_eur) == pytest.approx(1.84e-7, rel=1e-6)
