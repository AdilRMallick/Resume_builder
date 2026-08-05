"""Top-level CLI. Each subsystem owns a `cli.py` exposing a `typer.Typer()` named `app`;
this module mounts them. Subcommands are mounted defensively so a subsystem that is
mid-development does not break the whole CLI.
"""

from __future__ import annotations

import importlib

import typer
from rich.console import Console

from jme.logging import configure_logging

app = typer.Typer(help="Job Match Engine", no_args_is_help=True)
console = Console()

# (module path, command name, help text)
_SUBCOMMANDS = [
    ("jme.ingestor.cli", "ingest", "Pull the feed and upsert postings"),
    ("jme.taxonomy.cli", "taxonomy", "Canonical skills, aliases, and the review queue"),
    ("jme.enricher.cli", "enrich", "Requirement extraction from job description text"),
    ("jme.evidence.cli", "evidence", "Evidence corpus ingestion, embedding, and tagging"),
    ("jme.rank.cli", "rank", "Filter and rank active postings into a shortlist"),
    ("jme.matcher.cli", "match", "LLM match with citations back to evidence"),
    ("jme.report.cli", "report", "Aggregate skill gap report"),
    ("jme.api.cli", "serve", "Run the read-only HTTP API"),
]


def _mount() -> list[str]:
    missing: list[str] = []
    for module_path, name, help_text in _SUBCOMMANDS:
        try:
            module = importlib.import_module(module_path)
        except ModuleNotFoundError:
            missing.append(name)
            continue
        sub = getattr(module, "app", None)
        if sub is None:
            missing.append(name)
            continue
        app.add_typer(sub, name=name, help=help_text)
    return missing


_MISSING = _mount()


@app.callback()
def _root(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    configure_logging("DEBUG" if verbose else "INFO")


@app.command()
def status() -> None:
    """Show datastore connectivity and which subsystems are wired up."""
    import redis

    from jme.config import get_settings
    from jme.db import get_engine

    settings = get_settings()

    try:
        with get_engine().connect() as conn:
            conn.exec_driver_sql("SELECT 1")
        pg = "[green]ok[/green]"
    except Exception as exc:  # noqa: BLE001
        pg = f"[red]{type(exc).__name__}[/red]"

    try:
        redis.Redis.from_url(settings.redis_url).ping()
        rd = "[green]ok[/green]"
    except Exception as exc:  # noqa: BLE001
        rd = f"[red]{type(exc).__name__}[/red]"

    console.print(f"postgres  {pg}   {settings.database_url}")
    console.print(f"redis     {rd}   {settings.redis_url}")
    console.print(f"model     {settings.anthropic_model} (effort={settings.anthropic_effort})")
    console.print(f"embedding {settings.embedding_provider}")
    if _MISSING:
        console.print(f"[yellow]not yet wired:[/yellow] {', '.join(_MISSING)}")


if __name__ == "__main__":
    app()
