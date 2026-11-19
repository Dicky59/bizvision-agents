import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from core.config import get_settings
from core.db.models import Tenant
from core.db.session import make_engine


@pytest.fixture(scope="session")
def test_database_url() -> str:
    return get_settings().test_database_url


@pytest.fixture
async def test_engine(test_database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(test_database_url)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def migrated_db(test_database_url: str) -> None:
    """Ensure the bv_test schema is at head (never touches the dev database)."""
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=Path(__file__).resolve().parent.parent,
        env={**os.environ, "ALEMBIC_DATABASE_URL": test_database_url},
        check=True,
        capture_output=True,
    )


@pytest.fixture
async def sessionmaker(
    test_engine: AsyncEngine, migrated_db: None
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(test_engine, expire_on_commit=False)


@pytest.fixture
async def tenant_id(sessionmaker: async_sessionmaker[AsyncSession]) -> AsyncIterator[uuid.UUID]:
    """A throwaway tenant in bv_test (never the dev DB); cascades its usage_events on cleanup."""
    tid = uuid.uuid4()
    async with sessionmaker() as s:
        s.add(Tenant(id=tid, slug=f"t-{tid.hex[:10]}", name="Test", public_key=f"pk_{tid.hex}"))
        await s.commit()
    try:
        yield tid
    finally:
        async with sessionmaker() as s:
            await s.execute(delete(Tenant).where(Tenant.id == tid))
            await s.commit()
