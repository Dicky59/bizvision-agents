import json
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import anthropic
import httpx2
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.db.models import Tenant, UsageEvent
from core.llm.anthropic_provider import AnthropicProvider
from core.llm.budget import BudgetGuard
from core.llm.client import LLMClient
from core.llm.errors import BudgetExceeded
from core.llm.pricing import cost_eur
from core.llm.types import Message, Usage

SM = async_sessionmaker[AsyncSession]
MSG = [Message("user", "Hei")]
SYS = "You are helpful."
HAIKU = "claude-haiku-5-5"


def sse(*events: tuple[str, dict[str, Any]]) -> bytes:
    return "".join(f"event: {n}\ndata: {json.dumps(d)}\n\n" for n, d in events).encode()


def stream_body(texts: list[str], out_tokens: int = 15, in_tokens: int = 100) -> bytes:
    evs: list[tuple[str, dict[str, Any]]] = [
        (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "model": HAIKU,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {
                        "input_tokens": in_tokens,
                        "output_tokens": 1,
                        "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 0,
                    },
                },
            },
        ),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
    ]
    for t in texts:
        delta = {"type": "text_delta", "text": t}
        evs.append(
            (
                "content_block_delta",
                {"type": "content_block_delta", "index": 0, "delta": delta},
            )
        )
    evs += [
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": out_tokens},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]
    return sse(*evs)


def ok_json(text: str = "Moi!") -> httpx2.Response:
    return httpx2.Response(
        200,
        json={
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": HAIKU,
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 1000, "output_tokens": 200},
        },
    )


def ok_stream(
    texts: list[str] | None = None, out_tokens: int = 15, in_tokens: int = 100
) -> httpx2.Response:
    body = stream_body(texts or ["Hei", " maailma", "!"], out_tokens, in_tokens)
    return httpx2.Response(200, content=body, headers={"content-type": "text/event-stream"})


def err(status: int, headers: dict[str, str] | None = None) -> httpx2.Response:
    return httpx2.Response(
        status,
        json={"type": "error", "error": {"type": "api_error", "message": "boom"}},
        headers=headers,
    )


class Recorder:
    """httpx2 mock transport handler that records request bodies and injected sleeps."""

    def __init__(self, responses: list[httpx2.Response] | Callable[[], httpx2.Response]) -> None:
        self.responses = responses
        self.requests: list[dict[str, Any]] = []
        self.sleeps: list[float] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(json.loads(request.content))
        if callable(self.responses):
            return self.responses()
        return self.responses.pop(0)

    async def sleep(self, s: float) -> None:
        self.sleeps.append(s)

    def provider(self) -> AnthropicProvider:
        http = httpx2.AsyncClient(transport=httpx2.MockTransport(self))
        return AnthropicProvider("k", http_client=http, sleep=self.sleep)

    async def complete(self, model: str = HAIKU, temperature: float | None = None) -> Any:
        return await self.provider().complete(
            messages=MSG, system=SYS, model=model, max_tokens=10, temperature=temperature
        )


def make_client(provider: AnthropicProvider, sm: SM) -> LLMClient:
    return LLMClient(provider, sm, BudgetGuard(sm), eur_per_usd=0.92)


def call_kwargs(tid: uuid.UUID) -> dict[str, Any]:
    return {
        "tenant_id": tid,
        "monthly_budget_eur": 30,
        "operation": "chat",
        "messages": MSG,
        "system": SYS,
        "model": HAIKU,
        "max_tokens": 600,
    }


async def usage_rows(sm: SM, tid: uuid.UUID) -> list[UsageEvent]:
    async with sm() as s:
        q = select(UsageEvent).where(UsageEvent.tenant_id == tid)
        return list((await s.execute(q)).scalars())


# ---------- pricing ----------


def test_cost_math_and_eur_conversion() -> None:
    usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    assert float(cost_eur(HAIKU, usage, 0.92)) == pytest.approx(0.6 * 0.92)
    assert float(cost_eur("claude-sonnet-5-5", usage, 1.0)) == pytest.approx(12.0)


def test_cache_tokens_priced() -> None:
    usage = Usage(cache_read_tokens=1_000_000, cache_write_tokens=1_000_000)
    # sonnet-5-5: cache read $0.10 (explicit), cache write 1.25 * $2
    assert float(cost_eur("claude-sonnet-5-5", usage, 1.0)) == pytest.approx(0.10 + 2.5)


