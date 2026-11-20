"""Persistence for sources, chunks and ingest jobs.

Every function takes `tenant_id` and filters by it (architecture rule 2). None of them commits:
the caller owns the transaction, so "replace a page's chunks" is one atomic unit.
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.db.models import Chunk, IngestJob, Source
from core.db.textsearch import ts_config_expr
from core.ingest.chunking import ChunkDraft

# Source identity: web and faq sources by uri, files by filename (unique index uq_sources_identity)
_IDENTITY = func.coalesce(Source.uri, Source.filename)


async def get_source(
    session: AsyncSession, tenant_id: uuid.UUID, type_: str, key: str
) -> Source | None:
    stmt = select(Source).where(
        Source.tenant_id == tenant_id, Source.type == type_, _IDENTITY == key
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_sources(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    type_: str | None = None,
    status: str | None = None,
) -> list[Source]:
    stmt = select(Source).where(Source.tenant_id == tenant_id).order_by(Source.created_at)
    if type_ is not None:
        stmt = stmt.where(Source.type == type_)
    if status is not None:
        stmt = stmt.where(Source.status == status)
    return list((await session.execute(stmt)).scalars())


async def upsert_source(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    type_: str,
    key: str,
    title: str | None,
    language: str | None,
    content_hash: str,
) -> Source:
    """Create the source or update it in place (reactivating a removed one)."""
    source = await get_source(session, tenant_id, type_, key)
    if source is None:
        source = Source(
            tenant_id=tenant_id,
            type=type_,
            uri=None if type_ == "file" else key,
            filename=key if type_ == "file" else None,
        )
        session.add(source)
    source.title = title
    source.language = language
    source.content_hash = content_hash
    source.status = "active"
    source.last_ingested_at = datetime.now(UTC)
    await session.flush()
    return source


async def touch_source(session: AsyncSession, tenant_id: uuid.UUID, source_id: uuid.UUID) -> None:
    """Record that an unchanged source was seen again."""
    await session.execute(
        update(Source)
        .where(Source.tenant_id == tenant_id, Source.id == source_id)
        .values(last_ingested_at=datetime.now(UTC))
    )


async def replace_chunks(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    source_id: uuid.UUID,
    *,
    title: str | None,
    language: str | None,
    drafts: Sequence[ChunkDraft],
    vectors: Sequence[Sequence[float]],
) -> int:
    """Delete the source's chunks and insert the new ones. Returns the number inserted."""
    if len(drafts) != len(vectors):
        raise ValueError(f"{len(drafts)} chunks but {len(vectors)} vectors")
    owned = await session.execute(
        select(Source.id).where(Source.id == source_id, Source.tenant_id == tenant_id)
    )
    if owned.scalar_one_or_none() is None:
        raise LookupError(f"source {source_id} not found for tenant {tenant_id}")

    await session.execute(
        delete(Chunk).where(Chunk.tenant_id == tenant_id, Chunk.source_id == source_id)
    )
    config = ts_config_expr(language)
    for draft, vector in zip(drafts, vectors, strict=True):
        searchable = " ".join(p for p in (title, draft.heading_path, draft.content) if p)
        session.add(
            Chunk(
                tenant_id=tenant_id,
                source_id=source_id,
                ordinal=draft.ordinal,
                content=draft.content,
                heading_path=draft.heading_path,
                token_count=draft.token_count,
                language=language,
                embedding=list(vector),
                tsv=func.to_tsvector(config, searchable),
            )
        )
    await session.flush()
    return len(drafts)


async def mark_removed(
    session: AsyncSession, tenant_id: uuid.UUID, source_ids: Sequence[uuid.UUID]
) -> int:
    """Mark sources removed and delete their chunks. Returns the number of sources changed."""
    if not source_ids:
        return 0
    await session.execute(
        delete(Chunk).where(Chunk.tenant_id == tenant_id, Chunk.source_id.in_(source_ids))
    )
    result = await session.execute(
        update(Source)
        .where(Source.tenant_id == tenant_id, Source.id.in_(source_ids), Source.status != "removed")
        .values(status="removed")
    )
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


async def delete_sources(
    session: AsyncSession, tenant_id: uuid.UUID, source_ids: Sequence[uuid.UUID]
) -> int:
    """Hard-delete sources (chunks cascade). Used for FAQ entries, which are managed content."""
    if not source_ids:
        return 0
    result = await session.execute(
        delete(Source).where(Source.tenant_id == tenant_id, Source.id.in_(source_ids))
    )
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


async def count_chunks(
    session: AsyncSession, tenant_id: uuid.UUID, source_id: uuid.UUID | None = None
) -> int:
    stmt = select(func.count()).select_from(Chunk).where(Chunk.tenant_id == tenant_id)
    if source_id is not None:
        stmt = stmt.where(Chunk.source_id == source_id)
    return int((await session.execute(stmt)).scalar_one())


async def create_job(session: AsyncSession, tenant_id: uuid.UUID, kind: str) -> IngestJob:
    job = IngestJob(tenant_id=tenant_id, kind=kind, status="running", stats={})
    session.add(job)
    await session.flush()
    return job


async def finish_job(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    job_id: uuid.UUID,
    *,
    status: str,
    stats: dict[str, Any],
    error: str | None = None,
) -> None:
    await session.execute(
        update(IngestJob)
        .where(IngestJob.tenant_id == tenant_id, IngestJob.id == job_id)
        .values(status=status, stats=stats, error=error)
    )
