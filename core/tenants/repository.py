import secrets

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db.models import Tenant
from core.tenants.config import TenantConfig


async def upsert_tenant(session: AsyncSession, config: TenantConfig) -> tuple[Tenant, bool]:
    """Create or update a tenant from config. Returns (tenant, created). public_key is stable."""
    tenant = await get_by_slug(session, config.slug)
    created = tenant is None
    if tenant is None:
        tenant = Tenant(slug=config.slug, public_key=f"pk_{secrets.token_urlsafe(24)}")
        session.add(tenant)
    tenant.name = config.name
    tenant.config = config.model_dump(mode="json")
    await session.commit()
    await session.refresh(tenant)
    return tenant, created


async def get_by_slug(session: AsyncSession, slug: str) -> Tenant | None:
    return (await session.execute(select(Tenant).where(Tenant.slug == slug))).scalar_one_or_none()


async def list_tenants(session: AsyncSession) -> list[Tenant]:
    return list((await session.execute(select(Tenant).order_by(Tenant.slug))).scalars())
