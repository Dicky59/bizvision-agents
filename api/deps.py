"""App wiring and request dependencies.

External collaborators are injectable (`ApiDeps`) so tests need no keys and no network.
"""

import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Annotated

import httpx
from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agents.assistant.factory import make_chat_service
from agents.assistant.service import ChatService
from api.errors import ApiError, forbidden
from api.ratelimit import InMemoryLimiter, RateLimiter, client_ip
from core.config import get_settings
from core.db.session import get_sessionmaker
from core.embeddings.client import EmbeddingClient
from core.embeddings.openai_provider import OpenAIEmbeddingProvider
from core.ingest.crawler import DEFAULT_DELAY, make_client
from core.ingest.safety import Resolver, system_resolver
from core.llm.anthropic_provider import AnthropicProvider
from core.llm.budget import BudgetGuard
from core.llm.client import LLMClient
from core.notify.sender import EmailSender, make_sender
from core.tenants.service import ResolvedTenant, resolve_public_tenant

log = logging.getLogger(__name__)

SessionMaker = async_sessionmaker[AsyncSession]

_chat_service: ChatService | None = None
_embedder: EmbeddingClient | None = None
_sender: EmailSender | None = None


def default_embedder() -> EmbeddingClient:
    global _embedder
    if _embedder is None:
        s = get_settings()
        if not s.openai_api_key:
            log.error("embeddings unavailable: OPENAI_API_KEY not set")
            raise ApiError(503, "unavailable")
        _embedder = EmbeddingClient(
            OpenAIEmbeddingProvider(s.openai_api_key), get_sessionmaker(), s.eur_per_usd
        )
    return _embedder


def default_chat_service() -> ChatService:
    global _chat_service
    if _chat_service is None:
        s = get_settings()
        if not s.anthropic_api_key:
            log.error("chat unavailable: ANTHROPIC_API_KEY not set")
            raise ApiError(503, "unavailable")
        sm = get_sessionmaker()
        llm = LLMClient(AnthropicProvider(s.anthropic_api_key), sm, BudgetGuard(sm), s.eur_per_usd)
        _chat_service = make_chat_service(llm, default_embedder(), sm)
    return _chat_service


def default_sender() -> EmailSender:
    global _sender
    if _sender is None:
        s = get_settings()
        _sender = make_sender(s.resend_api_key, s.email_from)
    return _sender


def _proxy_count() -> int:
    return get_settings().trusted_proxy_count


def _admin_key() -> str:
    return get_settings().admin_api_key


@dataclass
class ApiDeps:
    sessionmaker: Callable[[], SessionMaker] = get_sessionmaker
    chat_service: Callable[[], ChatService] = default_chat_service
    sender: Callable[[], EmailSender] = default_sender
    limiter: RateLimiter = field(default_factory=InMemoryLimiter)
    trusted_proxy_count: Callable[[], int] = _proxy_count
    notify_backoff: tuple[float, ...] = (1.0, 4.0)  # delays between email send attempts
    admin_key: Callable[[], str] = _admin_key
    embedder: Callable[[], EmbeddingClient] = default_embedder
    crawl_client: Callable[[], httpx.AsyncClient] = make_client
    resolver: Resolver = system_resolver
    crawl_delay: float = DEFAULT_DELAY


def get_deps(request: Request) -> ApiDeps:
    return request.app.state.deps


Deps = Annotated[ApiDeps, Depends(get_deps)]


async def get_session(deps: Deps) -> AsyncIterator[AsyncSession]:
    async with deps.sessionmaker()() as session:
        yield session


Session = Annotated[AsyncSession, Depends(get_session)]


async def public_tenant(slug: str, request: Request, session: Session) -> ResolvedTenant:
    tenant = await resolve_public_tenant(session, slug, request.headers.get("origin"))
    if tenant is None:
        raise forbidden()
    return tenant


PublicTenant = Annotated[ResolvedTenant, Depends(public_tenant)]


def ip_of(request: Request, deps: ApiDeps) -> str:
    return client_ip(request, deps.trusted_proxy_count())
