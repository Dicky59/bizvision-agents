"""`bv chat`: terminal chat through the real ChatService (same graph, guards and persistence the
API will use). Conversations are stored like any other (visitor id `cli-...`), so they show up in
usage and are removed by the retention job."""

import time
import uuid
from decimal import Decimal
from typing import Annotated

import typer
from sqlalchemy import func, select

from agents.assistant.factory import make_chat_service
from agents.assistant.types import Debug, Done, Error, LeadForm, Meta, Sources, Token
from cli import deps
from cli.deps import SessionMaker, load_tenant
from cli.tenants import _fail, run_with_sessionmaker
from core.db.models import UsageEvent
from core.tenants.config import Lang

QUIT = {"", "/quit", "/exit", "/q"}


async def _conversation_cost(
    sessionmaker: SessionMaker, tenant_id: uuid.UUID, conversation_id: uuid.UUID
) -> Decimal:
    async with sessionmaker() as s:
        total = await s.execute(
            select(func.coalesce(func.sum(UsageEvent.cost_eur), 0)).where(
                UsageEvent.tenant_id == tenant_id, UsageEvent.conversation_id == conversation_id
            )
        )
        return Decimal(total.scalar_one())


def chat(
    slug: Annotated[str, typer.Argument(help="Tenant slug")],
    lang: Annotated[
        str | None, typer.Option("--lang", help="Force the language: fi or en (default: detected)")
    ] = None,
    show_debug: Annotated[
        bool,
        typer.Option("--show-debug", help="Print intent, search query, relevance, timings"),
    ] = False,
) -> None:
    """Chat with the tenant's assistant in the terminal (empty line or /quit to leave)."""
    if lang is not None and lang not in ("fi", "en"):
        raise _fail("--lang must be 'fi' or 'en'")
    forced: Lang | None = None if lang is None else ("fi" if lang == "fi" else "en")

    async def run(sessionmaker: SessionMaker) -> None:
        async with sessionmaker() as session:
            tenant_id, cfg = await load_tenant(session, slug)
        service = make_chat_service(
            deps.DEPS.make_llm(sessionmaker), deps.DEPS.make_embedder(sessionmaker), sessionmaker
        )
        visitor_id = f"cli-{uuid.uuid4().hex[:12]}"
        conversation_id: uuid.UUID | None = None

        greeting = cfg.assistant.greeting.get(forced or cfg.default_language)
        typer.secho(f"{cfg.name} assistant (empty line or /quit to leave)", dim=True)
        if greeting:
            typer.echo(f"bot> {greeting}")

        while True:
            try:
                message = typer.prompt("you", default="", show_default=False, prompt_suffix="> ")
            except typer.Abort:
                break
            if message.strip().lower() in QUIT:
                break

            started = time.perf_counter()
            first_token: float | None = None
            typer.echo("bot> ", nl=False)
            async for ev in service.chat(
                tenant_id=tenant_id,
                cfg=cfg,
                visitor_id=visitor_id,
                message=message.strip(),
                conversation_id=conversation_id,
                language=forced,
            ):
                if isinstance(ev, Meta):
                    conversation_id = ev.conversation_id
                elif isinstance(ev, Token):
                    if first_token is None:
                        first_token = time.perf_counter() - started
                    typer.echo(ev.text, nl=False)
                elif isinstance(ev, Sources):
                    typer.echo("")
                    for src in ev.items:
                        typer.secho(f"  source: {src.title} - {src.url}", dim=True)
                elif isinstance(ev, LeadForm):
                    typer.secho(f"\n  [lead form would be shown: {ev.reason}]", fg="yellow")
                elif isinstance(ev, Error):
                    typer.secho(f"\n  [error {ev.code}] {ev.message}", fg="red")
                elif isinstance(ev, Debug) and show_debug:
                    typer.secho(
                        "\n  . " + "  ".join(f"{k}={v!r}" for k, v in ev.data.items()),
                        dim=True,
                    )
                elif isinstance(ev, Done):
                    typer.echo("")
                    if show_debug:
                        total = time.perf_counter() - started
                        ttft = "-" if first_token is None else f"{first_token:.2f}s"
                        typer.secho(
                            f"  . intent={ev.intent} answerable={ev.answerable} "
                            f"header_failed={ev.header_failed} ttft={ttft} total={total:.2f}s",
                            dim=True,
                        )

        if conversation_id is not None:
            cost = await _conversation_cost(sessionmaker, tenant_id, conversation_id)
            typer.echo(f"Session cost: EUR {cost:.6f}")

    run_with_sessionmaker(run)
