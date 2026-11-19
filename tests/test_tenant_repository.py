import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from typer.testing import CliRunner

from cli.main import app
from core.config import get_settings
from core.db.models import Tenant
from core.tenants import service
from core.tenants.config import TenantConfig
from core.tenants.repository import get_by_slug, list_tenants, upsert_tenant

pytestmark = pytest.mark.usefixtures("migrated_db")
ROOT = Path(__file__).resolve().parent.parent


def make_cfg(slug: str, **kw: object) -> TenantConfig:
    return TenantConfig.model_validate(
        {
            "slug": slug,
            "name": "T",
            "handoff": {"email": "a@t.fi", "privacy_notice_version": "1"},
            "allowed_origins": ["https://t.fi"],
            **kw,
        }
    )


@pytest.fixture
async def session(test_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with async_sessionmaker(test_engine, expire_on_commit=False)() as s:
        yield s


@pytest.fixture
def slug() -> str:
    return f"t-{uuid.uuid4().hex[:10]}"


@pytest.fixture(autouse=True)
async def _cleanup(test_engine: AsyncEngine) -> AsyncIterator[None]:
    service.clear_cache()
    yield
    async with async_sessionmaker(test_engine)() as s:
        await s.execute(
            delete(Tenant).where(Tenant.slug.like("t-%") | (Tenant.slug == "bizvision"))
        )
        await s.commit()


async def test_upsert_is_idempotent_and_keeps_public_key(session: AsyncSession, slug: str) -> None:
    t1, created1 = await upsert_tenant(session, make_cfg(slug))
    t2, created2 = await upsert_tenant(session, make_cfg(slug, name="Renamed"))
    assert created1 and not created2
    assert t1.id == t2.id and t1.public_key == t2.public_key
    assert t2.name == "Renamed" and t2.config["name"] == "Renamed"
    assert (await get_by_slug(session, slug)) is not None
    assert slug in [t.slug for t in await list_tenants(session)]


async def test_resolve_public_tenant(session: AsyncSession, slug: str) -> None:
    await upsert_tenant(session, make_cfg(slug))
    ok = await service.resolve_public_tenant(session, slug, "https://t.fi")
    assert ok is not None and ok.slug == slug
    assert await service.resolve_public_tenant(session, slug, "https://evil.fi") is None
    assert await service.resolve_public_tenant(session, "t-missing", "https://t.fi") is None
    row = await get_by_slug(session, slug)
    assert row is not None
    row.status = "paused"
    await session.commit()
    service.clear_cache()
    assert await service.resolve_public_tenant(session, slug, "https://t.fi") is None


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, test_database_url: str) -> Iterator[None]:
    monkeypatch.setenv("DATABASE_URL", test_database_url)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_cli_create_and_show(cli_env: None) -> None:
    runner = CliRunner()
    r = runner.invoke(
        app, ["tenants", "create", "bizvision", "--config", str(ROOT / "tenants/bizvision.yaml")]
    )
    assert r.exit_code == 0, r.output
    assert "Created tenant 'bizvision'" in r.output
    r = runner.invoke(
        app, ["tenants", "create", "bizvision", "--config", str(ROOT / "tenants/bizvision.yaml")]
    )
    assert "Updated tenant" in r.output
    assert "bizvision" in runner.invoke(app, ["tenants", "show", "bizvision"]).output
    assert "bizvision" in runner.invoke(app, ["tenants", "list"]).output


def test_cli_slug_mismatch_fails(cli_env: None) -> None:
    r = CliRunner().invoke(
        app, ["tenants", "create", "other", "--config", str(ROOT / "tenants/bizvision.yaml")]
    )
    assert r.exit_code == 1
    assert "Slug mismatch" in r.output
