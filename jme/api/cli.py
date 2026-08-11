"""`jme serve start` - run the local API and browser-extension backend under uvicorn."""

from __future__ import annotations

from typing import Annotated

import typer

app = typer.Typer(help="Run the local HTTP API", no_args_is_help=True)


@app.command()
def start(
    host: Annotated[
        str, typer.Option("--host", help="bind address; localhost by design, there is no auth")
    ] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", "-p")] = 8000,
    reload: Annotated[
        bool, typer.Option("--reload", help="autoreload on source change, for development")
    ] = False,
    log_level: Annotated[str, typer.Option("--log-level")] = "info",
) -> None:
    """Start the API. Defaults to 127.0.0.1 because this service has no authentication."""
    import uvicorn

    # An import string, not the app object: --reload needs one, and uvicorn's worker
    # process re-imports it rather than inheriting a half-initialised engine.
    uvicorn.run(
        "jme.api.app:app", host=host, port=port, reload=reload, log_level=log_level
    )


@app.command()
def routes() -> None:
    """List the routes the API exposes. Handy for checking a deploy without curl."""
    from rich.console import Console
    from rich.table import Table

    from jme.api.app import app as fastapi_app

    table = Table(title="routes", header_style="bold", title_justify="left")
    table.add_column("methods")
    table.add_column("path")
    table.add_column("name", style="dim")
    for route in fastapi_app.routes:
        methods = ",".join(sorted(getattr(route, "methods", []) or []))
        table.add_row(methods, getattr(route, "path", ""), getattr(route, "name", ""))
    Console().print(table)


if __name__ == "__main__":  # pragma: no cover
    app()
