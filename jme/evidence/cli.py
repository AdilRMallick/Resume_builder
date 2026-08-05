"""`jme evidence ...` - build, inspect, and tag the evidence corpus."""

from __future__ import annotations

from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from jme.db import session_scope
from jme.evidence.ingest import (
    TaxonomyEmptyError,
    UnknownSkillError,
    chunk_skills,
    ingest_from_config,
    live_chunks,
    skills_by_chunk,
    tag_chunk,
    untag_chunk,
)
from jme.evidence.ingest import reembed as reembed_chunks
from jme.evidence.version import version_row
from jme.models import EvidenceChunk

app = typer.Typer(help="Evidence corpus ingestion, embedding, and tagging", no_args_is_help=True)
console = Console()


@app.command()
def ingest(
    directory: Annotated[
        str | None,
        typer.Option("--dir", help="Markdown directory to ingest [default: JME_EVIDENCE_DIR]"),
    ] = None,
    repos: Annotated[
        str | None,
        typer.Option("--repos", help="Comma-separated owner/repo list [default: JME_EVIDENCE_REPOS]"),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Report what would change without writing or embedding"),
    ] = False,
) -> None:
    """Chunk, embed, and upsert the corpus. Bumps evidence_version only if it changed."""
    repo_list = [r.strip() for r in repos.split(",") if r.strip()] if repos else None
    with session_scope() as session:
        stats = ingest_from_config(
            session, directory=directory, repos=repo_list, dry_run=dry_run
        )

    table = Table(title="evidence ingest" + (" (dry run)" if dry_run else ""), show_header=False)
    table.add_column("", style="bold")
    table.add_column("")
    table.add_row("sources", str(stats.sources))
    table.add_row("chunks", str(stats.chunks))
    table.add_row("added", f"[green]{stats.added}[/green]")
    table.add_row("updated", f"[yellow]{stats.updated}[/yellow]")
    table.add_row("revived", str(stats.revived))
    table.add_row("unchanged", str(stats.unchanged))
    table.add_row("deleted", f"[red]{stats.deleted}[/red]")
    if not dry_run:
        table.add_row("embedded", str(stats.embedded))
    console.print(table)

    if dry_run:
        verdict = (
            f"[yellow]would bump evidence_version {stats.version_before} -> "
            f"{stats.version_after}[/yellow]"
            if stats.changed
            else f"[green]no change; evidence_version stays at {stats.version_before}[/green]"
        )
    else:
        verdict = (
            f"[yellow]evidence_version {stats.version_before} -> {stats.version_after}; "
            "matches for active postings marked stale[/yellow]"
            if stats.changed
            else f"[green]no change; evidence_version stays at {stats.version_before}[/green]"
        )
    console.print(verdict)


@app.command("list")
def list_chunks(
    source_type: Annotated[
        str | None, typer.Option("--type", help="markdown | repo_readme")
    ] = None,
    source_ref: Annotated[
        str | None, typer.Option("--ref", help="Exact source ref to filter on")
    ] = None,
    limit: Annotated[int, typer.Option("--limit")] = 200,
) -> None:
    """List live chunks."""
    with session_scope() as session:
        chunks = live_chunks(
            session, source_type=source_type, source_ref=source_ref, limit=limit
        )
        tags = skills_by_chunk(session, [c.id for c in chunks])
        rows = [
            (
                str(c.id),
                c.source_ref,
                (c.heading or "-")[:40],
                str(c.token_estimate),
                ", ".join(tags.get(c.id, [])) or "-",
                "[green]y[/green]" if c.embedding is not None else "[red]n[/red]",
            )
            for c in chunks
        ]

    table = Table(title=f"evidence chunks ({len(rows)})")
    table.add_column("id", justify="right")
    table.add_column("source_ref")
    table.add_column("heading")
    table.add_column("tokens", justify="right")
    table.add_column("tags")
    table.add_column("emb", justify="center")
    for row in rows:
        table.add_row(*row)
    console.print(table)


@app.command()
def show(chunk_id: Annotated[int, typer.Argument(help="evidence_chunk.id")]) -> None:
    """Print a chunk's full text and its manual tags."""
    with session_scope() as session:
        chunk = session.get(EvidenceChunk, chunk_id)
        if chunk is None:
            console.print(f"[red]no evidence chunk with id {chunk_id}[/red]")
            raise typer.Exit(1)
        tags = chunk_skills(session, chunk.id)
        text = chunk.text
        meta = (
            f"{chunk.source_type}:{chunk.source_ref} #{chunk.ordinal}  "
            f"tokens={chunk.token_estimate}  v={chunk.evidence_version}  "
            f"model={chunk.embedding_model or '-'}"
            + ("  [red]deleted[/red]" if chunk.deleted_at else "")
        )
        tag_line = ", ".join(tags) or "[dim]no manual tags[/dim]"

    console.print(Panel(text, title=f"chunk {chunk_id}", subtitle=meta))
    console.print(f"tags: {tag_line}")


@app.command()
def tag(
    chunk_id: Annotated[int, typer.Argument(help="evidence_chunk.id")],
    skills: Annotated[list[str], typer.Argument(help="Canonical skill names")],
) -> None:
    """Manually tag a chunk with canonical skills. Bumps evidence_version."""
    _apply_tags(chunk_id, skills, add=True)


@app.command()
def untag(
    chunk_id: Annotated[int, typer.Argument(help="evidence_chunk.id")],
    skills: Annotated[list[str], typer.Argument(help="Canonical skill names")],
) -> None:
    """Remove manual tags from a chunk. Bumps evidence_version."""
    _apply_tags(chunk_id, skills, add=False)


def _apply_tags(chunk_id: int, skills: list[str], *, add: bool) -> None:
    try:
        with session_scope() as session:
            names = (
                tag_chunk(session, chunk_id, skills)
                if add
                else untag_chunk(session, chunk_id, skills)
            )
            version = version_row(session).version
    except TaxonomyEmptyError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    except (UnknownSkillError, ValueError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    if not names:
        console.print("[dim]no change[/dim]")
        return
    verb = "tagged" if add else "untagged"
    console.print(f"{verb} chunk {chunk_id}: {', '.join(names)}")
    console.print(f"[yellow]evidence_version now {version}[/yellow]")


@app.command()
def version() -> None:
    """Show the corpus version and size."""
    with session_scope() as session:
        row = version_row(session)
        chunks = len(live_chunks(session))
        data = (row.version, row.bumped_at, row.reason, chunks)

    table = Table(show_header=False, title="evidence version")
    table.add_column("", style="bold")
    table.add_column("")
    table.add_row("version", str(data[0]))
    table.add_row("bumped_at", str(data[1]))
    table.add_row("reason", data[2] or "-")
    table.add_row("live chunks", str(data[3]))
    console.print(table)


@app.command()
def reembed(
    force: Annotated[
        bool,
        typer.Option("--force", help="Recompute every embedding, not just the missing ones"),
    ] = False,
) -> None:
    """Embed chunks that have no vector. With --force, re-embed everything and bump."""
    with session_scope() as session:
        count = reembed_chunks(session, force=force)
        version_after = version_row(session).version
    console.print(f"re-embedded {count} chunk(s)")
    if force:
        console.print(f"[yellow]evidence_version now {version_after}[/yellow]")


if __name__ == "__main__":
    app()
