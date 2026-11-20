"""`bv ingest`: crawl a tenant's site (or ingest local files) and index it.

`--dry-run` crawls and prints what would be extracted: no embeddings, no DB writes.
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import httpx
import typer

from cli import deps
from cli.deps import SessionMaker, db_now, echo_cost, embed_cost_since, load_tenant
from cli.tenants import _fail, run_with_session, run_with_sessionmaker
from core.ingest.crawler import (
    DEFAULT_DELAY,
    CrawledPage,
    CrawledPdf,
    CrawlFailure,
    CrawlReport,
    crawl,
    preview,
)
from core.ingest.errors import IngestError
from core.ingest.files import parse_file
from core.ingest.safety import Resolver, system_resolver
from core.ingest.service import IngestStats, crawl_tenant, ingest_file, truncation_warning
from core.tenants.config import IngestConfig

MAX_ERRORS_SHOWN = 5


@dataclass
class DryRunStats:
    pages: int = 0
    pdfs: int = 0
    failures: int = 0
    chars: int = 0
    truncated: bool = False


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
    report = CrawlReport()
    async for item in crawl(config, client, resolver=resolver, delay=delay, report=report):
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
    if report.truncated:
        stats.truncated = True
        warning = truncation_warning(report.attempted)
        echo(f"\nWARNING: {warning}")
    return stats


def _progress(outcome: str, name: str) -> None:
    typer.echo(f"  {outcome:<9} {name}")


def _echo_summary(stats: IngestStats) -> None:
    typer.echo(
        f"\n{stats.new} new, {stats.updated} updated, {stats.unchanged} unchanged, "
        f"{stats.removed} removed, {stats.errors} errors; {stats.chunks} chunks written"
    )
    for sample in stats.error_samples[:MAX_ERRORS_SHOWN]:
        typer.secho(f"  error: {sample}", fg=typer.colors.RED, err=True)
    if stats.errors > MAX_ERRORS_SHOWN:
        typer.echo(f"  ... and {stats.errors - MAX_ERRORS_SHOWN} more (see the job stats)")
    if stats.truncated and stats.removal_skipped:
        typer.secho(f"WARNING: {stats.removal_skipped}", fg=typer.colors.YELLOW)
    elif stats.removal_skipped:
        typer.secho(f"Note: removal skipped ({stats.removal_skipped})", fg=typer.colors.YELLOW)


async def _run_crawl(sessionmaker: SessionMaker, slug: str) -> bool:
    async with sessionmaker() as session:
        tenant_id, cfg = await load_tenant(session, slug)
    if not cfg.ingest.start_urls:
        raise _fail(f"Tenant '{slug}' has no ingest.start_urls configured")
    since = await db_now(sessionmaker)
    embedder = deps.DEPS.make_embedder(sessionmaker)
    typer.echo(f"Ingesting '{slug}' from {', '.join(cfg.ingest.start_urls)}")
    async with deps.DEPS.make_client() as client:
        stats = await crawl_tenant(
            sessionmaker,
            embedder,
            tenant_id,
            cfg.ingest,
            client,
            resolver=deps.DEPS.resolver,
            delay=deps.DEPS.delay,
            progress=_progress,
        )
    _echo_summary(stats)
    echo_cost(await embed_cost_since(sessionmaker, tenant_id, since))
    return stats.succeeded > 0


async def _run_files(sessionmaker: SessionMaker, slug: str, files: list[Path]) -> bool:
    async with sessionmaker() as session:
        tenant_id, _ = await load_tenant(session, slug)
    since = await db_now(sessionmaker)
    embedder = deps.DEPS.make_embedder(sessionmaker)
    ok = True
    total = IngestStats()
    for path in files:
        try:
            data = await asyncio.to_thread(path.read_bytes)
            stats = await ingest_file(sessionmaker, embedder, tenant_id, path.name, data)
        except (IngestError, OSError) as e:
            ok = False
            total.add_error(path.name, f"{type(e).__name__}: {e}")
            _progress("error", path.name)
            continue
        for field in ("new", "updated", "unchanged"):
            if getattr(stats, field):
                _progress(field, path.name)
        total.new += stats.new
        total.updated += stats.updated
        total.unchanged += stats.unchanged
        total.chunks += stats.chunks
    _echo_summary(total)
    echo_cost(await embed_cost_since(sessionmaker, tenant_id, since))
    return ok


def ingest(
    slug: Annotated[str, typer.Argument(help="Tenant slug")],
    files: Annotated[
        list[Path] | None,
        typer.Option(
            "--files",
            exists=True,
            dir_okay=False,
            readable=True,
            help="Ingest this file (PDF, DOCX, MD, TXT) instead of crawling; repeat for several",
        ),
    ] = None,
    dry_run_: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Crawl and print extraction results only (no embeddings, no DB writes)",
        ),
    ] = False,
) -> None:
    """Index a tenant's website (default) or local files. Re-runs skip unchanged content."""
    if dry_run_ and files:
        raise _fail("--dry-run only applies to crawling; don't combine it with --files")

    if dry_run_:

        async def load(session):  # type: ignore[no-untyped-def]
            return await load_tenant(session, slug)

        _, cfg = run_with_session(load)
        if not cfg.ingest.start_urls:
            raise _fail(f"Tenant '{slug}' has no ingest.start_urls configured")

        async def main() -> DryRunStats:
            async with deps.DEPS.make_client() as client:
                return await dry_run(
                    cfg.ingest,
                    client,
                    typer.echo,
                    resolver=deps.DEPS.resolver,
                    delay=deps.DEPS.delay,
                )

        typer.echo(
            f"Dry run for '{slug}': {', '.join(cfg.ingest.start_urls)} "
            "(no embeddings, no DB writes)"
        )
        stats = asyncio.run(main())
        typer.echo(
            f"\nDone: {stats.pages} pages, {stats.pdfs} PDFs, {stats.failures} failures, "
            f"{stats.chars} chars extracted."
        )
        return

    if files:
        ok = run_with_sessionmaker(lambda sm: _run_files(sm, slug, files))
    else:
        ok = run_with_sessionmaker(lambda sm: _run_crawl(sm, slug))
    if not ok:
        raise typer.Exit(code=1)
