"""`jme match ...` - run the citation matcher and read its cost back out."""

from __future__ import annotations

import typer
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from jme.config import get_settings
from jme.db import session_scope
from jme.matcher.match import (
    MatchError,
    MatchOutcome,
    RunBudgetExceeded,
    latest_shortlist_run,
    match_posting_detailed,
    match_shortlist,
    recompute_stale,
)
from jme.models import CanonicalSkill, EvidenceChunk, Match, MatchCitation, Posting, RunMetric

app = typer.Typer(help="LLM match with citations back to evidence", no_args_is_help=True)
console = Console()

STATUS_STYLE = {"evidenced": "green", "weak": "yellow", "absent": "red"}
VERDICT_STYLE = {"strong": "green", "plausible": "cyan", "stretch": "yellow", "no": "red"}


def _render_match(session: Session, match: Match, *, cached: bool) -> None:
    posting = session.get(Posting, match.posting_id)
    rows = session.execute(
        select(MatchCitation, CanonicalSkill.name, EvidenceChunk.source_ref)
        .outerjoin(CanonicalSkill, CanonicalSkill.id == MatchCitation.canonical_skill_id)
        .outerjoin(EvidenceChunk, EvidenceChunk.id == MatchCitation.evidence_chunk_id)
        .where(MatchCitation.match_id == match.id)
        .order_by(MatchCitation.id)
    ).all()

    table = Table(
        title=f"{posting.company} - {posting.title}" if posting else f"posting {match.posting_id}",
        show_lines=False,
    )
    table.add_column("skill", style="bold", max_width=26)
    table.add_column("status", max_width=10)
    table.add_column("citation", max_width=34)
    table.add_column("reasoning", overflow="fold")

    for citation, skill_name, source_ref in rows:
        status = citation.status.value
        cite = (
            f"chunk {citation.evidence_chunk_id} ({source_ref})"
            if citation.evidence_chunk_id
            else "-"
        )
        table.add_row(
            skill_name or "(unmapped)",
            f"[{STATUS_STYLE[status]}]{status}[/{STATUS_STYLE[status]}]",
            cite,
            (citation.reasoning or "").strip(),
        )

    console.print(table)

    verdict = match.verdict.value if match.verdict else "?"
    style = VERDICT_STYLE.get(verdict, "white")
    rationale = (match.rationale or {}).get("text", "")
    console.print(f"verdict   [{style}]{verdict}[/{style}]    score {float(match.score or 0):.4f}")
    console.print(f"rationale {rationale}")
    console.print(
        f"cost      ${float(match.cost_usd or 0):.6f}"
        f"   tokens {match.input_tokens} in / {match.output_tokens} out"
        f"   {'[cyan]served from cache[/cyan]' if cached else 'freshly computed'}"
    )
    console.print(
        f"key       match {match.id} | evidence_version={match.evidence_version} "
        f"prompt={match.prompt_version} model={match.model_id} jd={match.jd_sha256[:12]}"
    )


@app.command()
def posting(
    posting_id: int = typer.Argument(..., help="posting.id to match"),
    force: bool = typer.Option(False, "--force", help="recompute, bypassing both caches"),
    top_k: int = typer.Option(None, "--k", help="evidence chunks retrieved per requirement"),
    abort_on_cost: bool = typer.Option(
        False, "--abort-on-cost", help="fail instead of warning when over the per-match ceiling"
    ),
) -> None:
    """Match one posting and print its requirements, citations, verdict, and cost."""
    with session_scope() as session:
        try:
            outcome = match_posting_detailed(
                session,
                posting_id,
                force=force,
                top_k=top_k,
                cost_policy="abort" if abort_on_cost else "warn",
            )
        except MatchError as exc:
            console.print(f"[red]{type(exc).__name__}[/red] {exc}")
            raise typer.Exit(code=1) from exc
        _render_match(session, outcome.match, cached=outcome.from_match_cache)


