import typer

from cli import tenants

app = typer.Typer(help="BizVision agents admin CLI", no_args_is_help=True)
app.add_typer(tenants.app, name="tenants")
