"""`bv faq import`: replace a tenant's FAQ from a YAML file.

File format (a list; `question`/`answer` are accepted as longer key names):

    - q: Paljonko AI-asiakaspalvelija maksaa?
      a: Hinta riippuu laajuudesta.
"""

from pathlib import Path
from typing import Annotated, Any

import typer
import yaml

from cli import deps
from cli.deps import SessionMaker, db_now, echo_cost, embed_cost_since, load_tenant
from cli.tenants import _fail, run_with_sessionmaker
from core.ingest.errors import IngestError
from core.ingest.service import FaqEntry, replace_faq

app = typer.Typer(help="Manage a tenant's FAQ", no_args_is_help=True)


def parse_faq_yaml(path: Path) -> list[FaqEntry]:
    try:
        data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError, UnicodeDecodeError) as e:
        raise ValueError(f"cannot read {path}: {e}") from e
    if isinstance(data, dict) and "faq" in data:
        data = data["faq"]
    if data is None:  # an empty file must not silently wipe the FAQ; write `[]` to clear it
        raise ValueError("file is empty (write `[]` if you really want to clear the FAQ)")
    if not isinstance(data, list):
        raise ValueError("expected a YAML list of {q, a} entries")
    entries: list[FaqEntry] = []
    for i, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"entry {i}: expected a mapping with q and a")
        q = item.get("q", item.get("question"))
        a = item.get("a", item.get("answer"))
        if not isinstance(q, str) or not isinstance(a, str):
            raise ValueError(f"entry {i}: q and a must both be text")
        entries.append(FaqEntry(q, a))
    return entries


@app.command("import")
def import_(
    slug: Annotated[str, typer.Argument(help="Tenant slug")],
    file: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="FAQ YAML file")],
) -> None:
    """Make the tenant's FAQ exactly the entries in FILE (new/changed embedded, absent deleted)."""
    try:
        entries = parse_faq_yaml(file)
    except ValueError as e:
        raise _fail(f"Invalid FAQ file: {e}") from e

    async def run(sessionmaker: SessionMaker) -> bool:
        async with sessionmaker() as session:
            tenant_id, _ = await load_tenant(session, slug)
        since = await db_now(sessionmaker)
        embedder = deps.DEPS.make_embedder(sessionmaker)
        try:
            stats = await replace_faq(sessionmaker, embedder, tenant_id, entries)
        except IngestError as e:
            raise _fail(f"Invalid FAQ: {e}") from e
        typer.echo(
            f"FAQ for '{slug}': {stats.new} new, {stats.updated} updated, "
            f"{stats.unchanged} unchanged, {stats.removed} removed, {stats.errors} errors"
        )
        for sample in stats.error_samples:
            typer.secho(f"  error: {sample}", fg=typer.colors.RED, err=True)
        if stats.removal_skipped:
            typer.secho(f"Note: {stats.removal_skipped}", fg=typer.colors.YELLOW)
        echo_cost(await embed_cost_since(sessionmaker, tenant_id, since))
        return stats.errors == 0

    if not run_with_sessionmaker(run):
        raise typer.Exit(code=1)
