"""`bv leads ...` and `bv cleanup`: lead inspection, notification retry, erasure, retention."""

import typer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cli import deps
from cli.tenants import _fail, run_with_sessionmaker
from core.leads import repository as repo
from core.leads.service import cleanup_tenant, notify_pending
from core.tenants.config import TenantConfig
from core.tenants.repository import list_tenants

app = typer.Typer(help="Leads: list, retry notifications, erase", no_args_is_help=True)

SessionMaker = async_sessionmaker[AsyncSession]


@app.command("list")
def list_cmd(
    slug: str = typer.Argument(..., help="Tenant slug"),
    limit: int = typer.Option(20, min=1, max=500),
) -> None:
    """Newest leads first (personal data: shown only in this terminal)."""

    async def run(sm: SessionMaker) -> None:
        async with sm() as s:
            tenant_id, _ = await deps.load_tenant(s, slug)
            leads = await repo.list_leads(s, tenant_id, limit=limit)
        if not leads:
            typer.echo("No leads.")
        for lead in leads:
            sent = "sent" if lead.notified_at else "PENDING"
            typer.echo(f"{lead.created_at:%Y-%m-%d %H:%M}  {sent:8}  {lead.name} <{lead.email}>")

    run_with_sessionmaker(run)


@app.command("notify-pending")
def notify_pending_cmd(slug: str = typer.Argument(..., help="Tenant slug")) -> None:
    """Send the notification email for every lead that has none yet."""
    sender = deps.DEPS.make_sender()

    async def run(sm: SessionMaker) -> None:
        async with sm() as s:
            tenant_id, cfg = await deps.load_tenant(s, slug)
        res = await notify_pending(sm, sender, tenant_id, cfg)
        typer.echo(f"Sent {res.sent}, failed {res.failed}.")
        if res.failed:
            raise typer.Exit(code=1)

    run_with_sessionmaker(run)


@app.command("delete")
def delete_cmd(
    slug: str = typer.Argument(..., help="Tenant slug"),
    email: str = typer.Argument(..., help="Erase every lead with this address"),
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation"),
) -> None:
    """Erasure request: deletes the leads for this address and the conversations they came from."""
    if not yes:
        typer.confirm(f"Delete all leads (and linked conversations) for {email}?", abort=True)

    async def run(sm: SessionMaker) -> None:
        async with sm() as s:
            tenant_id, _ = await deps.load_tenant(s, slug)
        async with sm() as s, s.begin():
            erased = await repo.delete_by_email(s, tenant_id, email)
        typer.echo(f"Deleted {erased.leads} lead(s) and {erased.conversations} conversation(s).")

    run_with_sessionmaker(run)


def cleanup(
    slug: str | None = typer.Argument(None, help="Tenant slug (default: all tenants)"),
) -> None:
    """Retention job: delete expired conversations and leads older than lead_retention_days."""

    async def run(sm: SessionMaker) -> None:
        async with sm() as s:
            if slug is None:
                rows = await list_tenants(s)
                targets = [(t.id, TenantConfig.model_validate(t.config), t.slug) for t in rows]
            else:
                tenant_id, cfg = await deps.load_tenant(s, slug)
                targets = [(tenant_id, cfg, slug)]
        if not targets:
            raise _fail("No tenants.")
        for tenant_id, cfg, name in targets:
            res = await cleanup_tenant(sm, tenant_id, cfg)
            typer.echo(f"{name}: deleted {res.conversations} conversation(s), {res.leads} lead(s).")

    run_with_sessionmaker(run)
