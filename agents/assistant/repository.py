"""Persistence for conversations and messages.

Every function takes `tenant_id` and filters by it (architecture rule 2). None of them commits:
the caller owns the transaction. Message contents are never logged.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db.models import Conversation
from core.db.models import Message as MessageRow
from core.llm.types import Message


async def get_conversation(
    session: AsyncSession, tenant_id: uuid.UUID, conversation_id: uuid.UUID
) -> Conversation | None:
    stmt = select(Conversation).where(
        Conversation.tenant_id == tenant_id, Conversation.id == conversation_id
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_or_create_conversation(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    visitor_id: str,
    conversation_id: uuid.UUID | None,
    *,
    retention_days: int,
    language: str | None = None,
    page_url: str | None = None,
) -> Conversation:
    """Resume `conversation_id` only if it belongs to this tenant AND this visitor and has not
    expired; otherwise start a new one (a guessed or stale id never exposes another's history)."""
    now = datetime.now(UTC)
    if conversation_id is not None:
        existing = await get_conversation(session, tenant_id, conversation_id)
        if existing is not None and existing.visitor_id == visitor_id and existing.expires_at > now:
            return existing
    conv = Conversation(
        tenant_id=tenant_id,
        visitor_id=visitor_id,
        language=language,
        page_url=page_url,
        expires_at=now + timedelta(days=retention_days),
    )
    session.add(conv)
    await session.flush()
    return conv


async def load_history(
    session: AsyncSession, tenant_id: uuid.UUID, conversation_id: uuid.UUID, limit: int
) -> list[Message]:
    """The last `limit` messages of the conversation, oldest first."""
    if limit <= 0:
        return []
    stmt = (
        select(MessageRow)
        .where(MessageRow.tenant_id == tenant_id, MessageRow.conversation_id == conversation_id)
        .order_by(MessageRow.created_at.desc(), MessageRow.id.desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [Message("user" if r.role == "user" else "assistant", r.content) for r in reversed(rows)]


async def count_user_messages(
    session: AsyncSession, tenant_id: uuid.UUID, conversation_id: uuid.UUID
) -> int:
    stmt = select(func.count()).where(
        MessageRow.tenant_id == tenant_id,
        MessageRow.conversation_id == conversation_id,
        MessageRow.role == "user",
    )
    return int((await session.execute(stmt)).scalar_one())


async def add_message(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    role: str,
    content: str,
    *,
    sources: list[dict[str, Any]] | None = None,
    confidence: float | None = None,
) -> MessageRow:
    """The conversation must belong to `tenant_id` (the FK alone would not enforce that)."""
    if await get_conversation(session, tenant_id, conversation_id) is None:
        raise LookupError("conversation not found for tenant")
    row = MessageRow(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        role=role,
        content=content,
        sources=sources,
        confidence=confidence,
    )
    session.add(row)
    await session.flush()
    return row
