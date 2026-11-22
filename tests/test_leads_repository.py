import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agents.assistant import repository as arepo
from core.db.models import Conversation, Lead
from core.db.models import Message as MessageRow
from core.leads import repository as repo
from core.leads.service import cleanup_tenant
from tests.api_helpers import tenant_config

SM = async_sessionmaker[AsyncSession]
NOW = datetime.now(UTC)


async def make_lead(
    s: AsyncSession,
    tenant_id: uuid.UUID,
    email: str = "a@example.com",
    conversation_id: uuid.UUID | None = None,
) -> Lead:
    return await repo.create_lead(
        s,
        tenant_id,
        conversation_id=conversation_id,
        name="Name",
        email=email,
        phone=None,
        message=None,
        language="fi",
        consent_at=NOW,
        privacy_notice_version="v1",
    )


async def make_conversation(s: AsyncSession, tenant_id: uuid.UUID) -> uuid.UUID:
    conv = await arepo.get_or_create_conversation(
        s, tenant_id, "visitor-1", None, retention_days=90
    )
    await arepo.add_message(s, tenant_id, conv.id, "user", "hello")
    return conv.id


async def test_create_drops_foreign_conversation(
    sessionmaker: SM, tenant_id: uuid.UUID, other_tenant_id: uuid.UUID
) -> None:
    async with sessionmaker() as s, s.begin():
        foreign = await make_conversation(s, other_tenant_id)
        own = await make_conversation(s, tenant_id)
        a = await make_lead(s, tenant_id, conversation_id=foreign)
        b = await make_lead(s, tenant_id, conversation_id=own)
    assert a.conversation_id is None and b.conversation_id == own


async def test_get_list_unnotified_are_tenant_scoped(
    sessionmaker: SM, tenant_id: uuid.UUID, other_tenant_id: uuid.UUID
) -> None:
    async with sessionmaker() as s, s.begin():
        mine = await make_lead(s, tenant_id)
        theirs = await make_lead(s, other_tenant_id)
    async with sessionmaker() as s:
        assert await repo.get_lead(s, tenant_id, mine.id) is not None
        assert await repo.get_lead(s, tenant_id, theirs.id) is None  # other tenant's id
        assert [x.id for x in await repo.list_leads(s, tenant_id)] == [mine.id]
        assert [x.id for x in await repo.list_unnotified(s, tenant_id)] == [mine.id]


async def test_mark_notified_is_scoped_and_once(
    sessionmaker: SM, tenant_id: uuid.UUID, other_tenant_id: uuid.UUID
) -> None:
    async with sessionmaker() as s, s.begin():
        lead = await make_lead(s, tenant_id)
    async with sessionmaker() as s, s.begin():
        assert await repo.mark_notified(s, other_tenant_id, lead.id, NOW) is False
        assert await repo.mark_notified(s, tenant_id, lead.id, NOW) is True
        assert await repo.mark_notified(s, tenant_id, lead.id, NOW) is False
    async with sessionmaker() as s:
        assert await repo.list_unnotified(s, tenant_id) == []


async def test_delete_by_email_erases_lead_and_conversation_for_one_tenant(
    sessionmaker: SM, tenant_id: uuid.UUID, other_tenant_id: uuid.UUID
) -> None:
    async with sessionmaker() as s, s.begin():
        conv = await make_conversation(s, tenant_id)
        await make_lead(s, tenant_id, "Anna@Example.com", conv)
        await make_lead(s, tenant_id, "other@example.com")
        other_conv = await make_conversation(s, other_tenant_id)
        await make_lead(s, other_tenant_id, "anna@example.com", other_conv)
    async with sessionmaker() as s, s.begin():
        erased = await repo.delete_by_email(s, tenant_id, " anna@example.com ")
    assert (erased.leads, erased.conversations) == (1, 1)
    async with sessionmaker() as s:
        mine = [x.email for x in await repo.list_leads(s, tenant_id)]
        theirs = [x.email for x in await repo.list_leads(s, other_tenant_id)]
        msgs = (
            await s.execute(select(func.count()).where(MessageRow.conversation_id == conv))
        ).scalar_one()
        other_conv_left = (
            await s.execute(select(func.count()).where(Conversation.id == other_conv))
        ).scalar_one()
    assert mine == ["other@example.com"]
    assert theirs == ["anna@example.com"]  # same address at another tenant is untouched
    assert msgs == 0 and other_conv_left == 1


async def test_retention_cleanup_deletes_only_expired_for_the_tenant(
    sessionmaker: SM, tenant_id: uuid.UUID, other_tenant_id: uuid.UUID
) -> None:
    cfg = tenant_config("x", handoff={
        "email": "i@example.com", "privacy_notice_version": "v1", "lead_retention_days": 30,
    })  # fmt: skip
    async with sessionmaker() as s, s.begin():
        old = await make_lead(s, tenant_id, "old@example.com")
        await make_lead(s, tenant_id, "new@example.com")
        foreign_old = await make_lead(s, other_tenant_id, "f@example.com")
        for lead in (old, foreign_old):
            lead.created_at = NOW - timedelta(days=31)
        expired = await make_conversation(s, tenant_id)
        live = await make_conversation(s, tenant_id)
        foreign_expired = await make_conversation(s, other_tenant_id)
        for cid in (expired, foreign_expired):
            conv = await s.get(Conversation, cid)
            assert conv is not None
            conv.expires_at = NOW - timedelta(seconds=1)
    res = await cleanup_tenant(sessionmaker, tenant_id, cfg)
    assert (res.conversations, res.leads) == (1, 1)
    async with sessionmaker() as s:
        assert [x.email for x in await repo.list_leads(s, tenant_id)] == ["new@example.com"]
        assert len(await repo.list_leads(s, other_tenant_id)) == 1  # other tenant untouched
        assert await repo.conversation_exists(s, tenant_id, live)
        assert not await repo.conversation_exists(s, tenant_id, expired)
        assert await repo.conversation_exists(s, other_tenant_id, foreign_expired)


async def test_transcript_and_page_url_are_tenant_scoped(
    sessionmaker: SM, tenant_id: uuid.UUID, other_tenant_id: uuid.UUID
) -> None:
    async with sessionmaker() as s, s.begin():
        conv = await arepo.get_or_create_conversation(
            s, tenant_id, "visitor-1", None, retention_days=90, page_url="https://x.fi/a"
        )
    for i, role in enumerate(
        ["user", "assistant", "user"]
    ):  # one transaction per message, as in chat
        async with sessionmaker() as s, s.begin():
            await arepo.add_message(s, tenant_id, conv.id, role, f"m{i}")
    async with sessionmaker() as s:
        last2 = await repo.recent_messages(s, tenant_id, conv.id, 2)
        leaked = await repo.recent_messages(s, other_tenant_id, conv.id, 10)
        url = await repo.conversation_page_url(s, tenant_id, conv.id)
        url_leaked = await repo.conversation_page_url(s, other_tenant_id, conv.id)
    assert last2 == [("assistant", "m1"), ("user", "m2")]
    assert leaked == [] and url == "https://x.fi/a" and url_leaked is None
