"""LLMClient: the single entry point for LLM calls (architecture rule 3).

budget check -> provider call -> usage_events row (with EUR cost).
"""

import asyncio
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.llm.budget import BudgetGuard
from core.llm.pricing import cost_eur
from core.llm.provider import LLMProvider
from core.llm.types import LLMResult, Message, TextDelta, Usage, UsageUpdate
from core.llm.usage import record_usage


class LLMClient:
    def __init__(
        self,
        provider: LLMProvider,
        sessionmaker: async_sessionmaker[AsyncSession],
        budget: BudgetGuard,
        eur_per_usd: float,
    ) -> None:
        self._provider = provider
        self._sessionmaker = sessionmaker
        self._budget = budget
        self._eur_per_usd = eur_per_usd

    async def _record(
        self,
        tenant_id: uuid.UUID,
        operation: str,
        model: str,
        usage: Usage,
        conversation_id: uuid.UUID | None,
        estimated: bool = False,
    ) -> None:
        await record_usage(
            self._sessionmaker,
            tenant_id=tenant_id,
            operation=operation,
            provider=self._provider.name,
            model=model,
            usage=usage,
            cost_eur=cost_eur(model, usage, self._eur_per_usd),
            conversation_id=conversation_id,
            estimated=estimated,
        )

    async def complete(
        self,
        *,
        tenant_id: uuid.UUID,
        monthly_budget_eur: float | None,
        operation: str,
        messages: Sequence[Message],
        system: str,
        model: str,
        max_tokens: int,
        temperature: float | None = None,
        tools: list[dict[str, Any]] | None = None,
        conversation_id: uuid.UUID | None = None,
    ) -> LLMResult:
        if monthly_budget_eur is not None:
            await self._budget.check(tenant_id, monthly_budget_eur)
        result = await self._provider.complete(
            messages=messages,
            system=system,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            tools=tools,
        )
        await self._record(tenant_id, operation, model, result.usage, conversation_id)
        return result

    async def stream(
        self,
        *,
        tenant_id: uuid.UUID,
        monthly_budget_eur: float | None,
        operation: str,
        messages: Sequence[Message],
        system: str,
        model: str,
        max_tokens: int,
        temperature: float | None = None,
        tools: list[dict[str, Any]] | None = None,
        conversation_id: uuid.UUID | None = None,
    ) -> AsyncGenerator[TextDelta]:
        """Yield text deltas. Usage is recorded when the stream ends - also when the consumer
        closes it early, in which case output tokens are estimated (chars / 4) and the row is
        flagged estimated=True."""
        if monthly_budget_eur is not None:
            await self._budget.check(tenant_id, monthly_budget_eur)
        usage = Usage()
        final = False
        started = False
        text_chars = 0
        events: AsyncIterator[Any] = self._provider.stream(
            messages=messages,
            system=system,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            tools=tools,
        )
        try:
            async for ev in events:
                if isinstance(ev, TextDelta):
                    text_chars += len(ev.text)
                    yield ev
                elif isinstance(ev, UsageUpdate):
                    started = True
                    usage, final = ev.usage, ev.final
        finally:
            aclose = getattr(events, "aclose", None)
            if aclose is not None:
                await aclose()
            if started or text_chars:
                estimated = not final
                if estimated:
                    usage = Usage(
                        input_tokens=usage.input_tokens,
                        output_tokens=text_chars // 4,
                        cache_read_tokens=usage.cache_read_tokens,
                        cache_write_tokens=usage.cache_write_tokens,
                    )
                # Shielded: must still be written when the consumer task is being cancelled.
                await asyncio.shield(
                    self._record(tenant_id, operation, model, usage, conversation_id, estimated)
                )
