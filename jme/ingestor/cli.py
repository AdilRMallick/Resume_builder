"""`jme ingest` - pull the feed and upsert postings.

Mounted by `jme.cli`. Kept free of business logic: every command is argument parsing, a
call into `jme.ingestor.run`, and rendering.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import select

from jme.config import get_settings
from jme.db import session_scope
from jme.ingestor.feed import FeedError, FeedNotFoundError, FeedParseError
from jme.ingestor.run import IngestSummary, ingest
from jme.models import IngestRun

app = typer.Typer(help="Pull the SimplifyJobs feed and upsert postings", no_args_is_help=True)
console = Console()


def _render_summary(summary: IngestSummary) -> None:
    table = Table(
        # not "[dry run]": square brackets are rich markup and would be swallowed
        title=f"ingest {summary.run_id}" + (" (dry run)" if summary.dry_run else ""),
        title_style="bold",
        show_header=False,
        box=None,
    )
    table.add_column(style="dim")
    table.add_column(justify="right")
    table.add_row("seen", str(summary.total_seen))
    table.add_row("new", f"[green]{summary.new}[/green]")
    table.add_row("updated", str(summary.updated))
    table.add_row("reactivated", f"[cyan]{summary.reactivated}[/cyan]")
    table.add_row("deactivated", f"[yellow]{summary.deactivated}[/yellow]")
    if summary.skipped:
        table.add_row("skipped", f"[red]{summary.skipped}[/red]")
    table.add_row("enqueued", str(summary.enqueued))
    table.add_row("feed sha256", summary.feed_sha256[:16] or "-")
    table.add_row("duration", f"{summary.duration_sec:.2f}s")
    console.print(table)


@app.command("run")
def run_command(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Classify against the database and write nothing."
    ),
    no_enqueue: bool = typer.Option(
        False, "--no-enqueue", help="Skip XADDing fetch jobs. Does not require Redis."
    ),
    file: Path | None = typer.Option(  # noqa: B008 - typer's declaration style
        None,
        "--file",
        exists=True,
        dir_okay=False,
        readable=True,
        help="Ingest a local listings.json instead of fetching the feed.",
    ),
    url: str | None = typer.Option(None, "--url", help="Override JME_FEED_URL."),
    no_deactivate: bool = typer.Option(
        False,
        "--no-deactivate",
        help="Do not sweep postings absent from this feed. Use with a partial --file.",
    ),
) -> None:
    """Fetch the feed and upsert postings."""
    try:
        with session_scope() as session:
            summary = ingest(
                session,
                url=url,
                file=file,
                enqueue=not no_enqueue,
                dry_run=dry_run,
                deactivate=not no_deactivate,
            )
    except FeedNotFoundError as exc:
        console.print(f"[red]feed not found[/red]: {exc.url}")
        console.print("[dim]the path or branch moved; check JME_FEED_URL[/dim]")
        raise typer.Exit(code=2) from exc
    except FeedParseError as exc:
        console.print(f"[red]feed is malformed[/red]: {exc.reason}")
        raise typer.Exit(code=3) from exc
    except FeedError as exc:
        console.print(f"[red]feed unavailable[/red]: {exc}")
        raise typer.Exit(code=4) from exc

    _render_summary(summary)


@app.command("summary")
def summary_command(
    limit: int = typer.Option(10, "--limit", "-n", min=1, help="How many runs to show."),
) -> None:
    """Print the last N ingest runs."""
    with session_scope() as session:
        runs = (
            session.execute(select(IngestRun).order_by(IngestRun.started_at.desc()).limit(limit))
            .scalars()
            .all()
        )

    if not runs:
        console.print("[dim]no ingest runs recorded yet[/dim]")
        return

    table = Table(title=f"last {len(runs)} ingest runs", title_style="bold")
    table.add_column("run id", style="dim", no_wrap=True)
    table.add_column("started", no_wrap=True)
    table.add_column("secs", justify="right")
    table.add_column("seen", justify="right")
    table.add_column("new", justify="right", style="green")
    table.add_column("upd", justify="right")
    table.add_column("react", justify="right", style="cyan")
    table.add_column("deact", justify="right", style="yellow")
    table.add_column("feed", style="dim", no_wrap=True)
    table.add_column("error", style="red")

    for run in runs:
        elapsed = (
            f"{(run.finished_at - run.started_at).total_seconds():.1f}" if run.finished_at else "-"
        )
        table.add_row(
            run.run_id,
            run.started_at.strftime("%Y-%m-%d %H:%M:%S"),
            elapsed,
            str(run.total_seen),
            str(run.new_count),
            str(run.updated_count),
            str(run.reactivated_count),
            str(run.deactivated_count),
            (run.feed_sha256 or "")[:12],
            (run.error or "")[:40],
        )

    console.print(table)


@app.command("feed-url")
def feed_url_command() -> None:
    """Print the feed URL this install will fetch."""
    console.print(get_settings().feed_url)


if __name__ == "__main__":
    app()
