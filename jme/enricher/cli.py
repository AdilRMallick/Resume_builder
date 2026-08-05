"""`jme enrich ...` - requirement extraction from the terminal."""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from jme.db import session_scope
from jme.enricher.extraction import extract_requirements
from jme.enricher.prompts import PROMPT_VERSION
from jme.enricher.worker import run_worker
from jme.logging import get_logger
from jme.metrics import new_run_id
from jme.models import CanonicalSkill, Posting, PostingJD, PostingRequirement, RunMetric

app = typer.Typer(help="Requirement extraction from job description text", no_args_is_help=True)
console = Console()
log = get_logger(__name__)


def _skill_names(session: Session, rows: list[PostingRequirement]) -> dict[int, str]:
    ids = {row.canonical_skill_id for row in rows if row.canonical_skill_id is not None}
    if not ids:
        return {}
    return dict(
        session.execute(
            select(CanonicalSkill.id, CanonicalSkill.name).where(CanonicalSkill.id.in_(ids))
        ).all()
    )


def _render(session: Session, posting_id: int, rows: list[PostingRequirement]) -> None:
    names = _skill_names(session, rows)
    table = Table(
        title=f"posting {posting_id} - {len(rows)} requirements (prompt {PROMPT_VERSION})",
        show_lines=False,
    )
    table.add_column("importance", style="bold", width=10)
    table.add_column("conf", justify="right", width=5)
    table.add_column("skill", width=22)
    table.add_column("raw_text (verbatim span)", overflow="fold")

    colors = {"required": "red", "preferred": "yellow", "mentioned": "dim"}
    order = {"required": 0, "preferred": 1, "mentioned": 2}
    for row in sorted(
        rows, key=lambda r: (order[r.importance.value], -float(r.confidence))
    ):
        skill = names.get(row.canonical_skill_id or -1)
        table.add_row(
            f"[{colors[row.importance.value]}]{row.importance.value}[/]",
            f"{float(row.confidence):.2f}",
            skill or "[dim]unresolved[/dim]",
            row.raw_text,
        )
    console.print(table)


@app.command()
def posting(
    posting_id: int = typer.Argument(..., help="posting.id to extract requirements for"),
    force: bool = typer.Option(
        False, "--force", help="bypass the LLM cache and re-ask the model"
    ),
) -> None:
    """Extract requirements for one posting and print them."""
    with session_scope() as session:
        jd = session.get(PostingJD, posting_id)
        if jd is None or not (jd.raw_text or "").strip():
            console.print(f"[red]posting {posting_id} has no JD text[/red]")
            raise typer.Exit(code=1)
        post = session.get(Posting, posting_id)
        rows = extract_requirements(
            session,
            posting_id,
            jd.raw_text or "",
            run_id=new_run_id("extract"),
            company=post.company if post else None,
            title=post.title if post else jd.title,
            force_refresh=force,
        )
        _render(session, posting_id, rows)


@app.command()
def backlog(
    limit: int = typer.Option(50, "--limit", "-n", help="max postings to process"),
    force: bool = typer.Option(False, "--force", help="bypass the LLM cache"),
) -> None:
    """Extract for active postings with JD text and no requirements at the current version."""
    run_id = new_run_id("extract")
    with session_scope() as session:
        already = (
            select(PostingRequirement.posting_id)
            .where(PostingRequirement.prompt_version == PROMPT_VERSION)
            .distinct()
        )
        stmt = (
            select(Posting.id)
            .join(PostingJD, PostingJD.posting_id == Posting.id)
            .where(
                and_(
                    Posting.inactive_at.is_(None),
                    PostingJD.raw_text.is_not(None),
                    func.length(PostingJD.raw_text) > 0,
                    Posting.id.not_in(already),
                )
            )
            .order_by(Posting.posted_at.desc().nullslast(), Posting.id.desc())
            .limit(limit)
        )
        posting_ids = list(session.scalars(stmt))

    if not posting_ids:
        console.print("[green]nothing to do[/green]: no active postings pending extraction")
        return

    console.print(f"run [bold]{run_id}[/bold]: {len(posting_ids)} postings")
    total = failed = 0
    for pid in posting_ids:
        try:
            with session_scope() as session:
                jd = session.get(PostingJD, pid)
                post = session.get(Posting, pid)
                rows = extract_requirements(
                    session,
                    pid,
                    (jd.raw_text or "") if jd else "",
                    run_id=run_id,
                    company=post.company if post else None,
                    title=post.title if post else None,
                    force_refresh=force,
                )
                total += len(rows)
                console.print(f"  [green]ok[/green]  {pid}  {len(rows):>3} requirements")
        except Exception as exc:  # noqa: BLE001 - one bad posting must not stop the backlog
            failed += 1
            log.error("backlog_posting_failed", posting_id=pid, error=str(exc))
            console.print(f"  [red]err[/red] {pid}  {type(exc).__name__}: {exc}")

    console.print(
        f"\n[bold]{len(posting_ids) - failed}[/bold] postings, "
        f"[bold]{total}[/bold] requirements, [red]{failed}[/red] failed"
    )
    console.print(f"cost:  jme enrich cost --run-id {run_id}")


