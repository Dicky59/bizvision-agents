"""`bv ingest`: currently only `--dry-run` (crawl + extract, no embeddings, no DB writes)."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated

import httpx
import typer
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from cli.tenants import _fail, run_with_session
from core.ingest.crawler import (
    DEFAULT_DELAY,
    CrawledPage,
    CrawledPdf,
    CrawlFailure,
    crawl,
    make_client,
    preview,
)
from core.ingest.errors import IngestError
from core.ingest.files import parse_file
from core.ingest.safety import Resolver, system_resolver
from core.tenants.config import IngestConfig, TenantConfig
from core.tenants.repository import get_by_slug


@dataclass
class DryRunStats:
    pages: int = 0
    pdfs: int = 0
    failures: int = 0
    chars: int = 0


async def dry_run(
    config: IngestConfig,
    client: httpx.AsyncClient,
    echo: Callable[[str], None],
    *,
    resolver: Resolver = system_resolver,
    delay: float = DEFAULT_DELAY,
) -> DryRunStats:
    """Crawl and print per page: URL, language, extracted chars, first ~300 chars."""
    stats = DryRunStats()
    async for item in crawl(config, client, resolver=resolver, delay=delay):
        if isinstance(item, CrawledPage):
            stats.pages += 1
            stats.chars += len(item.text)
            echo(f"\n[page] {item.url}")
            if item.fetched_url != item.url:
                echo(f"  fetched as: {item.fetched_url}")
            echo(f"  title: {item.title or '(none)'}")
            echo(f"  language: {item.language or '?'}   chars: {len(item.text)}")
            echo(f"  {preview(item.text)}")
        elif isinstance(item, CrawledPdf):
            stats.pdfs += 1
            echo(f"\n[pdf] {item.url}")
            try:
                doc = parse_file("linked.pdf", item.data)
            except IngestError as e:
                echo(f"  cannot parse: {e}")
                continue
            stats.chars += len(doc.text)
            echo(f"  title: {doc.title}")
            echo(f"  language: {doc.language or '?'}   chars: {len(doc.text)}")
            echo(f"  {preview(doc.text)}")
        elif isinstance(item, CrawlFailure):
            stats.failures += 1
            echo(f"\n[failed] {item.url}")
            echo(f"  {item.reason}")
    return stats


def ingest(
    slug: Annotated[str, typer.Argument(help="Tenant slug")],
    dry_run_: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Crawl and print extraction results only (no embeddings, no DB writes)",
        ),
    ] = False,
) -> None:
    """Ingest a tenant's website content (full ingest arrives in a later step)."""
    if not dry_run_:
        raise _fail("Only --dry-run is implemented so far; re-run with --dry-run.")

    async def load(session: AsyncSession) -> TenantConfig | None:
        tenant = await get_by_slug(session, slug)
        return TenantConfig.model_validate(tenant.config) if tenant else None

    try:
        cfg = run_with_session(load)
    except ValidationError as e:
        raise _fail(f"Stored config for '{slug}' is invalid:\n{e}") from e
    if cfg is None:
        raise _fail(
            f"Tenant '{slug}' not found (run `bv tenants create {slug} --config ...` first)"
        )
    if not cfg.ingest.start_urls:
        raise _fail(f"Tenant '{slug}' has no ingest.start_urls configured")

    async def main() -> DryRunStats:
        async with make_client() as client:
            return await dry_run(cfg.ingest, client, typer.echo)

    typer.echo(
        f"Dry run for '{slug}': {', '.join(cfg.ingest.start_urls)} (no embeddings, no DB writes)"
    )
    stats = asyncio.run(main())
    typer.echo(
        f"\nDone: {stats.pages} pages, {stats.pdfs} PDFs, {stats.failures} failures, "
        f"{stats.chars} chars extracted."
    )
