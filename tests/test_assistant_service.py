import logging
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agents.assistant import repository as repo
from agents.assistant.graph import Assistant, AssistantDeps
from agents.assistant.messages import static_message
from agents.assistant.service import ChatService
from agents.assistant.types import Done, Error, Event, LeadForm, Meta, Token
from core.db.models import Conversation
from core.db.models import Message as MessageRow
from core.llm.budget import BudgetGuard
from core.llm.client import LLMClient
from core.rag.retrieve import RetrievedChunk
from core.tenants.config import Lang, TenantConfig
from tests.fakes import FakeLLMProvider
from tests.test_assistant_graph import ANSWER, make_cfg, make_chunk, understand_json

SM = async_sessionmaker[AsyncSession]


# ---------------------------------------------------------------- repository


async def test_conversation_created_with_retention(sessionmaker: SM, tenant_id: uuid.UUID) -> None:
    async with sessionmaker() as s, s.begin():
        conv = await repo.get_or_create_conversation(
            s, tenant_id, "v1", None, retention_days=30, language="fi", page_url="https://x.fi/a"
        )
    delta = conv.expires_at - datetime.now(UTC)
    assert timedelta(days=29, hours=23) < delta <= timedelta(days=30)
    assert conv.language == "fi" and conv.page_url == "https://x.fi/a"


async def test_conversation_resumed_only_for_same_tenant_and_visitor(
    sessionmaker: SM, tenant_id: uuid.UUID, other_tenant_id: uuid.UUID
) -> None:
    async with sessionmaker() as s, s.begin():
        conv = await repo.get_or_create_conversation(s, tenant_id, "v1", None, retention_days=90)
    async with sessionmaker() as s, s.begin():
        same = await repo.get_or_create_conversation(s, tenant_id, "v1", conv.id, retention_days=90)
        other_visitor = await repo.get_or_create_conversation(
            s, tenant_id, "v2", conv.id, retention_days=90
        )
        other_tenant = await repo.get_or_create_conversation(
            s, other_tenant_id, "v1", conv.id, retention_days=90
        )
    assert same.id == conv.id
    assert other_visitor.id != conv.id
    assert other_tenant.id != conv.id and other_tenant.tenant_id == other_tenant_id


