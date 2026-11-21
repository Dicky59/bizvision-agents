"""ChatService: one visitor turn = input guards + persistence around the assistant graph.

    conversation (resume or create) -> Meta -> guards -> save user message
    -> graph events (Token/Sources/LeadForm/Done) -> save assistant message -> Done(message_id)

Guard failures and provider/budget errors are answered with localized static texts and, where it
helps the visitor, a lead form: the visitor is never left with a dead end. Message contents are
never logged; only ids and exception types are.
"""

import dataclasses
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agents.assistant import repository as repo
from agents.assistant.graph import Assistant
from agents.assistant.messages import static_message
from agents.assistant.types import (
    AssistantRequest,
    Done,
    Error,
    Event,
    LeadForm,
    Meta,
    Token,
)
from core.ingest.language import detect_language
from core.llm.errors import BudgetExceeded
from core.tenants.config import Lang, TenantConfig

log = logging.getLogger(__name__)

SessionMaker = async_sessionmaker[AsyncSession]


def _lang(lang: str | None, message: str, cfg: TenantConfig) -> Lang:
    chosen = lang or detect_language(message) or cfg.default_language
    return "fi" if chosen == "fi" else "en"


class ChatService:
    def __init__(self, assistant: Assistant, sessionmaker: SessionMaker) -> None:
        self._assistant = assistant
        self._sessionmaker = sessionmaker

    async def chat(
        self,
        *,
        tenant_id: uuid.UUID,
        cfg: TenantConfig,
        visitor_id: str,
        message: str,
        conversation_id: uuid.UUID | None = None,
        language: Lang | None = None,
        page_url: str | None = None,
    ) -> AsyncIterator[Event]:
        lang = _lang(language, message, cfg)
        async with self._sessionmaker() as s, s.begin():
            conv = await repo.get_or_create_conversation(
                s,
                tenant_id,
                visitor_id,
                conversation_id,
                retention_days=cfg.retention_days,
                language=lang,
                page_url=page_url,
            )
            cid = conv.id
            history = await repo.load_history(s, tenant_id, cid, cfg.assistant.history_turns)
            used = await repo.count_user_messages(s, tenant_id, cid)
        yield Meta(cid)

        if len(message) > cfg.limits.max_message_chars:
            yield Token(static_message(cfg, "too_long", lang))
            yield self._done(cfg, lang, static_message(cfg, "too_long", lang), lead_form=False)
            return
        if used >= cfg.limits.messages_per_conversation:
            async for ev in self._fallback(
                tenant_id, cid, cfg, lang, "conversation_limit", message
            ):
                yield ev
            return

        await self._save(tenant_id, cid, "user", message)

        request = AssistantRequest(
            tenant_id=tenant_id,
            cfg=cfg,
            message=message,
            history=history,
            conversation_id=cid,
            language_hint=lang,
        )
        try:
            async for ev in self._assistant.reply(request):
                if isinstance(ev, Done):
                    sources = [{"title": x.title, "url": x.url} for x in ev.sources]
                    mid = await self._save(
                        tenant_id,
                        cid,
                        "assistant",
                        ev.text,
                        sources=sources,
                        confidence=ev.relevance,
                    )
                    yield dataclasses.replace(ev, message_id=mid)
                else:
                    yield ev
        except BudgetExceeded:
            log.warning("budget exceeded tenant=%s conversation=%s", tenant_id, cid)
            async for ev in self._fallback(tenant_id, cid, cfg, lang, "unavailable", None):
                yield ev
        except Exception as e:
            # Details (exception text may echo request data) stay out of the log: type only.
            log.error(
                "assistant failed tenant=%s conversation=%s: %s", tenant_id, cid, type(e).__name__
            )
            yield Error("assistant_error", static_message(cfg, "unavailable", lang))
            yield LeadForm("error")

    async def _fallback(
        self,
        tenant_id: uuid.UUID,
        cid: uuid.UUID,
        cfg: TenantConfig,
        lang: Lang,
        key: Literal["unavailable", "conversation_limit"],
        user_message: str | None,
    ) -> AsyncIterator[Event]:
        """Static reply + lead form, persisted so the transcript is complete."""
        text = static_message(cfg, key, lang)
        if user_message is not None:
            await self._save(tenant_id, cid, "user", user_message)
        mid = await self._save(tenant_id, cid, "assistant", text)
        yield Token(text)
        yield LeadForm(key)
        yield dataclasses.replace(self._done(cfg, lang, text, lead_form=True), message_id=mid)

    @staticmethod
    def _done(cfg: TenantConfig, lang: Lang, text: str, *, lead_form: bool) -> Done:
        return Done(
            text=text,
            sources=[],
            intent="question",
            language=lang,
            answerable=None,
            relevance=None,
            header_failed=False,
            lead_form=lead_form,
        )

    async def _save(
        self,
        tenant_id: uuid.UUID,
        cid: uuid.UUID,
        role: str,
        content: str,
        *,
        sources: list[dict[str, str]] | None = None,
        confidence: float | None = None,
    ) -> uuid.UUID:
        async with self._sessionmaker() as s, s.begin():
            row = await repo.add_message(
                s,
                tenant_id,
                cid,
                role,
                content,
                sources=list(sources) if sources else None,
                confidence=confidence,
            )
            return row.id
