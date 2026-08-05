"""`jme taxonomy ...` -- the only path that may create a canonical skill.

    jme taxonomy seed [--dry-run]              load/refresh from data/taxonomy/skills.yaml
    jme taxonomy resolve "Experience with Go"  debug a single string
    jme taxonomy candidates                    review queue, seen_count desc
    jme taxonomy approve 12 --skill Go         attach the raw text to an existing skill
    jme taxonomy promote 12 --name Zig --category language
    jme taxonomy reject 12
    jme taxonomy stats
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import Integer, func, select

from jme.db import session_scope
from jme.models import (
    CandidateStatus,
    CanonicalSkill,
    SkillAlias,
    SkillAliasCandidate,
    SkillCategory,
)
from jme.taxonomy import resolver
from jme.taxonomy.normalize import normalize, tokenize
from jme.taxonomy.seed import SeedError, load_specs
from jme.taxonomy.seed import seed as run_seed

app = typer.Typer(help="Canonical skills, aliases, and the review queue", no_args_is_help=True)
console = Console()


def _skill_by_name(session, name: str) -> CanonicalSkill | None:
    """Exact name, then case-insensitive name, then alias."""
    skill = session.scalar(select(CanonicalSkill).where(CanonicalSkill.name == name))
    if skill is not None:
        return skill
    skill = session.scalar(
        select(CanonicalSkill).where(func.lower(CanonicalSkill.name) == name.lower())
    )
    if skill is not None:
        return skill
    return session.scalar(
        select(CanonicalSkill)
        .join(SkillAlias, SkillAlias.canonical_skill_id == CanonicalSkill.id)
        .where(SkillAlias.alias_norm == normalize(name))
    )


def _get_candidate(session, candidate_id: int) -> SkillAliasCandidate:
    candidate = session.get(SkillAliasCandidate, candidate_id)
    if candidate is None:
        console.print(f"[red]no candidate with id {candidate_id}[/red]")
        raise typer.Exit(code=1)
    return candidate


@app.command()
def seed(
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="show the diff, write nothing")
    ] = False,
    path: Annotated[
        Path | None, typer.Option("--path", help="override the seed YAML path")
    ] = None,
) -> None:
    """Load or refresh canonical_skill + skill_alias from the seed YAML."""
    try:
        specs = load_specs(path)
    except SeedError as exc:
        console.print(f"[red]seed file rejected:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    with session_scope() as session:
        report = run_seed(session, specs, dry_run=dry_run)

    if dry_run:
        for line in report.changes[:200]:
            colour = {"+": "green", "-": "red", "~": "yellow"}.get(line[0], "white")
            console.print(f"[{colour}]{line}[/{colour}]")
        if len(report.changes) > 200:
            console.print(f"... and {len(report.changes) - 200} more")

    table = Table(title="taxonomy seed" + (" (dry run)" if dry_run else ""))
    table.add_column("metric")
    table.add_column("count", justify="right")
    for key, value in report.as_dict().items():
        if key in {"orphan_skills", "dry_run"}:
            continue
        table.add_row(key, str(value))
    table.add_row("skills in yaml", str(len(specs)))
    console.print(table)
    if report.orphan_skills:
        console.print(
            f"[yellow]{len(report.orphan_skills)} skill(s) in the database but not in the YAML "
            f"(left alone):[/yellow] {', '.join(report.orphan_skills[:20])}"
        )
    if not report.changed:
        console.print("[green]taxonomy already up to date[/green]")


@app.command()
def resolve(
    text: str = typer.Argument(..., help="raw requirement span"),
    record: bool = typer.Option(
        False, "--record/--no-record", help="also write a candidate row on no match"
    ),
) -> None:
    """Debug resolution of a single string."""
    with session_scope() as session:
        resolver.invalidate_cache()
        index = resolver.get_index(session)
        tokens = tokenize(text)
        matches = resolver.find_matches(index, tokens)
        console.print(f"normalized : [cyan]{normalize(text)}[/cyan]")
        console.print(f"tokens     : {list(tokens)}")
        console.print(f"alias index: {index.size} aliases, max {index.max_tokens} tokens")
        if not matches:
            console.print("[yellow]no match[/yellow]")
            if record:
                resolver.resolve(session, text)
                console.print("recorded a candidate row")
            return
        table = Table()
        table.add_column("#")
        table.add_column("alias")
        table.add_column("skill")
        table.add_column("category")
        table.add_column("actionable")
        for i, match in enumerate(matches):
            skill = session.get(CanonicalSkill, match.skill_id)
            table.add_row(
                str(i),
                match.alias,
                f"{skill.name} (id={skill.id})" if skill else "?",
                skill.category.value if skill else "?",
                str(skill.is_actionable) if skill else "?",
            )
        console.print(table)
        console.print(f"[green]resolve() returns id={matches[0].skill_id}[/green] (first match)")


@app.command()
def candidates(
    status: Annotated[CandidateStatus, typer.Option("--status")] = CandidateStatus.pending,
    limit: Annotated[int, typer.Option("--limit")] = 50,
) -> None:
    """List review-queue rows, most-seen first."""
    with session_scope() as session:
        rows = session.scalars(
            select(SkillAliasCandidate)
            .where(SkillAliasCandidate.status == status)
            .order_by(SkillAliasCandidate.seen_count.desc(), SkillAliasCandidate.id)
            .limit(limit)
        ).all()
        table = Table(title=f"skill_alias_candidate ({status.value})")
        table.add_column("id", justify="right")
        table.add_column("seen", justify="right")
        table.add_column("raw_text")
        table.add_column("normalized")
        table.add_column("posting", justify="right")
        table.add_column("last seen")
        for row in rows:
            table.add_row(
                str(row.id),
                str(row.seen_count),
                row.raw_text[:60],
                row.raw_norm[:60],
                str(row.example_posting_id or "-"),
                row.last_seen_at.strftime("%Y-%m-%d"),
            )
        console.print(table)
        if not rows:
            console.print("[green]queue empty[/green]")


@app.command()
def approve(
    candidate_id: int = typer.Argument(...),
    skill: str = typer.Option(..., "--skill", help="existing canonical skill name"),
) -> None:
    """Attach a candidate's raw text to an existing canonical skill as an alias."""
    with session_scope() as session:
        candidate = _get_candidate(session, candidate_id)
        target = _skill_by_name(session, skill)
        if target is None:
            console.print(f"[red]no canonical skill named {skill!r}[/red]")
            console.print("use `jme taxonomy promote` to create a new one")
            raise typer.Exit(code=1)
        if len(candidate.raw_norm) > 128:
            console.print("[red]candidate text is too long to be an alias (>128 normalized)[/red]")
            raise typer.Exit(code=1)
        clash = session.scalar(
            select(SkillAlias).where(SkillAlias.alias_norm == candidate.raw_norm)
        )
        if clash is not None and clash.canonical_skill_id != target.id:
            other = session.get(CanonicalSkill, clash.canonical_skill_id)
            console.print(
                f"[red]alias {candidate.raw_norm!r} already belongs to "
                f"{other.name if other else clash.canonical_skill_id}[/red]"
            )
            raise typer.Exit(code=1)
        if clash is None:
            session.add(
                SkillAlias(
                    canonical_skill_id=target.id,
                    alias=candidate.raw_text[:128],
                    alias_norm=candidate.raw_norm,
                )
            )
        candidate.status = CandidateStatus.approved
        candidate.resolved_skill_id = target.id
        resolver.invalidate_cache()
        console.print(
            f"[green]approved[/green] {candidate.raw_text!r} -> {target.name} (id={target.id})"
        )
        console.print("[yellow]remember to add this alias to data/taxonomy/skills.yaml[/yellow]")