async def test_expired_conversation_is_not_resumed(sessionmaker: SM, tenant_id: uuid.UUID) -> None:
    async with sessionmaker() as s, s.begin():
        conv = await repo.get_or_create_conversation(s, tenant_id, "v1", None, retention_days=90)
        await s.execute(
            update(Conversation)
            .where(Conversation.id == conv.id)
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    async with sessionmaker() as s, s.begin():
        again = await repo.get_or_create_conversation(
            s, tenant_id, "v1", conv.id, retention_days=90
        )
    assert again.id != conv.id


async def test_history_order_limit_and_tenant_isolation(
    sessionmaker: SM, tenant_id: uuid.UUID, other_tenant_id: uuid.UUID
) -> None:
    async with sessionmaker() as s, s.begin():
        conv = await repo.get_or_create_conversation(s, tenant_id, "v1", None, retention_days=90)
    for i, role in enumerate(["user", "assistant", "user", "assistant"]):
        async with sessionmaker() as s, s.begin():
            await repo.add_message(s, tenant_id, conv.id, role, f"m{i}")
    async with sessionmaker() as s:
        last3 = await repo.load_history(s, tenant_id, conv.id, 3)
        none = await repo.load_history(s, tenant_id, conv.id, 0)
        leaked = await repo.load_history(s, other_tenant_id, conv.id, 10)  # wrong tenant
        users = await repo.count_user_messages(s, tenant_id, conv.id)
        users_other = await repo.count_user_messages(s, other_tenant_id, conv.id)
    assert [m.content for m in last3] == ["m1", "m2", "m3"]
    assert none == [] and leaked == [] and users == 2 and users_other == 0


async def test_add_message_rejects_foreign_conversation(
    sessionmaker: SM, tenant_id: uuid.UUID, other_tenant_id: uuid.UUID
) -> None:
    async with sessionmaker() as s, s.begin():
        conv = await repo.get_or_create_conversation(s, tenant_id, "v1", None, retention_days=90)
    async with sessionmaker() as s:
        with pytest.raises(LookupError):
            await repo.add_message(s, other_tenant_id, conv.id, "user", "x")


# ---------------------------------------------------------------- ChatService


class Rig:
    def __init__(self, sessionmaker: SM, tenant_id: uuid.UUID, *scripts: str | list[str]) -> None:
        self.sm = sessionmaker
        self.tenant_id = tenant_id
        self.provider = FakeLLMProvider(*scripts)
        self.chunks: list[RetrievedChunk] = [make_chunk()]
        llm = LLMClient(self.provider, sessionmaker, BudgetGuard(sessionmaker), 0.92)

        async def retriever(
            tid: uuid.UUID,
            q: str,
            lang: Lang,
            k: int,
            cid: uuid.UUID | None,
            *,
            raw_query: str | None = None,
        ) -> list[RetrievedChunk]:
            return self.chunks

        self.service = ChatService(Assistant(AssistantDeps(llm, retriever)), sessionmaker)

    async def chat(
        self,
        message: str,
        *,
        cfg: TenantConfig | None = None,
        conversation_id: uuid.UUID | None = None,
        visitor_id: str = "v1",
    ) -> list[Event]:
        return [
            e
            async for e in self.service.chat(
                tenant_id=self.tenant_id,
                cfg=cfg or make_cfg(),
                visitor_id=visitor_id,
                message=message,
                conversation_id=conversation_id,
            )
        ]

    async def stored(self) -> list[MessageRow]:
        async with self.sm() as s:
            stmt = (
                select(MessageRow)
                .where(MessageRow.tenant_id == self.tenant_id)
                .order_by(MessageRow.created_at, MessageRow.role.desc())
            )
            return list((await s.execute(stmt)).scalars().all())


def meta(events: list[Event]) -> Meta:
    first = events[0]
    assert isinstance(first, Meta)
    return first


async def test_turn_is_persisted_with_sources_and_confidence(
    sessionmaker: SM, tenant_id: uuid.UUID
) -> None:
    rig = Rig(sessionmaker, tenant_id, understand_json(), ANSWER)
    events = await rig.chat("Tarjoatteko SEO:ta?")
    done = events[-1]
    assert isinstance(done, Done) and done.message_id is not None

    rows = await rig.stored()
    assert [(r.role, r.content) for r in rows] == [
        ("user", "Tarjoatteko SEO:ta?"),
        ("assistant", "Kyllä, tarjoamme SEO-palvelua [1]."),
    ]
    assistant = rows[1]
    assert (
        assistant.id == done.message_id
        and assistant.conversation_id == meta(events).conversation_id
    )
    assert assistant.sources == [{"title": "Page 1", "url": "https://acme.fi/seo"}]
    assert float(assistant.confidence or 0) == pytest.approx(0.8)


async def test_second_turn_sees_history_in_the_same_conversation(
    sessionmaker: SM, tenant_id: uuid.UUID
) -> None:
    rig = Rig(sessionmaker, tenant_id, understand_json(), ANSWER, understand_json(), ANSWER)
    first = await rig.chat("Tarjoatteko SEO:ta?")
    cid = meta(first).conversation_id
    second = await rig.chat("Entä mitä se maksaa?", conversation_id=cid)

    assert meta(second).conversation_id == cid
    answer_call = rig.provider.calls[3]
    assert [m.role for m in answer_call["messages"]] == ["user", "assistant", "user"]
    assert answer_call["messages"][0].content == "Tarjoatteko SEO:ta?"
    assert len(await rig.stored()) == 4


async def test_too_long_message_is_rejected_without_llm_or_persistence(
    sessionmaker: SM, tenant_id: uuid.UUID
) -> None:
    rig = Rig(sessionmaker, tenant_id)
    cfg = make_cfg()
    events = await rig.chat("x" * (cfg.limits.max_message_chars + 1), cfg=cfg)
    texts = [e.text for e in events if isinstance(e, Token)]
    assert texts == [static_message(cfg, "too_long", "fi")] or texts == [
        static_message(cfg, "too_long", "en")
    ]
    assert rig.provider.calls == [] and await rig.stored() == []
    done = events[-1]
    assert isinstance(done, Done) and not done.lead_form


async def test_conversation_limit_gives_fallback_and_lead_form(
    sessionmaker: SM, tenant_id: uuid.UUID
) -> None:
    cfg = make_cfg()
    cfg = cfg.model_copy(
        update={"limits": cfg.limits.model_copy(update={"messages_per_conversation": 1})}
    )
    rig = Rig(sessionmaker, tenant_id, understand_json(), ANSWER)
    first = await rig.chat("Tarjoatteko SEO:ta?", cfg=cfg)
    second = await rig.chat(
        "Ja toinen kysymys?", cfg=cfg, conversation_id=meta(first).conversation_id
    )

    assert len(rig.provider.calls) == 2  # only the first turn used the LLM
    assert any(isinstance(e, LeadForm) and e.reason == "conversation_limit" for e in second)
    done = second[-1]
    assert isinstance(done, Done) and done.lead_form and done.message_id is not None
    assert len(await rig.stored()) == 4  # transcript stays complete


async def test_budget_exceeded_falls_back_with_lead_form(
    sessionmaker: SM, tenant_id: uuid.UUID
) -> None:
    cfg = make_cfg()
    cfg = cfg.model_copy(update={"limits": cfg.limits.model_copy(update={"monthly_budget_eur": 0})})
    rig = Rig(sessionmaker, tenant_id)
    events = await rig.chat("Tarjoatteko SEO:ta?", cfg=cfg)

    assert rig.provider.calls == []
    assert any(isinstance(e, LeadForm) and e.reason == "unavailable" for e in events)
    assert not any(isinstance(e, Error) for e in events)
    rows = await rig.stored()
    assert [r.role for r in rows] == ["user", "assistant"]
    done = events[-1]
    assert isinstance(done, Done) and done.lead_form


async def test_unexpected_failure_yields_safe_error_and_logs_no_content(
    sessionmaker: SM, tenant_id: uuid.UUID, caplog: pytest.LogCaptureFixture
) -> None:
    rig = Rig(sessionmaker, tenant_id)  # no scripts: the first LLM call raises
    with caplog.at_level(logging.INFO):
        events = await rig.chat("Salainen kysymys henkilötiedoista")
    errors = [e for e in events if isinstance(e, Error)]
    assert len(errors) == 1 and errors[0].code == "assistant_error"
    assert any(isinstance(e, LeadForm) for e in events)
    assert "Salainen" not in caplog.text and "AssertionError" in caplog.text
    assert [r.role for r in await rig.stored()] == ["user"]  # the question is kept
