import uuid
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.db.models import UsageEvent
from core.llm.types import Usage


async def record_usage(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    operation: str,
    provider: str,
    model: str,
    usage: Usage,
    cost_eur: Decimal,
    conversation_id: uuid.UUID | None = None,
    estimated: bool = False,
) -> None:
    """Write a usage_events row in its own short session, so a failing request cannot lose it."""
    async with sessionmaker() as session:
        session.add(
            UsageEvent(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                operation=operation,
                provider=provider,
                model=model,
                # Cache tokens are billed as input; store them together so totals add up.
                input_tokens=usage.input_tokens
                + usage.cache_read_tokens
                + usage.cache_write_tokens,
                output_tokens=usage.output_tokens,
                cost_eur=cost_eur,
                estimated=estimated,
            )
        )
        await session.commit()
