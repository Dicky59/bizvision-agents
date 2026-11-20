import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from typer.testing import CliRunner

from cli.ingest import dry_run
from cli.main import app
from core.db.models import Chunk, Source, UsageEvent
from core.tenants.config import IngestConfig
from tests.test_crawler import Site, html_page, resolve_public, sitemap


async def test_dry_run_prints_url_language_chars_and_preview() -> None:
    site = Site(
        {
            "/sitemap.xml": sitemap("https://example.com/", "https://example.com/missing"),
            "/": html_page("Etusivu", links=("/esite.pdf",)),
            "/esite.pdf": httpx.Response(
                200, content=b"not a pdf", headers={"content-type": "application/pdf"}
            ),
        }
    )
    lines: list[str] = []
    config = IngestConfig(start_urls=["https://example.com"])
    async with httpx.AsyncClient(transport=httpx.MockTransport(site.handler)) as client:
        stats = await dry_run(config, client, lines.append, resolver=resolve_public, delay=0)

    out = "\n".join(lines)
    assert "[page] https://example.com/" in out
    assert "language: fi" in out and "chars: " in out
    assert "# Etusivu" in out  # preview shows extracted markdown start
    assert "[pdf] https://example.com/esite.pdf" in out and "cannot parse" in out
    assert "[failed] https://example.com/missing" in out and "HTTP 404" in out
    assert (stats.pages, stats.pdfs, stats.failures) == (1, 1, 1)


async def test_dry_run_preview_is_capped_at_300_chars() -> None:
    site = Site({"/": html_page("Etusivu")})
    lines: list[str] = []
    config = IngestConfig(start_urls=["https://example.com"], use_sitemap=False)
    async with httpx.AsyncClient(transport=httpx.MockTransport(site.handler)) as client:
        await dry_run(config, client, lines.append, resolver=resolve_public, delay=0)
    assert all(len(line) <= 310 for line in lines)


@pytest.mark.usefixtures("migrated_db")
def test_cli_requires_dry_run_flag() -> None:
    result = CliRunner().invoke(app, ["ingest", "whatever"])
    assert result.exit_code == 1
    assert "--dry-run" in result.output


@pytest.mark.usefixtures("migrated_db")
def test_cli_unknown_tenant_fails() -> None:
    result = CliRunner().invoke(app, ["ingest", "no-such-tenant-xyz", "--dry-run"])
    assert result.exit_code == 1
    assert "not found" in result.output


async def test_dry_run_writes_nothing_to_the_database(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async def counts() -> tuple[int, int, int]:
        async with sessionmaker() as s:
            result = []
            for model in (Source, Chunk, UsageEvent):
                result.append(
                    (await s.execute(select(func.count()).select_from(model))).scalar_one()
                )
            return result[0], result[1], result[2]

    before = await counts()
    site = Site({"/": html_page("Etusivu")})
    config = IngestConfig(start_urls=["https://example.com"], use_sitemap=False)
    async with httpx.AsyncClient(transport=httpx.MockTransport(site.handler)) as client:
        await dry_run(config, client, lambda _: None, resolver=resolve_public, delay=0)
    assert await counts() == before


async def test_dry_run_warns_when_crawl_is_truncated() -> None:
    routes: dict[str, httpx.Response | str] = {
        "/": html_page("Etusivu", links=tuple(f"/p{i}" for i in range(5)))
    }
    routes.update({f"/p{i}": html_page(f"P{i}") for i in range(5)})
    lines: list[str] = []
    config = IngestConfig(start_urls=["https://example.com"], use_sitemap=False, max_pages=2)
    async with httpx.AsyncClient(transport=httpx.MockTransport(Site(routes).handler)) as client:
        stats = await dry_run(config, client, lines.append, resolver=resolve_public, delay=0)
    assert stats.truncated
    assert (
        "WARNING: crawl truncated at 2 pages — removed pages won't be detected; "
        "consider raising ingest.max_pages"
    ) in "\n".join(lines)


async def test_dry_run_does_not_warn_when_everything_fits() -> None:
    lines: list[str] = []
    config = IngestConfig(start_urls=["https://example.com"], use_sitemap=False, max_pages=5)
    site = Site({"/": html_page("Etusivu")})
    async with httpx.AsyncClient(transport=httpx.MockTransport(site.handler)) as client:
        stats = await dry_run(config, client, lines.append, resolver=resolve_public, delay=0)
    assert not stats.truncated and "WARNING" not in "\n".join(lines)
