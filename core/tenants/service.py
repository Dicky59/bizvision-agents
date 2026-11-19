import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from core.tenants.config import TenantConfig
from core.tenants.repository import get_by_slug

CACHE_TTL_S = 60.0


@dataclass(frozen=True)
class ResolvedTenant:
    id: uuid.UUID
    slug: str
    status: str
    config: TenantConfig


_cache: dict[str, tuple[float, ResolvedTenant | None]] = {}
_clock: Callable[[], float] = time.monotonic


def clear_cache() -> None:
    _cache.clear()


async def get_tenant(session: AsyncSession, slug: str) -> ResolvedTenant | None:
    now = _clock()
    hit = _cache.get(slug)
    if hit and now - hit[0] < CACHE_TTL_S:
        return hit[1]
    row = await get_by_slug(session, slug)
    resolved = (
        ResolvedTenant(
            id=row.id,
            slug=row.slug,
            status=row.status,
            config=TenantConfig.model_validate(row.config),
        )
        if row
        else None
    )
    _cache[slug] = (now, resolved)
    return resolved


def origin_allowed(config: TenantConfig, origin: str | None) -> bool:
    return origin is not None and origin.rstrip("/").lower() in config.allowed_origins


async def resolve_public_tenant(
    session: AsyncSession, slug: str, origin: str | None
) -> ResolvedTenant | None:
    """Tenant for a public (widget) request, only if active and origin is allowed."""
    tenant = await get_tenant(session, slug)
    if tenant is None or tenant.status != "active" or not origin_allowed(tenant.config, origin):
        return None
    return tenant
