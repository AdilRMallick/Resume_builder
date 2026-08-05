"""`jme rank` -- run the funnel, inspect the funnel, and argue with the funnel."""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import text
from sqlalchemy.orm import Session

from jme.config import get_settings
from jme.db import session_scope
from jme.metrics import new_run_id
from jme.rank import filters, pipeline, score

app = typer.Typer(help="Filter and rank active postings into a shortlist", no_args_is_help=True)
console = Console()


def _posting_meta(session: Session, posting_ids: list[int]) -> dict[int, dict]:
    if not posting_ids:
        return {}
    rows = session.execute(
        text(
            """
            SELECT p.id, p.company, p.title, p.locations, p.is_remote, p.url,
                   count(r.id) AS requirement_count
            FROM posting p
            LEFT JOIN posting_requirement r ON r.posting_id = p.id
            WHERE p.id = ANY(:ids)
            GROUP BY p.id
            """
        ),
        {"ids": posting_ids},
    ).mappings()
    return {int(row["id"]): dict(row) for row in rows}


def _location_label(meta: dict) -> str:
    locs = meta.get("locations")
    if isinstance(locs, list) and locs:
        label = ", ".join(str(item) for item in locs[:2])
        if len(locs) > 2:
            label += f" (+{len(locs) - 2})"
        return label
    if isinstance(locs, str) and locs:
        return locs
    return "Remote" if meta.get("is_remote") else "-"


@app.command("run")
def run_cmd(
    limit: int | None = typer.Option(None, "--limit", "-n", help="Shortlist size (default: config)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Compute but write nothing"),
    run_id: str | None = typer.Option(None, "--run-id", help="Reuse an existing run id"),
) -> None:
    """Run the filter+rank pipeline and print the shortlist."""
    settings = get_settings()
    rid = run_id or new_run_id("rank")

    with session_scope() as session:
        result = pipeline.rank(session, rid, limit, settings=settings, dry_run=dry_run)
        by_id = {s.posting_id: s for s in result.scores}
        ranked = [(entry.rank, by_id[entry.posting_id]) for entry in result.entries]
        meta = _posting_meta(session, [s.posting_id for _, s in ranked])

        table = Table(
            title=f"shortlist  run={rid}  evidence_version={result.evidence_version}"
            + ("  [DRY RUN]" if dry_run else "")
        )
        table.add_column("#", justify="right", style="bold")
        table.add_column("score", justify="right")
        table.add_column("company")
        table.add_column("title")
        table.add_column("location")
        table.add_column("reqs", justify="right")

        for position, item in ranked:
            row_meta = meta.get(item.posting_id, {})
            reqs = str(item.requirement_count)
            if item.low_confidence:
                reqs = "[yellow]0 (title only)[/yellow]"
            table.add_row(
                str(position),
                f"{item.coarse_score:.5f}",
                str(row_meta.get("company", "?")),
                str(row_meta.get("title", "?")),
                _location_label(row_meta),
                reqs,
            )

        console.print(table)
        console.print(
            f"total={result.outcome.total_postings}  "
            f"after_filters={len(result.outcome.kept)}  "
            f"after_ranking={len(result.entries)}"
        )
        if dry_run:
            session.rollback()


@app.command("funnel")
def funnel_cmd(
    run_id: str | None = typer.Option(None, "--run-id", help="Defaults to the most recent run"),
) -> None:
    """Print the stage-by-stage funnel counts recorded in run_metric."""
    with session_scope() as session:
        rid = run_id or pipeline.latest_run_id(session)
        if not rid:
            console.print("[yellow]no rank runs recorded yet[/yellow]")
            raise typer.Exit(code=1)

        rows = pipeline.funnel(session, rid)
        if not rows:
            console.print(f"[yellow]no metrics for run_id={rid}[/yellow]")
            raise typer.Exit(code=1)

        table = Table(title=f"rank funnel  run={rid}")
        table.add_column("metric")
        table.add_column("value", justify="right")
        table.add_column("kind")

        for metric, value, labels in rows:
            kind = (labels or {}).get("kind", "")
            rendered = f"{value:.4f}" if metric.endswith("_seconds") else f"{int(value)}"
            style = "yellow" if kind == "filter_flag" else ("red" if kind == "filter_drop" else "")
            table.add_row(metric, rendered, kind, style=style or None)
        console.print(table)


