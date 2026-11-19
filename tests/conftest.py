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


@pytest.fixture(scope="session")
def migrated_db(test_database_url: str) -> None:
    """Ensure the bv_test schema is at head (never touches the dev database)."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=Path(__file__).resolve().parent.parent,
        env={**os.environ, "ALEMBIC_DATABASE_URL": test_database_url},
        check=True,
        capture_output=True,
    )
