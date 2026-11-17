import typer

app = typer.Typer(help="BizVision agents admin CLI", no_args_is_help=True)


@app.callback()
def main() -> None:
    """BizVision agents admin CLI."""