@app.command()
def shortlist(
    run_id: str = typer.Option(None, "--run-id", help="shortlist run; defaults to the latest"),
    force: bool = typer.Option(False, "--force", help="recompute every posting"),
    top_k: int = typer.Option(None, "--k", help="evidence chunks retrieved per requirement"),
    abort_on_cost: bool = typer.Option(False, "--abort-on-cost"),
) -> None:
    """Match every posting in a shortlist run, with a running cost total."""
    with session_scope() as session:
        resolved = run_id or latest_shortlist_run(session)
        if resolved is None:
            console.print("[red]no shortlist runs found[/red]; run `jme rank` first")
            raise typer.Exit(code=1)

        settings = get_settings()
        spent = {"usd": 0.0}
        progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            TextColumn("[cyan]${task.fields[cost]:.4f}[/cyan]"),
            TimeElapsedColumn(),
            console=console,
        )

        with progress:
            task = progress.add_task(f"matching {resolved}", total=None, cost=0.0)

            def hook(
                index: int,
                total: int,
                posting_id: int,
                outcome: MatchOutcome | None,
                error: Exception | None,
            ) -> None:
                if outcome is not None:
                    spent["usd"] += outcome.cost_usd
                progress.update(task, total=total, completed=index, cost=spent["usd"])
                if error is not None:
                    progress.console.print(f"[yellow]posting {posting_id}[/yellow]: {error}")

            try:
                report = match_shortlist(
                    session,
                    resolved,
                    force=force,
                    top_k=top_k,
                    cost_policy="abort" if abort_on_cost else "warn",
                    on_progress=hook,
                )
            except RunBudgetExceeded as exc:
                console.print(f"[red]run budget exceeded[/red] {exc}")
                raise typer.Exit(code=2) from exc
            except MatchError as exc:
                console.print(f"[red]{type(exc).__name__}[/red] {exc}")
                raise typer.Exit(code=1) from exc

        table = Table(title=f"shortlist {report.run_id}")
        table.add_column("posting")
        table.add_column("verdict")
        table.add_column("score", justify="right")
        table.add_column("evidenced", justify="right")
        table.add_column("cost", justify="right")
        for outcome in report.outcomes:
            match = outcome.match
            evidenced = sum(1 for c in match.citations if c.status.value == "evidenced")
            verdict = match.verdict.value if match.verdict else "?"
            table.add_row(
                str(match.posting_id),
                f"[{VERDICT_STYLE.get(verdict, 'white')}]{verdict}[/]",
                f"{float(match.score or 0):.3f}",
                f"{evidenced}/{len(match.citations)}",
                f"${float(match.cost_usd or 0):.5f}",
            )
        console.print(table)
        console.print(
            f"matched {report.matched}/{report.total}  cache {report.from_cache}  "
            f"failed {report.failed}  skipped {report.skipped}"
        )
        console.print(
            f"cost ${report.cost_usd:.5f} (new spend ${report.new_cost_usd:.5f}) "
            f"budget ${report.budget_usd:.4f} "
            f"ceiling ${settings.max_cost_per_match_usd:.4f}/match"
        )


@app.command()
def stale(
    limit: int = typer.Option(20, "--limit", help="max postings to recompute this sweep"),
    top_k: int = typer.Option(None, "--k"),
) -> None:
    """Recompute stale matches for active postings, bounded by --limit."""
    with session_scope() as session:
        report = recompute_stale(session, limit=limit, top_k=top_k)
        console.print(
            f"recomputed {report.matched}/{report.total} stale matches "
            f"(failed {report.failed}) cost ${report.cost_usd:.5f}  run_id={report.run_id}"
        )
        for posting_id, error in report.errors:
            console.print(f"[yellow]posting {posting_id}[/yellow]: {error}")


@app.command()
def cost(
    run_id: str = typer.Option(None, "--run-id", help="restrict to one run"),
) -> None:
    """Tokens, dollars, and cache hit rate for the match stage from run_metric."""
    with session_scope() as session:
        where = [RunMetric.stage == "match"]
        if run_id:
            where.append(RunMetric.run_id == run_id)

        totals = dict(
            session.execute(
                select(RunMetric.metric, func.sum(RunMetric.value)).where(*where).group_by(
                    RunMetric.metric
                )
            ).all()
        )
        counts = dict(
            session.execute(
                select(RunMetric.metric, func.count()).where(*where).group_by(RunMetric.metric)
            ).all()
        )

        def total(name: str) -> float:
            return float(totals.get(name) or 0)

        llm_calls = counts.get("cache_hit", 0)
        cache_hits = total("cache_hit")
        hit_rate = (cache_hits / llm_calls * 100) if llm_calls else 0.0

        table = Table(title=f"match cost{' - ' + run_id if run_id else ' (all runs)'}")
        table.add_column("metric")
        table.add_column("value", justify="right")
        table.add_row("postings matched", f"{total('matched'):.0f}")
        table.add_row("match-row cache hits", f"{total('match_row_cache_hit'):.0f}")
        table.add_row("llm calls", f"{llm_calls}")
        table.add_row("llm cache hit rate", f"{hit_rate:.1f}%")
        table.add_row("input tokens", f"{total('input_tokens'):,.0f}")
        table.add_row("output tokens", f"{total('output_tokens'):,.0f}")
        table.add_row("cost (llm calls)", f"${total('cost_usd'):.5f}")
        table.add_row("cost attributed to matches", f"${total('match_cost_usd'):.5f}")
        table.add_row("citation retries", f"{total('citation_retry'):.0f}")
        table.add_row("fabricated citations", f"{total('fabricated_citation'):.0f}")
        table.add_row("missing citations", f"{total('missing_citation'):.0f}")
        table.add_row("cost ceiling breaches", f"{total('cost_ceiling_exceeded'):.0f}")
        console.print(table)

        matched = total("matched")
        if matched:
            console.print(f"average ${total('match_cost_usd') / matched:.5f} per match")

        stale_count = session.scalar(
            select(func.count())
            .select_from(Match)
            .join(Posting, Posting.id == Match.posting_id)
            .where(Match.is_stale.is_(True), Posting.inactive_at.is_(None))
        )
        console.print(f"stale matches on active postings: {stale_count}")


if __name__ == "__main__":
    app()
