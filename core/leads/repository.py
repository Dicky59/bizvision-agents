"""Persistence for leads, plus the retention/erasure deletes that touch them.

Every function takes `tenant_id` and filters by it (architecture rule 2). None of them commits.
Leads are personal data: contents are never logged.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.db.models import Conversation, Lead
from core.db.models import Message as MessageRow


async def create_lead(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    conversation_id: uuid.UUID | None,
    name: str,
    email: str,
    phone: str | None,
    message: str | None,
    language: str,
    consent_at: datetime,
    privacy_notice_version: str,
) -> Lead:
    """A `conversation_id` that does not belong to the tenant is dropped, not trusted."""
    if conversation_id is not None and not await conversation_exists(
        session, tenant_id, conversation_id
    ):
        conversation_id = None
    lead = Lead(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        name=name,
        email=email,
        phone=phone,
        message=message,
        language=language,
        consent_at=consent_at,
        privacy_notice_version=privacy_notice_version,
    )
    session.add(lead)
    await session.flush()
    return lead


async def conversation_exists(
    session: AsyncSession, tenant_id: uuid.UUID, conversation_id: uuid.UUID
) -> bool:
    stmt = select(Conversation.id).where(
        Conversation.tenant_id == tenant_id, Conversation.id == conversation_id
    )
    return (await session.execute(stmt)).first() is not None


async def get_lead(session: AsyncSession, tenant_id: uuid.UUID, lead_id: uuid.UUID) -> Lead | None:
    stmt = select(Lead).where(Lead.tenant_id == tenant_id, Lead.id == lead_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_leads(
    session: AsyncSession, tenant_id: uuid.UUID, *, limit: int = 100, offset: int = 0
) -> list[Lead]:
    stmt = (
        select(Lead)
        .where(Lead.tenant_id == tenant_id)
        .order_by(Lead.created_at.desc(), Lead.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list((await session.execute(stmt)).scalars())


async def list_unnotified(
    session: AsyncSession, tenant_id: uuid.UUID, *, limit: int = 100
) -> list[Lead]:
    stmt = (
        select(Lead)
        .where(Lead.tenant_id == tenant_id, Lead.notified_at.is_(None))
        .order_by(Lead.created_at)
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars())


async def mark_notified(
    session: AsyncSession, tenant_id: uuid.UUID, lead_id: uuid.UUID, when: datetime
) -> bool:
    stmt = (
        update(Lead)
        .where(Lead.tenant_id == tenant_id, Lead.id == lead_id, Lead.notified_at.is_(None))
        .values(notified_at=when)
    )
    return (await session.execute(stmt)).rowcount > 0  # type: ignore[attr-defined]


@dataclass(frozen=True)
class Erased:
    leads: int
    conversations: int


async def delete_by_email(session: AsyncSession, tenant_id: uuid.UUID, email: str) -> Erased:
    """Erasure request: the tenant's leads with this address (case-insensitive) and the
    conversations they came from (their messages go with them via ON DELETE CASCADE)."""
    match = select(Lead.id, Lead.conversation_id).where(
        Lead.tenant_id == tenant_id, func.lower(Lead.email) == email.strip().lower()
    )
    rows = (await session.execute(match)).all()
    lead_ids = [r[0] for r in rows]
    conv_ids = [r[1] for r in rows if r[1] is not None]
    if lead_ids:
        await session.execute(
            delete(Lead).where(Lead.tenant_id == tenant_id, Lead.id.in_(lead_ids))
        )
    if conv_ids:
        await session.execute(
            delete(Conversation).where(
                Conversation.tenant_id == tenant_id, Conversation.id.in_(conv_ids)
            )
        )
    return Erased(len(lead_ids), len(conv_ids))


async def delete_expired_leads(
    session: AsyncSession, tenant_id: uuid.UUID, cutoff: datetime
) -> int:
    stmt = delete(Lead).where(Lead.tenant_id == tenant_id, Lead.created_at < cutoff)
    return (await session.execute(stmt)).rowcount  # type: ignore[attr-defined]


async def delete_expired_conversations(
    session: AsyncSession, tenant_id: uuid.UUID, now: datetime
) -> int:
    stmt = delete(Conversation).where(
        Conversation.tenant_id == tenant_id, Conversation.expires_at < now
    )
    return (await session.execute(stmt)).rowcount  # type: ignore[attr-defined]


async def recent_messages(
    session: AsyncSession, tenant_id: uuid.UUID, conversation_id: uuid.UUID, limit: int
) -> list[tuple[str, str]]:
    """(role, content) of the last `limit` messages, oldest first."""
    stmt = (
        select(MessageRow.role, MessageRow.content)
        .where(MessageRow.tenant_id == tenant_id, MessageRow.conversation_id == conversation_id)
        .order_by(MessageRow.created_at.desc(), MessageRow.id.desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    return [(r[0], r[1]) for r in reversed(rows)]


async def conversation_page_url(
    session: AsyncSession, tenant_id: uuid.UUID, conversation_id: uuid.UUID
) -> str | None:
    stmt = select(Conversation.page_url).where(
        Conversation.tenant_id == tenant_id, Conversation.id == conversation_id
    )
    return (await session.execute(stmt)).scalar_one_or_none()
