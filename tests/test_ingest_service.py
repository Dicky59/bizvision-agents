import uuid

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.db.models import Chunk, IngestJob, Source, UsageEvent
from core.embeddings.client import EmbeddingClient
from core.ingest import repository as repo
from core.ingest.errors import IngestError, UnsupportedFile
from core.ingest.service import (
    FaqEntry,
    IngestStats,
    crawl_tenant,
    ingest_file,
    replace_faq,
)
from core.tenants.config import IngestConfig
from tests.fakes import make_embedder
from tests.test_crawler import Site, html_page, resolve_public, sitemap
from tests.test_ingest_files import FI_TEXT, make_pdf

SM = async_sessionmaker[AsyncSession]
BASE = "https://example.com"


def site_with(*paths: str, **overrides: str) -> Site:
    """A sitemap site: every path is a simple Finnish page; `overrides` replaces page HTML."""
    routes: dict[str, httpx.Response | str] = {
        "/sitemap.xml": sitemap(*(f"{BASE}{p}" for p in paths))
    }
    for p in paths:
        routes[p] = overrides.get(p, html_page(p.strip("/") or "Etusivu"))
    return Site(routes)


async def crawl_site(
    sm: SM, embedder: EmbeddingClient, tenant_id: uuid.UUID, site: Site, **cfg: object
) -> IngestStats:
    cfg.setdefault("start_urls", [BASE])
    config = IngestConfig.model_validate(cfg)
    async with httpx.AsyncClient(transport=httpx.MockTransport(site.handler)) as client:
        return await crawl_tenant(
            sm, embedder, tenant_id, config, client, resolver=resolve_public, delay=0
        )


async def sources(sm: SM, tenant_id: uuid.UUID) -> dict[str, Source]:
    async with sm() as s:
        return {x.uri or x.filename or "": x for x in await repo.list_sources(s, tenant_id)}


async def chunk_ids(sm: SM, tenant_id: uuid.UUID, source_id: uuid.UUID) -> set[uuid.UUID]:
    async with sm() as s:
        rows = await s.execute(
            select(Chunk.id).where(Chunk.tenant_id == tenant_id, Chunk.source_id == source_id)
        )
        return set(rows.scalars())


async def usage_count(sm: SM, tenant_id: uuid.UUID) -> int:
    async with sm() as s:
        q = select(func.count()).select_from(UsageEvent).where(UsageEvent.tenant_id == tenant_id)
        return (await s.execute(q)).scalar_one()


# ------------------------------------------------------------------ crawl


async def test_first_crawl_indexes_pages_and_records_job_and_usage(
    sessionmaker: SM, tenant_id: uuid.UUID
) -> None:
    embedder, provider = make_embedder(sessionmaker)
    stats = await crawl_site(sessionmaker, embedder, tenant_id, site_with("/", "/palvelut"))

    assert (stats.new, stats.updated, stats.unchanged, stats.errors) == (2, 0, 0, 0)
    srcs = await sources(sessionmaker, tenant_id)
    assert set(srcs) == {f"{BASE}/", f"{BASE}/palvelut"}
    assert all(
        s.type == "web" and s.status == "active" and s.language == "fi" for s in srcs.values()
    )
    assert all(s.content_hash and s.last_ingested_at for s in srcs.values())
    async with sessionmaker() as s:
        assert await repo.count_chunks(s, tenant_id) == stats.chunks > 0
        # tenant-scoped usage: one 'embed' row per embedding call, with a real cost
        events = (
            (await s.execute(select(UsageEvent).where(UsageEvent.tenant_id == tenant_id)))
            .scalars()
            .all()
        )
        assert len(events) == len(provider.calls) == 2
        assert {e.operation for e in events} == {"embed"}
        assert all(e.cost_eur > 0 for e in events)
        job = (
            await s.execute(select(IngestJob).where(IngestJob.tenant_id == tenant_id))
        ).scalar_one()
        assert (job.kind, job.status, job.error) == ("crawl", "done", None)
        assert job.stats["new"] == 2 and job.stats["errors"] == 0


