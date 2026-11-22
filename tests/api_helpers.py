"""Helpers for API tests: a throwaway active tenant with a valid config, and an app client."""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.deps import ApiDeps
from api.main import create_app
from core.db.models import Tenant
from core.tenants.config import TenantConfig
from core.tenants.service import clear_cache

SM = async_sessionmaker[AsyncSession]
ORIGIN = "https://shop.example.com"


def tenant_config(slug: str, **overrides: Any) -> TenantConfig:
    data: dict[str, Any] = {
        "slug": slug,
        "name": "Test Oy",
        "allowed_origins": [ORIGIN],
        "handoff": {
            "email": "info@test.example.com",
            "privacy_notice_url": {"fi": "https://shop.example.com/tietosuoja"},
            "privacy_notice_version": "v1",
        },
        "assistant": {"greeting": {"fi": "Hei!"}, "starter_questions": {"fi": ["Mitä teette?"]}},
    }
    data.update(overrides)
    return TenantConfig.model_validate(data)


@dataclass
class ApiTenant:
    id: uuid.UUID
    slug: str


@asynccontextmanager
async def api_tenant(sm: SM, status: str = "active", **cfg: Any) -> AsyncIterator[ApiTenant]:
    tid = uuid.uuid4()
    slug = f"api-{tid.hex[:10]}"
    config = tenant_config(slug, **cfg)
    async with sm() as s:
        s.add(
            Tenant(
                id=tid,
                slug=slug,
                name="Test Oy",
                public_key=f"pk_{tid.hex}",
                status=status,
                config=config.model_dump(mode="json"),
            )
        )
        await s.commit()
    clear_cache()
    try:
        yield ApiTenant(tid, slug)
    finally:
        async with sm() as s:
            await s.execute(delete(Tenant).where(Tenant.id == tid))
            await s.commit()
        clear_cache()


def client_for(deps: ApiDeps) -> AsyncClient:
    app = create_app(deps)
    return AsyncClient(
        transport=ASGITransport(app=app, client=("203.0.113.9", 5000)), base_url="http://test"
    )
