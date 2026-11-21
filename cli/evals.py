"""`bv evals run`: golden-set evaluation of the real assistant pipeline (spec §7.3)."""

import json
import uuid
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError
from sqlalchemy import func, select

from agents.assistant.factory import make_retriever
from cli import deps
from cli.deps import SessionMaker, db_now, load_tenant
from cli.tenants import _fail, run_with_sessionmaker
from core.db.models import UsageEvent
from core.tenants.config import TenantConfig
from evals.cases import EvalCase, load_cases
from evals.judge import DEFAULT_JUDGE_MODEL
from evals.report import calibrate, format_calibration, format_summary, summarize
from evals.runner import RunRecord, run_evals

app = typer.Typer(help="Evaluate the assistant against a golden set", no_args_is_help=True)

DEFAULT_DIR = Path("evals")


async def _cost_since(sessionmaker: SessionMaker, tenant_id: uuid.UUID, since: datetime) -> Decimal:
    async with sessionmaker() as s:
        total = await s.execute(
            select(func.coalesce(func.sum(UsageEvent.cost_eur), 0)).where(
                UsageEvent.tenant_id == tenant_id, UsageEvent.created_at >= since
            )
        )
        return Decimal(total.scalar_one())


def _with_model(cfg: TenantConfig, model: str, min_relevance: float) -> TenantConfig:
    retrieval = cfg.assistant.retrieval.model_copy(update={"min_relevance": min_relevance})
    assistant = cfg.assistant.model_copy(update={"model": model, "retrieval": retrieval})
    return cfg.model_copy(update={"assistant": assistant})


@app.command("run")
def run(
    slug: Annotated[str, typer.Argument(help="Tenant slug")],
    file: Annotated[
        Path | None,
        typer.Option(
            "--file", exists=True, dir_okay=False, help="Eval YAML (default evals/<slug>.yaml)"
        ),
    ] = None,
    model: Annotated[
        list[str] | None,
        typer.Option(
            "--model", help="Answer/understand model; repeat to compare (default: tenant's)"
        ),
    ] = None,
    repeat: Annotated[int, typer.Option("--repeat", min=1, max=20, help="Runs per case")] = 1,
    judge_model: Annotated[str, typer.Option("--judge-model")] = DEFAULT_JUDGE_MODEL,
    concurrency: Annotated[
        int, typer.Option("--concurrency", min=1, max=8, help="Parallel runs (1 = honest TTFT)")
    ] = 1,
    only: Annotated[
        list[str] | None, typer.Option("--only", help="Run only these case ids")
    ] = None,
    json_out: Annotated[
        Path | None, typer.Option("--json", help="Write all run records to this file")
    ] = None,
) -> None:
    """Run the golden cases through the real pipeline: pass/fail, TTFT, cost, calibration.

    The relevance gate is off during the run; pass rates at the tenant's configured
    min_relevance (and at every other threshold) are simulated from the recorded relevance.
    Exits 1 if any model is below target (>= 90 % pass, 100 % injection cases).
    """
    path = file or DEFAULT_DIR / f"{slug}.yaml"
    if not path.exists():
        raise _fail(f"No eval file at {path}")
    try:
        cases: list[EvalCase] = load_cases(path)
    except (ValidationError, ValueError) as e:
        raise _fail(f"Invalid eval file {path}:\n{e}") from e
    if only:
        unknown = sorted(set(only) - {c.id for c in cases})
        if unknown:
            raise _fail(f"Unknown case ids: {unknown}")
        cases = [c for c in cases if c.id in only]

    async def go(sessionmaker: SessionMaker) -> bool:
        async with sessionmaker() as session:
            tenant_id, cfg = await load_tenant(session, slug)
        llm = deps.DEPS.make_llm(sessionmaker)
        retriever = make_retriever(sessionmaker, deps.DEPS.make_embedder(sessionmaker))
        configured = cfg.assistant.retrieval.min_relevance
        models = model or [cfg.assistant.model]

        all_records: list[RunRecord] = []
        ok = True
        for m in models:
            typer.echo(f"\nRunning {len(cases)} cases x {repeat} with {m} ...")
            since = await db_now(sessionmaker)

            def progress(rec: RunRecord) -> None:
                ttft = "-" if rec.ttft_s is None else f"{rec.ttft_s:.1f}s"
                typer.secho(f"  {rec.case_id:<28} {rec.behaviour:<10} ttft {ttft}", dim=True)

            records = await run_evals(
                cases,
                llm=llm,
                retriever=retriever,
                tenant_id=tenant_id,
                cfg=_with_model(cfg, m, 0.0),
                repeat=repeat,
                judge_model=judge_model,
                concurrency=concurrency,
                on_record=progress,
            )
            cost = await _cost_since(sessionmaker, tenant_id, since)
            summary = summarize(cases, records, m, configured)
            typer.echo("\n" + format_summary(summary, float(cost)))
            ok = ok and summary.meets_targets
            all_records += records

        typer.echo("\n" + format_calibration(calibrate(cases, all_records), configured))
        if json_out is not None:
            json_out.write_text(
                json.dumps([asdict(r) for r in all_records], ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
            typer.echo(f"\nRecords written to {json_out}")
        return ok

    if not run_with_sessionmaker(go):
        raise typer.Exit(code=1)
