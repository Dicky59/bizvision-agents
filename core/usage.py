"""Monthly usage and cost reporting per tenant (UTC calendar months, like the budget guard)."""

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db.models import UsageEvent
from core.llm.budget import month_start

MONTH_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")


@dataclass(frozen=True)
class UsageRow:
    operation: str
    model: str
    events: int
    input_tokens: int
    output_tokens: int
    cost_eur: float


@dataclass(frozen=True)
class MonthlyUsage:
    month: str  # YYYY-MM
    rows: list[UsageRow]

    @property
    def cost_eur(self) -> float:
        return sum(r.cost_eur for r in self.rows)

    @property
    def input_tokens(self) -> int:
        return sum(r.input_tokens for r in self.rows)

    @property
    def output_tokens(self) -> int:
        return sum(r.output_tokens for r in self.rows)


def month_bounds(month: str | None, now: datetime | None = None) -> tuple[str, datetime, datetime]:
    """(YYYY-MM, start, next month's start) for `month`, default the current month.
    Raises ValueError for anything but a valid YYYY-MM."""
    if month is None:
        start = month_start(now or datetime.now(UTC))
    else:
        m = MONTH_RE.match(month)
        if not m:
            raise ValueError("month must be YYYY-MM")
        start = datetime(int(m.group(1)), int(m.group(2)), 1, tzinfo=UTC)
    end = (
        start.replace(year=start.year + 1, month=1)
        if start.month == 12
        else start.replace(month=start.month + 1)
    )
    return start.strftime("%Y-%m"), start, end


async def monthly_usage(
    session: AsyncSession, tenant_id: uuid.UUID, month: str | None = None
) -> MonthlyUsage:
    label, start, end = month_bounds(month)
    stmt = (
        select(
            UsageEvent.operation,
            UsageEvent.model,
            func.count(),
            func.coalesce(func.sum(UsageEvent.input_tokens), 0),
            func.coalesce(func.sum(UsageEvent.output_tokens), 0),
            func.coalesce(func.sum(UsageEvent.cost_eur), 0),
        )
        .where(
            UsageEvent.tenant_id == tenant_id,
            UsageEvent.created_at >= start,
            UsageEvent.created_at < end,
        )
        .group_by(UsageEvent.operation, UsageEvent.model)
        .order_by(UsageEvent.operation, UsageEvent.model)
    )
    rows = [
        UsageRow(op, model, int(n), int(tin), int(tout), float(cost))
        for op, model, n, tin, tout, cost in (await session.execute(stmt)).all()
    ]
    return MonthlyUsage(label, rows)
