import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

ROOT = Path(__file__).resolve().parent.parent
EXPECTED_TABLES = {
    "tenants",
    "sources",
    "chunks",
    "ingest_jobs",
    "conversations",
    "messages",
    "leads",
    "usage_events",
}


def run_alembic(url: str, *args: str) -> None:
    env = {**os.environ, "ALEMBIC_DATABASE_URL": url}
    subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
    )


async def test_upgrade_and_downgrade(test_engine: AsyncEngine, test_database_url: str) -> None:
    run_alembic(test_database_url, "downgrade", "base")
    run_alembic(test_database_url, "upgrade", "head")

    async with test_engine.connect() as conn:
        tables = {
            r[0]
            for r in await conn.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            )
        }
        assert EXPECTED_TABLES <= tables

        ext = await conn.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'vector'"))
        assert ext.scalar() == 1

        indexes = {
            r[0]
            for r in await conn.execute(
                text("SELECT indexname FROM pg_indexes WHERE tablename = 'chunks'")
            )
        }
        assert {"ix_chunks_embedding_hnsw", "ix_chunks_tsv"} <= indexes

        source_indexes = {
            r[0]
            for r in await conn.execute(
                text("SELECT indexname FROM pg_indexes WHERE tablename = 'sources'")
            )
        }
        assert "uq_sources_identity" in source_indexes

    run_alembic(test_database_url, "downgrade", "base")
    async with test_engine.connect() as conn:
        tables = {
            r[0]
            for r in await conn.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            )
        }
        assert not (EXPECTED_TABLES & tables)

    run_alembic(test_database_url, "upgrade", "head")