async def test_rerun_skips_unchanged_pages_without_embedding(
    sessionmaker: SM, tenant_id: uuid.UUID
) -> None:
    embedder, provider = make_embedder(sessionmaker)
    site = site_with("/", "/a", "/b")
    await crawl_site(sessionmaker, embedder, tenant_id, site)
    seen_before = await sources(sessionmaker, tenant_id)
    calls, usage = len(provider.calls), await usage_count(sessionmaker, tenant_id)

    stats = await crawl_site(sessionmaker, embedder, tenant_id, site)

    assert (stats.new, stats.updated, stats.unchanged, stats.removed) == (0, 0, 3, 0)
    assert len(provider.calls) == calls  # nothing re-embedded
    assert await usage_count(sessionmaker, tenant_id) == usage  # and no new cost rows
    seen_after = await sources(sessionmaker, tenant_id)
    for k, old in seen_before.items():  # but the pages were seen again
        t_old, t_new = old.last_ingested_at, seen_after[k].last_ingested_at
        assert t_old is not None and t_new is not None and t_new > t_old
    assert {s.id for s in seen_after.values()} == {s.id for s in seen_before.values()}


async def test_changed_page_gets_new_chunks(sessionmaker: SM, tenant_id: uuid.UUID) -> None:
    embedder, _ = make_embedder(sessionmaker)
    await crawl_site(sessionmaker, embedder, tenant_id, site_with("/", "/a"))
    before = await sources(sessionmaker, tenant_id)
    old_a = await chunk_ids(sessionmaker, tenant_id, before[f"{BASE}/a"].id)
    old_root = await chunk_ids(sessionmaker, tenant_id, before[f"{BASE}/"].id)

    changed = site_with("/", "/a", **{"/a": html_page("a", extra="<p>Uusi kappale sivulla.</p>")})
    stats = await crawl_site(sessionmaker, embedder, tenant_id, changed)

    assert (stats.new, stats.updated, stats.unchanged) == (0, 1, 1)
    after = await sources(sessionmaker, tenant_id)
    assert after[f"{BASE}/a"].id == before[f"{BASE}/a"].id  # same source row
    assert after[f"{BASE}/a"].content_hash != before[f"{BASE}/a"].content_hash
    new_a = await chunk_ids(sessionmaker, tenant_id, after[f"{BASE}/a"].id)
    assert new_a and not (new_a & old_a)  # replaced, not appended
    assert await chunk_ids(sessionmaker, tenant_id, after[f"{BASE}/"].id) == old_root


async def test_page_missing_from_site_is_removed_with_its_chunks(
    sessionmaker: SM, tenant_id: uuid.UUID
) -> None:
    embedder, _ = make_embedder(sessionmaker)
    await crawl_site(sessionmaker, embedder, tenant_id, site_with("/", "/a", "/b"))

    stats = await crawl_site(sessionmaker, embedder, tenant_id, site_with("/", "/a"))

    assert (stats.unchanged, stats.removed) == (2, 1)
    srcs = await sources(sessionmaker, tenant_id)
    assert srcs[f"{BASE}/b"].status == "removed"
    assert await chunk_ids(sessionmaker, tenant_id, srcs[f"{BASE}/b"].id) == set()
    assert srcs[f"{BASE}/a"].status == "active"

    # the page comes back: reactivated and re-embedded, not duplicated
    stats = await crawl_site(sessionmaker, embedder, tenant_id, site_with("/", "/a", "/b"))
    assert (stats.new, stats.unchanged) == (1, 2)
    again = await sources(sessionmaker, tenant_id)
    assert again[f"{BASE}/b"].id == srcs[f"{BASE}/b"].id and again[f"{BASE}/b"].status == "active"


async def test_http_404_removes_but_5xx_keeps_existing_content(
    sessionmaker: SM, tenant_id: uuid.UUID
) -> None:
    embedder, _ = make_embedder(sessionmaker)
    site = site_with("/", "/gone", "/flaky")
    await crawl_site(sessionmaker, embedder, tenant_id, site)

    site.routes["/gone"] = httpx.Response(404)
    site.routes["/flaky"] = httpx.Response(503)
    stats = await crawl_site(sessionmaker, embedder, tenant_id, site)

    assert stats.errors == 2 and stats.removed == 1
    srcs = await sources(sessionmaker, tenant_id)
    assert srcs[f"{BASE}/gone"].status == "removed"
    assert srcs[f"{BASE}/flaky"].status == "active"
    assert await chunk_ids(sessionmaker, tenant_id, srcs[f"{BASE}/flaky"].id)
    assert any("HTTP 503" in e for e in stats.error_samples)


