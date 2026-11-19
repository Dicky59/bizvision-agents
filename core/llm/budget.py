import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.db.models import UsageEvent
from core.llm.errors import BudgetExceeded

CACHE_TTL_S = 60.0


def month_start(now: datetime) -> datetime:
    return now.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class BudgetGuard:
    """Per-tenant monthly spend check with a short in-process cache (single instance in phase 1)."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._clock = clock
        self._now = now
        self._cache: dict[tuple[uuid.UUID, datetime], tuple[float, float]] = {}

    async def month_spend(self, tenant_id: uuid.UUID) -> float:
        start = month_start(self._now())
        key = (tenant_id, start)
        hit = self._cache.get(key)
        if hit and self._clock() - hit[0] < CACHE_TTL_S:
            return hit[1]
        async with self._sessionmaker() as session:
            total = (
                await session.execute(
                    select(func.coalesce(func.sum(UsageEvent.cost_eur), 0)).where(
                        UsageEvent.tenant_id == tenant_id, UsageEvent.created_at >= start
                    )
                )
            ).scalar_one()
        spend = float(total)
        self._cache[key] = (self._clock(), spend)
        return spend

    async def check(self, tenant_id: uuid.UUID, monthly_budget_eur: float) -> None:
        spent = await self.month_spend(tenant_id)
        if spent >= monthly_budget_eur:
            raise BudgetExceeded(spent, monthly_budget_eur)
