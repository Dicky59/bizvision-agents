import sys

import typer

from cli import chat as chat_cmd
from cli import evals as evals_cmd
from cli import faq, leads, tenants
from cli import ingest as ingest_cmd
from cli import search as search_cmd
from cli import usage as usage_cmd

app = typer.Typer(help="BizVision agents admin CLI", no_args_is_help=True)
app.add_typer(tenants.app, name="tenants")
app.add_typer(faq.app, name="faq")
app.add_typer(evals_cmd.app, name="evals")
app.add_typer(leads.app, name="leads")
app.command("cleanup")(leads.cleanup)
app.command("usage")(usage_cmd.usage)
app.command("ingest")(ingest_cmd.ingest)
app.command("search")(search_cmd.search)
app.command("chat")(chat_cmd.chat)


@app.callback()
def _main() -> None:
    # Page content is arbitrary Unicode; never crash a command on a legacy Windows code page.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