async def test_truncated_crawl_never_removes(sessionmaker: SM, tenant_id: uuid.UUID) -> None:
    embedder, _ = make_embedder(sessionmaker)
    site = site_with("/", "/a", "/b")
    await crawl_site(sessionmaker, embedder, tenant_id, site)

    stats = await crawl_site(sessionmaker, embedder, tenant_id, site, max_pages=1)

    assert stats.removed == 0 and stats.removal_skipped and "max_pages" in stats.removal_skipped
    assert {s.status for s in (await sources(sessionmaker, tenant_id)).values()} == {"active"}


async def test_outage_does_not_wipe_the_index(sessionmaker: SM, tenant_id: uuid.UUID) -> None:
    embedder, _ = make_embedder(sessionmaker)
    await crawl_site(sessionmaker, embedder, tenant_id, site_with("/", "/a"))

    down = Site({"/robots.txt": httpx.Response(503)})  # robots 5xx -> nothing may be crawled
    stats = await crawl_site(sessionmaker, embedder, tenant_id, down)

    assert stats.succeeded == 0 and stats.removed == 0 and stats.removal_skipped
    assert {s.status for s in (await sources(sessionmaker, tenant_id)).values()} == {"active"}
    async with sessionmaker() as s:
        jobs = (
            (
                await s.execute(
                    select(IngestJob)
                    .where(IngestJob.tenant_id == tenant_id)
                    .order_by(IngestJob.created_at)
                )
            )
            .scalars()
            .all()
        )
        assert [j.status for j in jobs] == ["done", "failed"]
        assert jobs[1].error == "no pages ingested"


async def test_failed_embedding_keeps_old_chunks_and_old_hash(
    sessionmaker: SM, tenant_id: uuid.UUID
) -> None:
    embedder, provider = make_embedder(sessionmaker)
    await crawl_site(sessionmaker, embedder, tenant_id, site_with("/", "/a"))
    before = await sources(sessionmaker, tenant_id)
    old_chunks = await chunk_ids(sessionmaker, tenant_id, before[f"{BASE}/a"].id)

    provider.fail_with = RuntimeError("embedding provider down")
    changed = site_with("/", "/a", **{"/a": html_page("a", extra="<p>Muuttunut sisältö.</p>")})
    stats = await crawl_site(sessionmaker, embedder, tenant_id, changed)

    assert stats.errors == 1 and stats.unchanged == 1 and stats.removed == 0
    assert "RuntimeError" in stats.error_samples[0]
    after = await sources(sessionmaker, tenant_id)
    assert after[f"{BASE}/a"].status == "active"
    assert after[f"{BASE}/a"].content_hash == before[f"{BASE}/a"].content_hash  # retried next run
    assert await chunk_ids(sessionmaker, tenant_id, after[f"{BASE}/a"].id) == old_chunks

    provider.fail_with = None  # recovery: next run picks the change up
    stats = await crawl_site(sessionmaker, embedder, tenant_id, changed)
    assert (stats.updated, stats.errors) == (1, 0)


async def test_failed_embedding_on_new_page_leaves_no_source(
    sessionmaker: SM, tenant_id: uuid.UUID
) -> None:
    embedder, provider = make_embedder(sessionmaker)
    provider.fail_with = RuntimeError("down")
    stats = await crawl_site(sessionmaker, embedder, tenant_id, site_with("/"))
    assert stats.errors == 1 and stats.succeeded == 0
    assert await sources(sessionmaker, tenant_id) == {}


async def test_linked_pdf_becomes_a_web_source(sessionmaker: SM, tenant_id: uuid.UUID) -> None:
    embedder, _ = make_embedder(sessionmaker)
    site = site_with("/", **{"/": html_page("Etusivu", links=("/esite.pdf",))})
    site.routes["/esite.pdf"] = httpx.Response(
        200,
        content=make_pdf("Hinnasto ja palvelut", title="Esite"),
        headers={"content-type": "application/pdf"},
    )
    stats = await crawl_site(sessionmaker, embedder, tenant_id, site)

    assert stats.new == 2
    pdf = (await sources(sessionmaker, tenant_id))[f"{BASE}/esite.pdf"]
    assert (pdf.type, pdf.title, pdf.status) == ("web", "Esite", "active")
    # and the "removed" logic covers it like any page
    del site.routes["/esite.pdf"]
    site.routes["/"] = html_page("Etusivu")
    stats = await crawl_site(sessionmaker, embedder, tenant_id, site)
    assert stats.removed == 1
    assert (await sources(sessionmaker, tenant_id))[f"{BASE}/esite.pdf"].status == "removed"


