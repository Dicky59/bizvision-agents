"""Lead notification and retention. Only the tenant is notified; the visitor never gets mail
(sending to unverified addresses would make the form an abuse vector)."""

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.leads import repository as repo
from core.notify.leads import (
    TRANSCRIPT_MAX_MESSAGES,
    NewLead,
    Sleep,
    TranscriptLine,
    send_new_lead,
)
from core.notify.sender import EmailSender
from core.tenants.config import TenantConfig

log = logging.getLogger(__name__)

SessionMaker = async_sessionmaker[AsyncSession]
DEFAULT_BACKOFF = (1.0, 4.0)


async def notify_lead(
    sessionmaker: SessionMaker,
    sender: EmailSender,
    tenant_id: uuid.UUID,
    cfg: TenantConfig,
    lead_id: uuid.UUID,
    *,
    backoff: tuple[float, ...] = DEFAULT_BACKOFF,
    sleep: Sleep = asyncio.sleep,
) -> bool:
    """Send the new_lead email and set `notified_at`. Idempotent; never raises on send failure
    (the lead stays stored with notified_at NULL and `notify_pending` retries it)."""
    async with sessionmaker() as s:
        lead = await repo.get_lead(s, tenant_id, lead_id)
        if lead is None:
            return False
        if lead.notified_at is not None:
            return True
        transcript: list[TranscriptLine] = []
        page_url: str | None = None
        if lead.conversation_id is not None:
            page_url = await repo.conversation_page_url(s, tenant_id, lead.conversation_id)
            if cfg.handoff.include_transcript:
                msgs = await repo.recent_messages(
                    s, tenant_id, lead.conversation_id, TRANSCRIPT_MAX_MESSAGES
                )
                transcript = [
                    TranscriptLine("user" if role == "user" else "assistant", text)
                    for role, text in msgs
                ]
        language: Literal["fi", "en"] = "en" if lead.language == "en" else "fi"
        payload = NewLead(
            tenant_name=cfg.name,
            handoff_email=str(cfg.handoff.email),
            language=language,
            name=lead.name,
            email=lead.email,
            phone=lead.phone,
            message=lead.message,
            page_url=page_url,
            consent_at=lead.consent_at,
            privacy_notice_version=lead.privacy_notice_version,
            transcript=transcript,
        )
    result = await send_new_lead(sender, payload, backoff=backoff, sleep=sleep)
    if not result.ok:
        log.error("lead notification failed tenant=%s lead=%s", tenant_id, lead_id)
        return False
    async with sessionmaker() as s, s.begin():
        await repo.mark_notified(s, tenant_id, lead_id, datetime.now(UTC))
    log.info(
        "lead notified tenant=%s lead=%s provider_id=%s", tenant_id, lead_id, result.provider_id
    )
    return True


@dataclass(frozen=True)
class PendingResult:
    sent: int
    failed: int


async def notify_pending(
    sessionmaker: SessionMaker,
    sender: EmailSender,
    tenant_id: uuid.UUID,
    cfg: TenantConfig,
    *,
    backoff: tuple[float, ...] = DEFAULT_BACKOFF,
) -> PendingResult:
    async with sessionmaker() as s:
        ids = [lead.id for lead in await repo.list_unnotified(s, tenant_id)]
    sent = failed = 0
    for lead_id in ids:
        if await notify_lead(sessionmaker, sender, tenant_id, cfg, lead_id, backoff=backoff):
            sent += 1
        else:
            failed += 1
    return PendingResult(sent, failed)


@dataclass(frozen=True)
class CleanupResult:
    conversations: int
    leads: int


async def cleanup_tenant(
    sessionmaker: SessionMaker,
    tenant_id: uuid.UUID,
    cfg: TenantConfig,
    now: datetime | None = None,
) -> CleanupResult:
    """Retention job for one tenant: expired conversations (messages cascade) and leads older
    than `handoff.lead_retention_days`."""
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=cfg.handoff.lead_retention_days)
    async with sessionmaker() as s, s.begin():
        conversations = await repo.delete_expired_conversations(s, tenant_id, now)
        leads = await repo.delete_expired_leads(s, tenant_id, cutoff)
    return CleanupResult(conversations, leads)
