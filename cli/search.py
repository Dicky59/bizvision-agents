"""`bv search`: run the real retrieval pipeline from the terminal (debugging / calibration)."""

from typing import Annotated

import typer

from cli import deps
from cli.deps import SessionMaker, load_tenant
from cli.tenants import _fail, run_with_sessionmaker
from core.ingest.crawler import preview
from core.ingest.language import detect_language
from core.rag.retrieve import best_relevance, retrieve


def search(
    slug: Annotated[str, typer.Argument(help="Tenant slug")],
    query: Annotated[str, typer.Argument(help="Question to search for")],
    lang: Annotated[
        str | None, typer.Option("--lang", help="Query language: fi or en (default: detected)")
    ] = None,
    expanded: Annotated[
        str | None,
        typer.Option(
            "--expanded",
            help="Expanded search query, as the assistant's understand step would produce it; "
            "QUERY is then the visitor's raw message (both are searched)",
        ),
    ] = None,
    top_k: Annotated[
        int | None, typer.Option("--top-k", min=1, max=20, help="Results (default: tenant config)")
    ] = None,
) -> None:
    """Show the chunks retrieval returns for QUERY, with relevance and the fused score."""
    if lang is not None and lang not in ("fi", "en"):
        raise _fail("--lang must be 'fi' or 'en'")

    async def run(sessionmaker: SessionMaker) -> None:
        async with sessionmaker() as session:
            tenant_id, cfg = await load_tenant(session, slug)
            language = lang or detect_language(query) or cfg.default_language
            k = top_k or cfg.assistant.retrieval.top_k
            embedder = deps.DEPS.make_embedder(sessionmaker)
            results = await retrieve(
                session,
                embedder,
                tenant_id,
                expanded or query,
                language,
                k,
                raw_query=query if expanded else None,
            )

        typer.echo(f"Query: {query!r}  (language={language}, top_k={k})")
        if expanded:
            typer.echo(f"Expanded: {expanded!r}")
        if not results:
            typer.echo("No results (is the tenant ingested?)")
            return
        for i, r in enumerate(results, start=1):
            kind = "faq" if r.is_faq else r.source_type
            where = r.url or r.title or "-"
            typer.echo(f"\n{i}. relevance {r.relevance:.3f}  score {r.score:.4f}  [{kind}] {where}")
            if r.heading_path:
                typer.echo(f"   {r.heading_path}")
            typer.echo(f"   {preview(r.content, 200)}")
        best = best_relevance(results)
        threshold = cfg.assistant.retrieval.min_relevance
        verdict = "answer" if best >= threshold else "don't know"
        typer.echo(f"\nBest relevance {best:.3f} vs min_relevance {threshold} -> {verdict}")

    run_with_sessionmaker(run)
