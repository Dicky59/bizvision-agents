"""`bv usage <tenant> [--month YYYY-MM]`: cost and token totals for a month."""

from typing import Annotated

import typer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cli import deps
from cli.tenants import _fail, run_with_sessionmaker
from core.usage import month_bounds, monthly_usage


def usage(
    slug: Annotated[str, typer.Argument(help="Tenant slug")],
    month: Annotated[str | None, typer.Option(help="YYYY-MM (default: this month, UTC)")] = None,
) -> None:
    """Monthly usage per operation and model, with the tenant's budget for comparison."""
    try:
        month_bounds(month)
    except ValueError as e:
        raise _fail(str(e)) from e

    async def run(sm: async_sessionmaker[AsyncSession]) -> None:
        async with sm() as s:
            tenant_id, cfg = await deps.load_tenant(s, slug)
            u = await monthly_usage(s, tenant_id, month)
        typer.echo(f"Usage for '{slug}', {u.month} (UTC)")
        for r in u.rows:
            typer.echo(
                f"  {r.operation:<10} {r.model:<24} {r.events:>6} calls  "
                f"{r.input_tokens:>9} in  {r.output_tokens:>8} out  EUR {r.cost_eur:.4f}"
            )
        if not u.rows:
            typer.echo("  (no usage)")
        typer.echo(
            f"Total: EUR {u.cost_eur:.4f} of EUR {cfg.limits.monthly_budget_eur:.2f} budget "
            f"({u.input_tokens} in / {u.output_tokens} out tokens)"
        )

    run_with_sessionmaker(run)