@app.command()
def worker(
    consumer: str = typer.Option(None, "--consumer", help="consumer name; defaults to host-pid"),
) -> None:
    """Run the `jme:enrich` stream consumer loop until SIGTERM."""
    run_worker(consumer)


@app.command()
def cost(
    run_id: str = typer.Option(None, "--run-id", help="restrict to one run"),
) -> None:
    """Summarize tokens and dollars spent on extraction."""
    with session_scope() as session:
        stmt = (
            select(
                RunMetric.run_id,
                RunMetric.metric,
                func.sum(RunMetric.value),
                func.count(),
            )
            .where(RunMetric.stage == "extraction")
            .group_by(RunMetric.run_id, RunMetric.metric)
        )
        if run_id:
            stmt = stmt.where(RunMetric.run_id == run_id)
        rows = session.execute(stmt).all()

    if not rows:
        console.print("[yellow]no extraction metrics recorded yet[/yellow]")
        return

    by_run: dict[str, dict[str, float]] = {}
    counts: dict[str, dict[str, int]] = {}
    for rid, metric, total, n in rows:
        by_run.setdefault(rid, {})[metric] = float(total)
        counts.setdefault(rid, {})[metric] = int(n)

    table = Table(title="extraction cost", show_footer=True)
    table.add_column("run_id", footer="total")
    table.add_column("calls", justify="right")
    table.add_column("cache hits", justify="right")
    table.add_column("in tokens", justify="right")
    table.add_column("out tokens", justify="right")
    table.add_column("USD", justify="right")
    table.add_column("reqs", justify="right")

    totals = {"calls": 0.0, "hits": 0.0, "in": 0.0, "out": 0.0, "usd": 0.0, "reqs": 0.0}
    for rid in sorted(by_run):
        metrics = by_run[rid]
        calls = counts.get(rid, {}).get("cache_hit", 0)
        hits = metrics.get("cache_hit", 0.0)
        totals["calls"] += calls
        totals["hits"] += hits
        totals["in"] += metrics.get("input_tokens", 0.0)
        totals["out"] += metrics.get("output_tokens", 0.0)
        totals["usd"] += metrics.get("cost_usd", 0.0)
        totals["reqs"] += metrics.get("requirements_found", 0.0)
        table.add_row(
            rid,
            f"{calls:,}",
            f"{hits:,.0f}",
            f"{metrics.get('input_tokens', 0.0):,.0f}",
            f"{metrics.get('output_tokens', 0.0):,.0f}",
            f"${metrics.get('cost_usd', 0.0):,.4f}",
            f"{metrics.get('requirements_found', 0.0):,.0f}",
        )
    table.columns[1].footer = f"{totals['calls']:,.0f}"
    table.columns[2].footer = f"{totals['hits']:,.0f}"
    table.columns[3].footer = f"{totals['in']:,.0f}"
    table.columns[4].footer = f"{totals['out']:,.0f}"
    table.columns[5].footer = f"${totals['usd']:,.4f}"
    table.columns[6].footer = f"{totals['reqs']:,.0f}"
    console.print(table)


if __name__ == "__main__":
    app()
