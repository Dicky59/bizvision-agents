from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from core.config import get_settings
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
