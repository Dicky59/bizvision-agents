"""Shared CLI plumbing: injectable external dependencies and tenant loading.

Tests replace `DEPS` (always access it as `deps.DEPS`) to avoid real HTTP and real embeddings.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

import httpx
import typer
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cli.tenants import _fail
from core.config import get_settings
from core.db.models import UsageEvent
from core.embeddings.client import EmbeddingClient
from core.embeddings.openai_provider import OpenAIEmbeddingProvider
from core.ingest.crawler import DEFAULT_DELAY, make_client
from core.ingest.safety import Resolver, system_resolver
from core.llm.anthropic_provider import AnthropicProvider
from core.llm.budget import BudgetGuard
from core.llm.client import LLMClient
from core.notify.sender import EmailSender, make_sender
from core.tenants.config import TenantConfig
from core.tenants.repository import get_by_slug

SessionMaker = async_sessionmaker[AsyncSession]


def _default_llm(sessionmaker: SessionMaker) -> LLMClient:
    settings = get_settings()
    if not settings.anthropic_api_key:
        raise _fail("ANTHROPIC_API_KEY is not set (copy .env.example to .env and fill it in)")
    return LLMClient(
        AnthropicProvider(settings.anthropic_api_key),
        sessionmaker,
        BudgetGuard(sessionmaker),
        settings.eur_per_usd,
    )


def _default_sender() -> EmailSender:
    settings = get_settings()
    if not settings.resend_api_key:
        raise _fail("RESEND_API_KEY is not set (copy .env.example to .env and fill it in)")
    return make_sender(settings.resend_api_key, settings.email_from)


@dataclass(frozen=True)
class Deps:
    make_embedder: Callable[[SessionMaker], EmbeddingClient]
    make_client: Callable[[], httpx.AsyncClient]
    resolver: Resolver
    delay: float
    make_llm: Callable[[SessionMaker], LLMClient] = _default_llm
    make_sender: Callable[[], EmailSender] = _default_sender


def _default_embedder(sessionmaker: SessionMaker) -> EmbeddingClient:
    settings = get_settings()
    if not settings.openai_api_key:
        raise _fail("OPENAI_API_KEY is not set (copy .env.example to .env and fill it in)")
    return EmbeddingClient(
        OpenAIEmbeddingProvider(settings.openai_api_key), sessionmaker, settings.eur_per_usd
    )


DEPS = Deps(
    make_embedder=_default_embedder,
    make_client=make_client,
    resolver=system_resolver,
    delay=DEFAULT_DELAY,
)


async def load_tenant(session: AsyncSession, slug: str) -> tuple[uuid.UUID, TenantConfig]:
    tenant = await get_by_slug(session, slug)
    if tenant is None:
        raise _fail(
            f"Tenant '{slug}' not found (run `bv tenants create {slug} --config ...` first)"
        )
    try:
        return tenant.id, TenantConfig.model_validate(tenant.config)
    except ValidationError as e:
        raise _fail(f"Stored config for '{slug}' is invalid:\n{e}") from e


async def db_now(sessionmaker: SessionMaker) -> datetime:
    """Database clock, so cost windows are not skewed by the local clock."""
    async with sessionmaker() as session:
        return (await session.execute(select(func.clock_timestamp()))).scalar_one()


async def embed_cost_since(
    sessionmaker: SessionMaker, tenant_id: uuid.UUID, since: datetime
) -> Decimal:
    async with sessionmaker() as session:
        total = await session.execute(
            select(func.coalesce(func.sum(UsageEvent.cost_eur), 0)).where(
                UsageEvent.tenant_id == tenant_id,
                UsageEvent.operation == "embed",
                UsageEvent.created_at >= since,
            )
        )
        return Decimal(total.scalar_one())


def echo_cost(cost: Decimal) -> None:
    typer.echo(f"Embedding cost: EUR {cost:.6f}")