@app.command("explain")
def explain_cmd(
    analyze: bool = typer.Option(True, "--analyze/--no-analyze", help="Actually execute the query"),
    buffers: bool = typer.Option(True, "--buffers/--no-buffers"),
) -> None:
    """EXPLAIN (ANALYZE, BUFFERS) the stage-1 filter query."""
    settings = get_settings()
    options = [opt for opt, on in (("ANALYZE", analyze), ("BUFFERS", buffers)) if on]
    prefix = f"EXPLAIN ({', '.join(options)}) " if options else "EXPLAIN "

    with session_scope() as session:
        stmt = filters.bind_arrays(prefix + filters.stage1_sql())
        rows = session.execute(stmt, filters.filter_params(settings)).all()
        for (line,) in rows:
            console.print(line, markup=False, highlight=False)


@app.command("show")
def show_cmd(posting_id: int = typer.Argument(..., help="posting.id")) -> None:
    """Explain one posting's score: per-requirement best evidence chunk and similarity."""
    settings = get_settings()
    with session_scope() as session:
        posting, drop_reason = filters.classify_posting(session, posting_id, settings)
        if posting is None:
            if drop_reason == "inactive":
                console.print(f"[red]posting {posting_id} is inactive (dropped at stage 1)[/red]")
            else:
                console.print(f"[red]no posting with id {posting_id}[/red]")
            raise typer.Exit(code=1)

        console.print(f"[bold]{posting.company}[/bold] - {posting.title}")
        console.print(f"  url          {posting.url}")
        console.print(f"  locations    {posting.locations or ('remote' if posting.is_remote else '-')}")
        console.print(f"  sponsorship  {posting.sponsorship or '-'}  ({posting.sponsorship_class})")
        console.print(f"  role_type    {posting.role_type or '-'}  ({posting.role_state})")
        console.print(f"  start_season {posting.start_season or '-'}  ({posting.start_state})")
        if posting.flags:
            console.print(f"  [yellow]flags        {', '.join(posting.flags)}[/yellow]")
        if drop_reason:
            console.print(f"  [red]dropped at stage 1: {drop_reason}[/red]")

        scored = score.score_postings(session, [posting], settings)[0]

        table = Table(title=f"coarse_score = {scored.coarse_score:.5f}  ({scored.basis})")
        table.add_column("importance")
        table.add_column("weight", justify="right")
        table.add_column("requirement")
        table.add_column("best chunk", justify="right")
        table.add_column("sim", justify="right")
        table.add_column("contribution", justify="right")

        for match in sorted(scored.matches, key=lambda m: (-m.contribution, m.requirement_id)):
            chunk = str(match.evidence_chunk_id) if match.evidence_chunk_id is not None else "-"
            text_preview = match.raw_text if len(match.raw_text) <= 70 else match.raw_text[:67] + "..."
            table.add_row(
                match.importance.value,
                f"{match.weight:.2f}",
                text_preview,
                chunk,
                f"{match.similarity:.4f}",
                f"{match.contribution:.4f}",
            )
        console.print(table)

        denominator = score.REQUIRED_WEIGHT * max(len(scored.matches), 1)
        console.print(
            f"  base = sum(contribution)/({score.REQUIRED_WEIGHT} * {len(scored.matches)}) "
            f"= {sum(m.contribution for m in scored.matches) / denominator:.5f}"
        )
        console.print(
            f"  location_boost applied: {scored.location_boosted} "
            f"(weight {score.LOCATION_BOOST_WEIGHT})"
        )
        if scored.low_confidence:
            console.print(
                "  [yellow]low confidence: no extracted requirements, "
                "scored on title+company similarity[/yellow]"
            )

        for chunk_id in {m.evidence_chunk_id for m in scored.matches if m.evidence_chunk_id}:
            row = session.execute(
                text("SELECT source_type, source_ref, heading FROM evidence_chunk WHERE id = :id"),
                {"id": chunk_id},
            ).first()
            if row:
                console.print(f"  chunk {chunk_id}: {row[0]}:{row[1]} {row[2] or ''}")


if __name__ == "__main__":
    app()
