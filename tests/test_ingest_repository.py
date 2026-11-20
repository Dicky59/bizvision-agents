import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.db.models import Chunk, Source, Tenant
from core.ingest import repository as repo
from core.ingest.chunking import ChunkDraft
from tests.fakes import hash_vector

SM = async_sessionmaker[AsyncSession]


async def _new_tenant(sm: SM) -> uuid.UUID:
    tid = uuid.uuid4()
    async with sm() as s:
        s.add(Tenant(id=tid, slug=f"t-{tid.hex[:10]}", name="T", public_key=f"pk_{tid.hex}"))
        await s.commit()
    return tid


@pytest.fixture
async def tenant_a(sessionmaker: SM) -> AsyncIterator[uuid.UUID]:
    tid = await _new_tenant(sessionmaker)
    yield tid
    async with sessionmaker() as s:
        await s.execute(delete(Tenant).where(Tenant.id == tid))
        await s.commit()


@pytest.fixture
async def tenant_b(sessionmaker: SM) -> AsyncIterator[uuid.UUID]:
    tid = await _new_tenant(sessionmaker)
    yield tid
    async with sessionmaker() as s:
        await s.execute(delete(Tenant).where(Tenant.id == tid))
        await s.commit()


def drafts(*contents: str) -> list[ChunkDraft]:
    return [ChunkDraft(i, c, "H", len(c) // 4) for i, c in enumerate(contents)]


async def add_source(
    sm: SM, tenant_id: uuid.UUID, key: str, *chunk_texts: str, language: str = "fi"
) -> uuid.UUID:
    async with sm() as s:
        src = await repo.upsert_source(
            s, tenant_id, type_="web", key=key, title="T", language=language, content_hash="h"
        )
        await repo.replace_chunks(
            s,
            tenant_id,
            src.id,
            title="T",
            language=language,
            drafts=drafts(*chunk_texts),
            vectors=[hash_vector(c) for c in chunk_texts],
        )
        await s.commit()
        return src.id


async def test_upsert_creates_then_updates_same_row(sessionmaker: SM, tenant_a: uuid.UUID) -> None:
    async with sessionmaker() as s:
        first = await repo.upsert_source(
            s,
            tenant_a,
            type_="web",
            key="https://x.fi/a",
            title="A",
            language="fi",
            content_hash="1",
        )
        second = await repo.upsert_source(
            s,
            tenant_a,
            type_="web",
            key="https://x.fi/a",
            title="B",
            language="en",
            content_hash="2",
        )
        await s.commit()
    assert first.id == second.id
    assert (second.title, second.language, second.content_hash) == ("B", "en", "2")
    assert second.uri == "https://x.fi/a" and second.filename is None


async def test_file_sources_are_keyed_by_filename(sessionmaker: SM, tenant_a: uuid.UUID) -> None:
    async with sessionmaker() as s:
        src = await repo.upsert_source(
            s,
            tenant_a,
            type_="file",
            key="hinnasto.pdf",
            title="H",
            language=None,
            content_hash="1",
        )
        await s.commit()
        assert (src.uri, src.filename) == (None, "hinnasto.pdf")
        assert (await repo.get_source(s, tenant_a, "file", "hinnasto.pdf")) is not None
        assert (await repo.get_source(s, tenant_a, "web", "hinnasto.pdf")) is None


async def test_chunks_get_embedding_tsv_and_language(sessionmaker: SM, tenant_a: uuid.UUID) -> None:
    sid = await add_source(sessionmaker, tenant_a, "https://x.fi/a", "Hinta on 990 euroa")
    async with sessionmaker() as s:
        row = (await s.execute(select(Chunk).where(Chunk.source_id == sid))).scalar_one()
        assert row.embedding is not None and len(row.embedding) == 1536
        assert row.tsv is not None and row.language == "fi" and row.heading_path == "H"
        # tsv was built with the finnish config
        hit = await s.execute(
            select(func.count())
            .select_from(Chunk)
            .where(
                Chunk.id == row.id,
                Chunk.tsv.op("@@")(func.websearch_to_tsquery("finnish", "euroa")),
            )
        )
        assert hit.scalar_one() == 1


async def test_replace_chunks_replaces(sessionmaker: SM, tenant_a: uuid.UUID) -> None:
    sid = await add_source(sessionmaker, tenant_a, "https://x.fi/a", "vanha 1", "vanha 2")
    async with sessionmaker() as s:
        assert await repo.count_chunks(s, tenant_a, sid) == 2
    await add_source(sessionmaker, tenant_a, "https://x.fi/a", "uusi")
    async with sessionmaker() as s:
        contents = (await s.execute(select(Chunk.content).where(Chunk.source_id == sid))).scalars()
        assert list(contents) == ["uusi"]


async def test_replace_chunks_rejects_mismatched_vectors(
    sessionmaker: SM, tenant_a: uuid.UUID
) -> None:
    async with sessionmaker() as s:
        src = await repo.upsert_source(
            s, tenant_a, type_="web", key="u", title=None, language=None, content_hash="h"
        )
        with pytest.raises(ValueError):
            await repo.replace_chunks(
                s, tenant_a, src.id, title=None, language=None, drafts=drafts("a", "b"), vectors=[]
            )


async def test_mark_removed_deletes_chunks_and_keeps_row(
    sessionmaker: SM, tenant_a: uuid.UUID
) -> None:
    sid = await add_source(sessionmaker, tenant_a, "https://x.fi/a", "sisältö")
    async with sessionmaker() as s:
        assert await repo.mark_removed(s, tenant_a, [sid]) == 1
        assert await repo.mark_removed(s, tenant_a, [sid]) == 0  # already removed
        await s.commit()
    async with sessionmaker() as s:
        src = (await s.execute(select(Source).where(Source.id == sid))).scalar_one()
        assert src.status == "removed"
        assert await repo.count_chunks(s, tenant_a) == 0


async def test_removed_source_is_reactivated_not_duplicated(
    sessionmaker: SM, tenant_a: uuid.UUID
) -> None:
    sid = await add_source(sessionmaker, tenant_a, "https://x.fi/a", "sisältö")
    async with sessionmaker() as s:
        await repo.mark_removed(s, tenant_a, [sid])
        await s.commit()
    again = await add_source(sessionmaker, tenant_a, "https://x.fi/a", "takaisin")
    assert again == sid
    async with sessionmaker() as s:
        assert [x.status for x in await repo.list_sources(s, tenant_a)] == ["active"]


async def test_delete_sources_cascades_chunks(sessionmaker: SM, tenant_a: uuid.UUID) -> None:
    sid = await add_source(sessionmaker, tenant_a, "faq:1", "q a")
    async with sessionmaker() as s:
        assert await repo.delete_sources(s, tenant_a, [sid]) == 1
        await s.commit()
    async with sessionmaker() as s:
        assert await repo.list_sources(s, tenant_a) == []
        assert await repo.count_chunks(s, tenant_a) == 0


async def test_identity_is_unique_per_tenant_and_type(
    sessionmaker: SM, tenant_a: uuid.UUID, tenant_b: uuid.UUID
) -> None:
    async with sessionmaker() as s:
        s.add(Source(tenant_id=tenant_a, type="web", uri="https://x.fi/a"))
        await s.commit()
    # same key, other tenant / other type: allowed
    async with sessionmaker() as s:
        s.add(Source(tenant_id=tenant_b, type="web", uri="https://x.fi/a"))
        s.add(Source(tenant_id=tenant_a, type="faq", uri="https://x.fi/a"))
        await s.commit()
    # same tenant + type + key: rejected by uq_sources_identity
    async with sessionmaker() as s:
        s.add(Source(tenant_id=tenant_a, type="web", uri="https://x.fi/a"))
        with pytest.raises(IntegrityError):
            await s.commit()
    # a file and a web source never collide even with equal strings; rows without identity are free
    async with sessionmaker() as s:
        s.add(Source(tenant_id=tenant_a, type="web"))
        s.add(Source(tenant_id=tenant_a, type="web"))
        await s.commit()
    async with sessionmaker() as s:
        n = await s.execute(
            text("SELECT count(*) FROM sources WHERE tenant_id = :t"), {"t": tenant_a}
        )
        assert n.scalar_one() == 4


async def test_job_lifecycle(sessionmaker: SM, tenant_a: uuid.UUID) -> None:
    async with sessionmaker() as s:
        job = await repo.create_job(s, tenant_a, "crawl")
        await s.commit()
        assert job.status == "running"
        await repo.finish_job(s, tenant_a, job.id, status="done", stats={"new": 3})
        await s.commit()
    async with sessionmaker() as s:
        from core.db.models import IngestJob

        row = (await s.execute(select(IngestJob).where(IngestJob.id == job.id))).scalar_one()
        assert (row.status, row.stats) == ("done", {"new": 3})


# ------------------------------------------------------------------ cross-tenant isolation


async def test_cross_tenant_reads_see_nothing(
    sessionmaker: SM, tenant_a: uuid.UUID, tenant_b: uuid.UUID
) -> None:
    sid = await add_source(sessionmaker, tenant_a, "https://x.fi/a", "salainen sisältö")
    async with sessionmaker() as s:
        assert await repo.get_source(s, tenant_b, "web", "https://x.fi/a") is None
        assert await repo.list_sources(s, tenant_b) == []
        assert await repo.count_chunks(s, tenant_b) == 0
        assert await repo.count_chunks(s, tenant_b, sid) == 0
        assert len(await repo.list_sources(s, tenant_a)) == 1


async def test_cross_tenant_writes_cannot_touch_other_tenants_data(
    sessionmaker: SM, tenant_a: uuid.UUID, tenant_b: uuid.UUID
) -> None:
    sid = await add_source(sessionmaker, tenant_a, "https://x.fi/a", "arvokas")
    async with sessionmaker() as s:
        # replacing chunks of A's source as tenant B is refused outright
        with pytest.raises(LookupError):
            await repo.replace_chunks(
                s,
                tenant_b,
                sid,
                title=None,
                language=None,
                drafts=drafts("x"),
                vectors=[hash_vector("x")],
            )
        await s.rollback()
        # and B can neither remove, delete nor touch it
        assert await repo.mark_removed(s, tenant_b, [sid]) == 0
        assert await repo.delete_sources(s, tenant_b, [sid]) == 0
        await repo.touch_source(s, tenant_b, sid)
        await s.commit()
    async with sessionmaker() as s:
        [src] = await repo.list_sources(s, tenant_a)
        assert src.status == "active" and src.last_ingested_at is not None
        assert (
            src.last_ingested_at
            == (
                await s.execute(select(Source.last_ingested_at).where(Source.id == sid))
            ).scalar_one()
        )
        assert await repo.count_chunks(s, tenant_a, sid) == 1


async def test_same_url_in_two_tenants_stays_separate(
    sessionmaker: SM, tenant_a: uuid.UUID, tenant_b: uuid.UUID
) -> None:
    sa = await add_source(sessionmaker, tenant_a, "https://x.fi/a", "A:n sisältö")
    sb = await add_source(sessionmaker, tenant_b, "https://x.fi/a", "B:n sisältö")
    assert sa != sb
    async with sessionmaker() as s:
        a_chunks = (
            await s.execute(select(Chunk.content).where(Chunk.tenant_id == tenant_a))
        ).scalars()
        b_chunks = (
            await s.execute(select(Chunk.content).where(Chunk.tenant_id == tenant_b))
        ).scalars()
        assert list(a_chunks) == ["A:n sisältö"] and list(b_chunks) == ["B:n sisältö"]