async def test_crawl_of_one_tenant_never_touches_another(
    sessionmaker: SM, tenant_id: uuid.UUID, other_tenant_id: uuid.UUID
) -> None:
    embedder, _ = make_embedder(sessionmaker)
    await crawl_site(sessionmaker, embedder, tenant_id, site_with("/", "/a", "/b"))
    await crawl_site(sessionmaker, embedder, other_tenant_id, site_with("/", "/a", "/b"))
    mine = await sources(sessionmaker, tenant_id)
    theirs = await sources(sessionmaker, other_tenant_id)
    assert {s.id for s in mine.values()}.isdisjoint({s.id for s in theirs.values()})

    # the other tenant's site shrinks: only *their* page is removed
    stats = await crawl_site(sessionmaker, embedder, other_tenant_id, site_with("/", "/a"))
    assert stats.removed == 1
    assert {s.status for s in (await sources(sessionmaker, tenant_id)).values()} == {"active"}
    async with sessionmaker() as s:
        assert await repo.count_chunks(s, tenant_id) > 0
    # usage rows are attributed to the right tenant (3 pages each, one embed call per page)
    assert await usage_count(sessionmaker, tenant_id) == 3
    assert await usage_count(sessionmaker, other_tenant_id) == 3


# ------------------------------------------------------------------ files


async def test_file_ingest_new_unchanged_updated(sessionmaker: SM, tenant_id: uuid.UUID) -> None:
    embedder, provider = make_embedder(sessionmaker)
    data = f"# Hinnasto\n\n{FI_TEXT}\n".encode()

    s1 = await ingest_file(sessionmaker, embedder, tenant_id, "hinnasto.md", data)
    assert s1.new == 1 and s1.chunks > 0
    src = (await sources(sessionmaker, tenant_id))["hinnasto.md"]
    assert (src.type, src.uri, src.filename, src.title, src.language) == (
        "file",
        None,
        "hinnasto.md",
        "Hinnasto",
        "fi",
    )
    calls = len(provider.calls)

    s2 = await ingest_file(sessionmaker, embedder, tenant_id, "hinnasto.md", data)
    assert s2.unchanged == 1 and len(provider.calls) == calls

    s3 = await ingest_file(sessionmaker, embedder, tenant_id, "hinnasto.md", data + b"\nLisaa.\n")
    assert s3.updated == 1
    async with sessionmaker() as s:
        kinds = (
            await s.execute(select(IngestJob.kind).where(IngestJob.tenant_id == tenant_id))
        ).scalars()
        assert list(kinds) == ["file", "file", "file"]


async def test_bad_file_raises_and_fails_the_job(sessionmaker: SM, tenant_id: uuid.UUID) -> None:
    embedder, provider = make_embedder(sessionmaker)
    with pytest.raises(UnsupportedFile):
        await ingest_file(sessionmaker, embedder, tenant_id, "broken.pdf", b"nope")
    assert provider.calls == [] and await sources(sessionmaker, tenant_id) == {}
    async with sessionmaker() as s:
        job = (
            await s.execute(select(IngestJob).where(IngestJob.tenant_id == tenant_id))
        ).scalar_one()
        assert job.status == "failed" and job.error


async def test_same_filename_in_two_tenants_is_independent(
    sessionmaker: SM, tenant_id: uuid.UUID, other_tenant_id: uuid.UUID
) -> None:
    embedder, _ = make_embedder(sessionmaker)
    await ingest_file(sessionmaker, embedder, tenant_id, "a.txt", FI_TEXT.encode())
    await ingest_file(sessionmaker, embedder, other_tenant_id, "a.txt", FI_TEXT.encode() + b" Eri.")
    a = await sources(sessionmaker, tenant_id)
    b = await sources(sessionmaker, other_tenant_id)
    assert a["a.txt"].id != b["a.txt"].id and a["a.txt"].content_hash != b["a.txt"].content_hash


# ------------------------------------------------------------------ FAQ


