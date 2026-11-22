"""Per-tenant counters for the admin overview. Every query filters by tenant_id."""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db.models import Chunk, Conversation, IngestJob, Lead, Source


@dataclass(frozen=True)
class LatestJob:
    kind: str
    status: str
    started_at: datetime
    stats: dict


@dataclass(frozen=True)
class TenantStats:
    sources: int
    chunks: int
    conversations: int
    leads: int
    leads_pending: int
    latest_ingest: LatestJob | None


async def _count(session: AsyncSession, stmt) -> int:  # type: ignore[no-untyped-def]
    return int((await session.execute(stmt)).scalar_one())


async def tenant_stats(session: AsyncSession, tenant_id: uuid.UUID) -> TenantStats:
    sources = await _count(
        session,
        select(func.count()).where(Source.tenant_id == tenant_id, Source.status == "active"),
    )
    chunks = await _count(session, select(func.count()).where(Chunk.tenant_id == tenant_id))
    convs = await _count(session, select(func.count()).where(Conversation.tenant_id == tenant_id))
    leads = await _count(session, select(func.count()).where(Lead.tenant_id == tenant_id))
    pending = await _count(
        session,
        select(func.count()).where(Lead.tenant_id == tenant_id, Lead.notified_at.is_(None)),
    )
    job = (
        await session.execute(
            select(IngestJob)
            .where(IngestJob.tenant_id == tenant_id)
            .order_by(IngestJob.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    latest = LatestJob(job.kind, job.status, job.created_at, job.stats) if job else None
    return TenantStats(sources, chunks, convs, leads, pending, latest)
