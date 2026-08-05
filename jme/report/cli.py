"""`jme report ...` - the terminal face of the gap report."""

from __future__ import annotations

from typing import Annotated

import typer
from rich.console import Console
from sqlalchemy import text

from jme.db import session_scope
from jme.report.gap import build_gap_report, coverage_report, explain_gap_query
from jme.report.render import (
    metrics_table,
    render_coverage,
    render_gap,
    render_trend,
    write_gap_json,
)
from jme.report.trend import build_trend_report

app = typer.Typer(help="Aggregate skill gap report", no_args_is_help=True)
console = Console()


@app.command()
def gap(
    top: Annotated[int, typer.Option("--top", "-n", help="how many ranked gaps to show")] = 20,
    json_path: Annotated[
        str | None, typer.Option("--json", help="also write the report as JSON to this path")
    ] = None,
    include_covered: Annotated[
        bool, typer.Option("--include-covered", help="also show skills I can already evidence")
    ] = False,
    all_active: Annotated[
        bool,
        typer.Option(
            "--all-active",
            help="skip the config eligibility filters and roll up every active posting",
        ),
    ] = False,
) -> None:
    """Ranked actionable skills that are required but not backed by my evidence."""
    with session_scope() as session:
        report = build_gap_report(session, apply_eligibility=not all_active)
        render_gap(console, report, top=top, include_covered=include_covered)
        if json_path:
            written = write_gap_json(
                report, json_path, top=top, include_covered=include_covered
            )
            console.print(f"\n[dim]wrote {written}[/dim]")


@app.command()
def trend(
    days: Annotated[int, typer.Option("--days", help="window length in days")] = 30,
    top: Annotated[int, typer.Option("--top", "-n")] = 20,
    all_active: Annotated[bool, typer.Option("--all-active")] = False,
) -> None:
    """How each skill's frequency in newly-seen postings changed over the window."""
    with session_scope() as session:
        report = build_trend_report(session, days=days, apply_eligibility=not all_active)
        render_trend(console, report, top=top)


@app.command()
def coverage() -> None:
    """Adapter coverage (ARCHITECTURE section 8) and taxonomy coverage (section 3)."""
    with session_scope() as session:
        render_coverage(console, coverage_report(session))


@app.command()
def metrics(
    run_id: Annotated[str | None, typer.Option("--run-id", help="filter to one run")] = None,
    stage: Annotated[str | None, typer.Option("--stage", help="filter to one stage")] = None,
    limit: Annotated[int, typer.Option("--limit")] = 100,
) -> None:
    """Dump `run_metric` rows. Every claim about this project should have a number."""
    sql = """
        SELECT recorded_at, run_id, stage, metric, value, labels
        FROM run_metric
        WHERE (CAST(:run_id AS text) IS NULL OR run_id = CAST(:run_id AS text))
          AND (CAST(:stage AS text) IS NULL OR stage = CAST(:stage AS text))
        ORDER BY recorded_at DESC, id DESC
        LIMIT :limit
    """
    with session_scope() as session:
        rows = (
            session.execute(text(sql), {"run_id": run_id, "stage": stage, "limit": limit})
            .mappings()
            .all()
        )
        console.print(metrics_table([dict(r) for r in rows]))


@app.command()
def explain(
    analyze: Annotated[bool, typer.Option("--analyze/--no-analyze")] = True,
    all_active: Annotated[bool, typer.Option("--all-active")] = False,
) -> None:
    """Print `EXPLAIN ANALYZE` for the rollup query, for docs/explain/gap_report.md."""
    with session_scope() as session:
        console.print(
            explain_gap_query(session, apply_eligibility=not all_active, analyze=analyze),
            highlight=False,
        )


if __name__ == "__main__":  # pragma: no cover
    app()