FAQ = [
    FaqEntry("Paljonko AI-asiakaspalvelija maksaa?", "Hinta riippuu laajuudesta."),
    FaqEntry("Miten projekti etenee?", "Aloitamme kartoituksella."),
]


async def test_faq_import_is_idempotent_and_single_chunk(
    sessionmaker: SM, tenant_id: uuid.UUID
) -> None:
    embedder, provider = make_embedder(sessionmaker)
    long_answer = "Pitkä vastaus. " * 400  # far above the chunk target; still one chunk
    entries = [*FAQ, FaqEntry("Pitkä kysymys?", long_answer)]
    s1 = await replace_faq(sessionmaker, embedder, tenant_id, entries)
    assert (s1.new, s1.chunks) == (3, 3)
    srcs = await sources(sessionmaker, tenant_id)
    assert all(k.startswith("faq:") and s.type == "faq" for k, s in srcs.items())
    async with sessionmaker() as s:
        contents = (
            await s.execute(select(Chunk.content).where(Chunk.tenant_id == tenant_id))
        ).scalars()
        assert any(c.startswith("Paljonko AI-asiakaspalvelija maksaa?\n\nHinta") for c in contents)

    calls = len(provider.calls)
    s2 = await replace_faq(sessionmaker, embedder, tenant_id, entries)
    assert (s2.unchanged, s2.new, s2.updated) == (3, 0, 0) and len(provider.calls) == calls


async def test_faq_replace_updates_changed_and_deletes_absent(
    sessionmaker: SM, tenant_id: uuid.UUID
) -> None:
    embedder, _ = make_embedder(sessionmaker)
    await replace_faq(sessionmaker, embedder, tenant_id, FAQ)
    before = await sources(sessionmaker, tenant_id)

    new_list = [FaqEntry("  paljonko  AI-asiakaspalvelija maksaa? ", "Hinta alkaa 990 eurosta.")]
    stats = await replace_faq(sessionmaker, embedder, tenant_id, new_list)

    assert (stats.updated, stats.removed) == (1, 1)  # same question (normalised) -> same source
    after = await sources(sessionmaker, tenant_id)
    assert len(after) == 1 and set(after) <= set(before)
    async with sessionmaker() as s:  # deleted entries leave no rows or chunks behind
        assert await repo.count_chunks(s, tenant_id) == 1


async def test_faq_validation_happens_before_any_change(
    sessionmaker: SM, tenant_id: uuid.UUID
) -> None:
    embedder, provider = make_embedder(sessionmaker)
    await replace_faq(sessionmaker, embedder, tenant_id, FAQ)
    calls = len(provider.calls)
    for bad in (
        [FAQ[0], FaqEntry(FAQ[0].question.upper(), "kaksoiskappale")],
        [FaqEntry("Kysymys?", "  ")],
        [FaqEntry("K?", "x" * 21_000)],
    ):
        with pytest.raises(IngestError):
            await replace_faq(sessionmaker, embedder, tenant_id, bad)
    assert len(provider.calls) == calls and len(await sources(sessionmaker, tenant_id)) == 2


async def test_faq_embedding_failure_deletes_nothing(
    sessionmaker: SM, tenant_id: uuid.UUID
) -> None:
    embedder, provider = make_embedder(sessionmaker)
    await replace_faq(sessionmaker, embedder, tenant_id, FAQ)
    provider.fail_with = RuntimeError("down")
    stats = await replace_faq(
        sessionmaker, embedder, tenant_id, [FaqEntry("Uusi kysymys?", "Uusi vastaus.")]
    )
    assert stats.errors == 1 and stats.removed == 0 and stats.removal_skipped
    assert len(await sources(sessionmaker, tenant_id)) == 2  # the old FAQ is still there


async def test_faq_is_isolated_between_tenants(
    sessionmaker: SM, tenant_id: uuid.UUID, other_tenant_id: uuid.UUID
) -> None:
    embedder, _ = make_embedder(sessionmaker)
    await replace_faq(sessionmaker, embedder, tenant_id, FAQ)
    await replace_faq(sessionmaker, embedder, other_tenant_id, FAQ[:1])
    await replace_faq(sessionmaker, embedder, other_tenant_id, [])  # clears only their FAQ
    assert len(await sources(sessionmaker, tenant_id)) == 2
    assert await sources(sessionmaker, other_tenant_id) == {}