def test_unknown_model_costs_zero_and_warns(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        assert cost_eur("mystery-model", Usage(input_tokens=10), 0.92) == 0
    assert "no pricing" in caplog.text


# ---------- provider request shape ----------


async def test_request_body_haiku() -> None:
    rec = Recorder([ok_json()])
    await rec.complete(HAIKU, temperature=0.2)
    body = rec.requests[0]
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert body["thinking"] == {"type": "disabled"}
    assert "temperature" not in body  # 5.5 models reject non-default sampling
    assert body["max_tokens"] == 10


async def test_request_body_unknown_model_has_no_thinking_override() -> None:
    rec = Recorder([ok_json()])
    await rec.complete("some-other", temperature=0.2)
    assert "temperature" not in rec.requests[0]
    assert "thinking" not in rec.requests[0]


# ---------- complete + usage ----------


async def test_complete_records_usage(sessionmaker: SM, tenant_id: uuid.UUID) -> None:
    client = make_client(Recorder([ok_json("Moi!")]).provider(), sessionmaker)
    res = await client.complete(**call_kwargs(tenant_id))
    assert res.text == "Moi!"
    (row,) = await usage_rows(sessionmaker, tenant_id)
    assert (row.operation, row.provider, row.model) == ("chat", "anthropic", HAIKU)
    assert (row.input_tokens, row.output_tokens, row.estimated) == (1000, 200, False)
    assert float(row.cost_eur) == pytest.approx((1000 * 0.10 + 200 * 0.50) / 1e6 * 0.92)


# ---------- streaming ----------


async def test_stream_events_and_final_usage(sessionmaker: SM, tenant_id: uuid.UUID) -> None:
    client = make_client(Recorder([ok_stream()]).provider(), sessionmaker)
    texts = [d.text async for d in client.stream(**call_kwargs(tenant_id))]
    assert texts == ["Hei", " maailma", "!"]
    (row,) = await usage_rows(sessionmaker, tenant_id)
    assert (row.input_tokens, row.output_tokens, row.estimated) == (100, 15, False)
    assert float(row.cost_eur) > 0


async def test_stream_closed_early_estimates_output_tokens(
    sessionmaker: SM, tenant_id: uuid.UUID
) -> None:
    chunk = "x" * 40
    client = make_client(
        Recorder([ok_stream([chunk, chunk, chunk], out_tokens=999)]).provider(), sessionmaker
    )
    gen = client.stream(**call_kwargs(tenant_id))
    first = await anext(gen)
    assert first.text == chunk
    await gen.aclose()  # client disconnect before message_delta
    (row,) = await usage_rows(sessionmaker, tenant_id)
    assert row.estimated is True
    assert row.output_tokens == 40 // 4
    assert row.input_tokens == 100  # known from message_start
    assert float(row.cost_eur) > 0


# ---------- retries ----------


async def test_retries_529_then_succeeds() -> None:
    rec = Recorder([err(529), ok_json()])
    res = await rec.complete()
    assert res.text == "Moi!"
    assert len(rec.requests) == 2 and len(rec.sleeps) == 1


async def test_retry_after_header_honoured() -> None:
    rec = Recorder([err(429, {"retry-after": "3"}), ok_json()])
    await rec.complete()
    assert rec.sleeps == [3.0]


async def test_retry_after_is_capped() -> None:
    rec = Recorder([err(429, {"retry-after": "120"}), ok_json()])
    await rec.complete()
    assert rec.sleeps == [20.0]


async def test_backoff_without_header_is_exponential_and_bounded() -> None:
    rec = Recorder([err(500), err(502), ok_json()])
    await rec.complete()
    assert len(rec.sleeps) == 2
    assert 1.0 <= rec.sleeps[0] < 2.0 and 2.0 <= rec.sleeps[1] < 3.0


async def test_retries_exhausted_raises() -> None:
    rec = Recorder(lambda: err(500))
    with pytest.raises(anthropic.InternalServerError):
        await rec.complete()
    assert len(rec.requests) == 4  # 1 + 3 retries


async def test_client_errors_not_retried() -> None:
    rec = Recorder([err(400)])
    with pytest.raises(anthropic.BadRequestError):
        await rec.complete()
    assert len(rec.requests) == 1 and rec.sleeps == []


async def test_stream_establishment_is_retried() -> None:
    rec = Recorder([err(529), ok_stream()])
    events = [
        e
        async for e in rec.provider().stream(
            messages=MSG, system=SYS, model=HAIKU, max_tokens=10, temperature=None
        )
    ]
    assert len(events) > 3 and len(rec.sleeps) == 1


# ---------- budget ----------


async def add_usage(sm: SM, tid: uuid.UUID, eur: float, created: datetime | None = None) -> None:
    async with sm() as s:
        ev = UsageEvent(
            tenant_id=tid,
            operation="chat",
            provider="anthropic",
            model="m",
            input_tokens=1,
            output_tokens=1,
            cost_eur=eur,
        )
        if created:
            ev.created_at = created
        s.add(ev)
        await s.commit()


async def test_budget_guard_blocks_at_limit(sessionmaker: SM, tenant_id: uuid.UUID) -> None:
    await BudgetGuard(sessionmaker).check(tenant_id, 1.0)  # no spend yet
    await add_usage(sessionmaker, tenant_id, 1.0)
    guard = BudgetGuard(sessionmaker)
    with pytest.raises(BudgetExceeded):
        await guard.check(tenant_id, 1.0)
    await guard.check(tenant_id, 1.01)


async def test_budget_only_counts_current_month(sessionmaker: SM, tenant_id: uuid.UUID) -> None:
    now = datetime(2026, 11, 15, 12, tzinfo=UTC)
    await add_usage(sessionmaker, tenant_id, 5.0, datetime(2026, 10, 31, 23, 59, tzinfo=UTC))
    await add_usage(sessionmaker, tenant_id, 0.5, datetime(2026, 11, 1, 0, 0, 1, tzinfo=UTC))
    guard = BudgetGuard(sessionmaker, now=lambda: now)
    assert await guard.month_spend(tenant_id) == pytest.approx(0.5)


async def test_budget_cache_ttl(sessionmaker: SM, tenant_id: uuid.UUID) -> None:
    t = [0.0]
    guard = BudgetGuard(sessionmaker, clock=lambda: t[0])
    assert await guard.month_spend(tenant_id) == 0
    await add_usage(sessionmaker, tenant_id, 2.0)
    assert await guard.month_spend(tenant_id) == 0  # still cached
    t[0] = 61.0
    assert await guard.month_spend(tenant_id) == pytest.approx(2.0)


async def test_budget_is_tenant_isolated(sessionmaker: SM, tenant_id: uuid.UUID) -> None:
    other = uuid.uuid4()
    async with sessionmaker() as s:
        s.add(Tenant(id=other, slug=f"t-{other.hex[:10]}", name="O", public_key=f"pk_{other.hex}"))
        await s.commit()
    try:
        await add_usage(sessionmaker, other, 99.0)
        assert await BudgetGuard(sessionmaker).month_spend(tenant_id) == 0
    finally:
        async with sessionmaker() as s:
            row = await s.get(Tenant, other)
            assert row is not None
            await s.delete(row)
            await s.commit()


async def test_client_refuses_when_over_budget(sessionmaker: SM, tenant_id: uuid.UUID) -> None:
    await add_usage(sessionmaker, tenant_id, 50.0)
    rec = Recorder([ok_json()])
    client = make_client(rec.provider(), sessionmaker)
    with pytest.raises(BudgetExceeded):
        await client.complete(**call_kwargs(tenant_id))
    assert rec.requests == []  # provider never called


async def test_budget_sums_tiny_costs_with_new_precision(
    sessionmaker: SM, tenant_id: uuid.UUID
) -> None:
    for _ in range(1000):
        await add_usage(sessionmaker, tenant_id, 1.84e-7)
    await add_usage(sessionmaker, tenant_id, 0.25)
    spend = await BudgetGuard(sessionmaker).month_spend(tenant_id)
    assert spend == pytest.approx(0.25 + 1000 * 1.84e-7, abs=1e-9)
    with pytest.raises(BudgetExceeded):
        await BudgetGuard(sessionmaker).check(tenant_id, 0.2501)
    await BudgetGuard(sessionmaker).check(tenant_id, 0.2503)