@app.command()
def promote(
    candidate_id: Annotated[int, typer.Argument()],
    name: Annotated[str, typer.Option("--name", help="new canonical skill name")],
    category: Annotated[SkillCategory, typer.Option("--category")],
    not_actionable: Annotated[
        bool, typer.Option("--not-actionable", help="exclude from the gap report")
    ] = False,
) -> None:
    """Create a NEW canonical skill from a candidate."""
    with session_scope() as session:
        candidate = _get_candidate(session, candidate_id)
        if _skill_by_name(session, name) is not None:
            console.print(f"[red]a canonical skill named {name!r} already exists[/red]")
            console.print("use `jme taxonomy approve` to attach the alias to it")
            raise typer.Exit(code=1)
        skill = CanonicalSkill(
            name=name,
            category=category,
            is_actionable=not not_actionable,
            notes=f"promoted from candidate {candidate.id}",
        )
        session.add(skill)
        session.flush()

        wanted = {normalize(name): name, candidate.raw_norm: candidate.raw_text[:128]}
        for alias_norm, display in wanted.items():
            if not alias_norm or len(alias_norm) > 128:
                continue
            exists = session.scalar(select(SkillAlias).where(SkillAlias.alias_norm == alias_norm))
            if exists is None:
                session.add(
                    SkillAlias(
                        canonical_skill_id=skill.id, alias=display, alias_norm=alias_norm
                    )
                )
        candidate.status = CandidateStatus.promoted
        candidate.resolved_skill_id = skill.id
        resolver.invalidate_cache()
        console.print(
            f"[green]promoted[/green] {candidate.raw_text!r} -> new skill "
            f"{skill.name} (id={skill.id}, {category.value}, actionable={not not_actionable})"
        )
        console.print("[yellow]remember to add this skill to data/taxonomy/skills.yaml[/yellow]")


@app.command()
def reject(candidate_id: int = typer.Argument(...)) -> None:
    """Mark a candidate as not a skill. It keeps counting occurrences but stays out of the way."""
    with session_scope() as session:
        candidate = _get_candidate(session, candidate_id)
        candidate.status = CandidateStatus.rejected
        console.print(f"[green]rejected[/green] {candidate.raw_text!r}")


@app.command()
def stats() -> None:
    """Counts by category, alias count, pending candidate count."""
    with session_scope() as session:
        rows = session.execute(
            select(
                CanonicalSkill.category,
                func.count(CanonicalSkill.id),
                func.sum(func.cast(CanonicalSkill.is_actionable, Integer)),
            ).group_by(CanonicalSkill.category)
        ).all()
        alias_count = session.scalar(select(func.count(SkillAlias.id))) or 0
        skill_count = session.scalar(select(func.count(CanonicalSkill.id))) or 0
        pending = (
            session.scalar(
                select(func.count(SkillAliasCandidate.id)).where(
                    SkillAliasCandidate.status == CandidateStatus.pending
                )
            )
            or 0
        )

        table = Table(title="taxonomy")
        table.add_column("category")
        table.add_column("skills", justify="right")
        table.add_column("actionable", justify="right")
        for category, count, actionable in sorted(rows, key=lambda r: r[0].value):
            table.add_row(category.value, str(count), str(int(actionable or 0)))
        table.add_section()
        table.add_row("[bold]total[/bold]", str(skill_count), "")
        console.print(table)
        console.print(f"aliases            : {alias_count}")
        console.print(f"pending candidates : {pending}")


if __name__ == "__main__":  # pragma: no cover
    app()
