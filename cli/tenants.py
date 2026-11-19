import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import get_settings
from core.db.session import make_engine
from core.tenants.loader import load_yaml
from core.tenants.repository import get_by_slug, list_tenants, upsert_tenant

app = typer.Typer(help="Manage tenants", no_args_is_help=True)


def run_with_session[T](fn: Callable[[AsyncSession], Awaitable[T]]) -> T:
    """Run fn in a fresh event loop with its own engine (CLI commands are short-lived)."""

    async def main() -> T:
        engine = make_engine(get_settings().database_url)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                return await fn(session)
        finally:
            await engine.dispose()

    return asyncio.run(main())


def _fail(msg: str) -> typer.Exit:
    typer.secho(msg, fg=typer.colors.RED, err=True)
    return typer.Exit(code=1)


@app.command()
def create(
    slug: Annotated[str, typer.Argument(help="Tenant slug; must match the slug in the YAML")],
    config: Annotated[
        Path, typer.Option("--config", exists=True, dir_okay=False, help="Tenant YAML")
    ],
) -> None:
    """Create or update a tenant from a YAML config."""
    try:
        cfg = load_yaml(config)
    except (ValidationError, ValueError) as e:
        raise _fail(f"Invalid tenant config {config}:\n{e}") from e
    if cfg.slug != slug:
        raise _fail(f"Slug mismatch: argument is '{slug}' but {config} has slug '{cfg.slug}'")

    async def run(session: AsyncSession) -> tuple[str, bool]:
        tenant, created = await upsert_tenant(session, cfg)
        return tenant.public_key, created

    public_key, created = run_with_session(run)
    typer.echo(f"{'Created' if created else 'Updated'} tenant '{slug}' (public_key={public_key})")


@app.command()
def show(slug: str) -> None:
    """Show a tenant and its config."""

    async def run(session: AsyncSession) -> dict | None:
        t = await get_by_slug(session, slug)
        if t is None:
            return None
        return {"slug": t.slug, "status": t.status, "public_key": t.public_key, "config": t.config}

    data = run_with_session(run)
    if data is None:
        raise _fail(f"Tenant '{slug}' not found")
    typer.echo(json.dumps(data, indent=2, ensure_ascii=False))


@app.command("list")
def list_() -> None:
    """List tenants."""

    async def run(session: AsyncSession) -> list[tuple[str, str, str]]:
        return [(t.slug, t.status, t.name) for t in await list_tenants(session)]

    for slug, status, name in run_with_session(run):
        typer.echo(f"{slug}\t{status}\t{name}")
