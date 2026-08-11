"""Daily, deterministic read model over the shortlist, matches, and gap report.

The digest deliberately makes no LLM call.  It only assembles already-grounded match
rows and their citations, so generating it is free, repeatable, and safe to schedule.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from jme.models import (
    CanonicalSkill,
    EvidenceChunk,
    Match,
    MatchCitation,
    Posting,
    ShortlistEntry,
)
from jme.report.gap import SkillGap, build_gap_report

DIGEST_SCHEMA_VERSION = "1.0"
DigestFormat = Literal["markdown", "json"]


@dataclass(frozen=True)
class DigestCitation:
    skill: str
    status: str
    evidence_chunk_id: int | None
    source_ref: str | None
    reasoning: str | None


@dataclass(frozen=True)
class DigestRole:
    rank: int
    posting_id: int
    company: str
    title: str
    url: str
    coarse_score: float
    match_id: int | None
    verdict: str | None
    match_score: float | None
    rationale: str | None
    match_stale: bool
    computed_at: dt.datetime | None
    citations: list[DigestCitation] = field(default_factory=list)

    @property
    def evidenced_count(self) -> int:
        return sum(c.status == "evidenced" for c in self.citations)

    @property
    def weak_count(self) -> int:
        return sum(c.status == "weak" for c in self.citations)

    @property
    def absent_count(self) -> int:
        return sum(c.status == "absent" for c in self.citations)


@dataclass(frozen=True)
class DigestReport:
    generated_at: dt.datetime
    shortlist_run_id: str | None
    evidence_version: int
    shortlisted_count: int
    matched_count: int
    stale_match_count: int
    unmatched_count: int
    live_evidence_chunks: int
    placeholder_evidence_chunks: int
    roles: list[DigestRole]
    gaps: list[SkillGap]
    total_gap_count: int
    taxonomy_coverage: float
    warnings: list[str]
    next_actions: list[str]


def _latest_run_id(session: Session) -> str | None:
    return session.scalar(
        select(ShortlistEntry.run_id)
        .group_by(ShortlistEntry.run_id)
        .order_by(func.max(ShortlistEntry.created_at).desc())
        .limit(1)
    )


def _rationale_text(match: Match) -> str | None:
    rationale = match.rationale or {}
    for key in ("text", "summary"):
        value = rationale.get(key)
        if value:
            return str(value).strip()
    return None


def build_digest_report(
    session: Session,
    *,
    run_id: str | None = None,
    top_roles: int = 10,
    top_gaps: int = 10,
    apply_eligibility: bool = True,
    now: dt.datetime | None = None,
) -> DigestReport:
    """Assemble the latest shortlist, latest match per role, and current gaps."""
    if top_roles < 1:
        raise ValueError("top_roles must be at least 1")
    if top_gaps < 1:
        raise ValueError("top_gaps must be at least 1")

    resolved_run = run_id or _latest_run_id(session)
    entries: list[tuple[ShortlistEntry, Posting]] = []
    if resolved_run is not None:
        entries = list(
            session.execute(
                select(ShortlistEntry, Posting)
                .join(Posting, Posting.id == ShortlistEntry.posting_id)
                .where(ShortlistEntry.run_id == resolved_run)
                .order_by(ShortlistEntry.rank.asc(), ShortlistEntry.id.asc())
            ).all()
        )

    posting_ids = [entry.posting_id for entry, _ in entries]
    latest_matches: dict[int, Match] = {}
    if posting_ids:
        matches = session.scalars(
            select(Match)
            .where(Match.posting_id.in_(posting_ids))
            .order_by(Match.posting_id, Match.computed_at.desc(), Match.id.desc())
        ).all()
        for match in matches:
            latest_matches.setdefault(match.posting_id, match)

    match_ids = [match.id for match in latest_matches.values()]
    citations_by_match: dict[int, list[DigestCitation]] = {mid: [] for mid in match_ids}
    if match_ids:
        citation_rows = session.execute(
            select(
                MatchCitation,
                CanonicalSkill.name,
                EvidenceChunk.source_ref,
            )
            .outerjoin(CanonicalSkill, CanonicalSkill.id == MatchCitation.canonical_skill_id)
            .outerjoin(EvidenceChunk, EvidenceChunk.id == MatchCitation.evidence_chunk_id)
            .where(MatchCitation.match_id.in_(match_ids))
            .order_by(MatchCitation.match_id, MatchCitation.id)
        ).all()
        for citation, skill_name, source_ref in citation_rows:
            citations_by_match[citation.match_id].append(
                DigestCitation(
                    skill=skill_name or "(unmapped requirement)",
                    status=citation.status.value,
                    evidence_chunk_id=citation.evidence_chunk_id,
                    source_ref=source_ref,
                    reasoning=(citation.reasoning or "").strip() or None,
                )
            )

    gap_report = build_gap_report(session, apply_eligibility=apply_eligibility)
    evidence_counts = session.execute(
        select(
            func.count(EvidenceChunk.id),
            func.count(EvidenceChunk.id).filter(EvidenceChunk.text.ilike("%PLACEHOLDER%")),
        ).where(EvidenceChunk.deleted_at.is_(None))
    ).one()
    live_chunks = int(evidence_counts[0] or 0)
    placeholder_chunks = int(evidence_counts[1] or 0)

    roles: list[DigestRole] = []
    stale_count = 0
    matched_count = 0
    for entry, posting in entries:
        match = latest_matches.get(posting.id)
        stale = bool(
            match
            and (match.is_stale or match.evidence_version != gap_report.evidence_version)
        )
        matched_count += match is not None
        stale_count += stale
        roles.append(
            DigestRole(
                rank=entry.rank,
                posting_id=posting.id,
                company=posting.company,
                title=posting.title,
                url=posting.url,
                coarse_score=float(entry.coarse_score),
                match_id=match.id if match else None,
                verdict=match.verdict.value if match and match.verdict else None,
                match_score=float(match.score) if match and match.score is not None else None,
                rationale=_rationale_text(match) if match else None,
                match_stale=stale,
                computed_at=match.computed_at if match else None,
                citations=citations_by_match.get(match.id, []) if match else [],
            )
        )

    shortlisted_count = len(entries)
    unmatched_count = shortlisted_count - matched_count
    warnings: list[str] = []
    if resolved_run is None:
        warnings.append("No shortlist exists yet; run `jme rank run`.")
    elif not entries:
        warnings.append(f"Shortlist run `{resolved_run}` has no entries.")
    if live_chunks == 0:
        warnings.append("The evidence corpus is empty; run `jme evidence ingest`.")
    elif placeholder_chunks:
        warnings.append(
            f"{placeholder_chunks} live evidence chunk(s) still contain PLACEHOLDER text."
        )
    if unmatched_count:
        warnings.append(f"{unmatched_count} shortlisted role(s) have not been matched.")
    if stale_count:
        warnings.append(f"{stale_count} shortlisted role(s) have a stale match.")

    next_actions: list[str] = []
    if placeholder_chunks:
        next_actions.append("Replace placeholder evidence, then run `jme evidence ingest`.")
    if unmatched_count and resolved_run:
        next_actions.append(f"Run `jme match shortlist --run-id {resolved_run}`.")
    if stale_count:
        next_actions.append(f"Run `jme match stale --limit {stale_count}`.")
    top_gap_rows = gap_report.top_gaps(top_gaps)
    if top_gap_rows:
        gap = top_gap_rows[0]
        next_actions.append(
            f"Close or document the top gap: {gap.skill} is required in "
            f"{gap.required_count} eligible posting(s)."
        )
    if not next_actions and roles:
        next_actions.append(f"Review and tailor for the top role: {roles[0].company} — {roles[0].title}.")

    return DigestReport(
        generated_at=now or dt.datetime.now(dt.UTC),
        shortlist_run_id=resolved_run,
        evidence_version=gap_report.evidence_version,
        shortlisted_count=shortlisted_count,
        matched_count=matched_count,
        stale_match_count=stale_count,
        unmatched_count=unmatched_count,
        live_evidence_chunks=live_chunks,
        placeholder_evidence_chunks=placeholder_chunks,
        roles=roles[:top_roles],
        gaps=top_gap_rows,
        total_gap_count=len(gap_report.gaps),
        taxonomy_coverage=gap_report.taxonomy_coverage,
        warnings=warnings,
        next_actions=next_actions,
    )


def digest_report_to_dict(report: DigestReport) -> dict[str, Any]:
    return {
        "schema_version": DIGEST_SCHEMA_VERSION,
        "kind": "daily_digest",
        "generated_at": report.generated_at.isoformat(),
        "shortlist_run_id": report.shortlist_run_id,
        "evidence_version": report.evidence_version,
        "counts": {
            "shortlisted": report.shortlisted_count,
            "roles_returned": len(report.roles),
            "matched": report.matched_count,
            "stale_matches": report.stale_match_count,
            "unmatched": report.unmatched_count,
            "live_evidence_chunks": report.live_evidence_chunks,
            "placeholder_evidence_chunks": report.placeholder_evidence_chunks,
            "gaps": report.total_gap_count,
            "gaps_returned": len(report.gaps),
            "taxonomy_coverage": round(report.taxonomy_coverage, 4),
        },
        "warnings": list(report.warnings),
        "roles": [
            {
                **{key: value for key, value in dataclasses.asdict(role).items() if key != "citations"},
                "computed_at": role.computed_at.isoformat() if role.computed_at else None,
                "evidenced_count": role.evidenced_count,
                "weak_count": role.weak_count,
                "absent_count": role.absent_count,
                "citations": [dataclasses.asdict(citation) for citation in role.citations],
            }
            for role in report.roles
        ],
        "gaps": [gap.as_dict() for gap in report.gaps],
        "next_actions": list(report.next_actions),
    }


def render_digest_markdown(report: DigestReport) -> str:
    lines = [
        f"# Job match digest — {report.generated_at.date().isoformat()}",
        "",
        f"Shortlist `{report.shortlist_run_id or 'none'}` · evidence v{report.evidence_version} · "
        f"{report.matched_count}/{report.shortlisted_count} matched · "
        f"{report.total_gap_count} current gaps",
    ]
    if report.warnings:
        lines.extend(["", "## Attention", ""])
        lines.extend(f"> {warning}" for warning in report.warnings)

    lines.extend(["", "## Best matches", ""])
    if not report.roles:
        lines.append("No shortlisted roles yet.")
    for role in report.roles:
        verdict = role.verdict or "not matched"
        score = f" · match {role.match_score:.0%}" if role.match_score is not None else ""
        stale = " · **STALE**" if role.match_stale else ""
        lines.append(
            f"### {role.rank}. [{role.company} — {role.title}]({role.url})"
        )
        lines.append("")
        lines.append(f"{verdict}{score} · coarse {role.coarse_score:.0%}{stale}")
        if role.rationale:
            lines.extend(["", role.rationale])
        if role.citations:
            lines.extend(["", "| Skill | Status | Evidence |", "|---|---|---|"])
            for citation in role.citations:
                evidence = "—"
                if citation.evidence_chunk_id:
                    evidence = f"{citation.source_ref or 'chunk'} #{citation.evidence_chunk_id}"
                if citation.reasoning:
                    evidence += f" — {citation.reasoning}"
                cells = (citation.skill, citation.status, evidence)
                lines.append("| " + " | ".join(_markdown_cell(cell) for cell in cells) + " |")
        lines.append("")

    lines.extend(["## Top skill gaps", ""])
    if report.gaps:
        lines.extend(["| Skill | Status | Required | Preferred | Postings |", "|---|---|---:|---:|---:|"])
        lines.extend(
            f"| {_markdown_cell(gap.skill)} | {gap.status} | {gap.required_count} | "
            f"{gap.preferred_count} | {gap.posting_count} |"
            for gap in report.gaps
        )
    else:
        lines.append("No actionable gaps in the current eligible set.")

    lines.extend(["", "## Next actions", ""])
    if report.next_actions:
        lines.extend(f"{index}. {action}" for index, action in enumerate(report.next_actions, 1))
    else:
        lines.append("Nothing queued.")
    return "\n".join(lines).rstrip() + "\n"


def _markdown_cell(value: str) -> str:
    """Keep data-controlled pipes and newlines from corrupting digest tables."""
    return value.replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def write_digest(report: DigestReport, path: str | Path, *, format: DigestFormat) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    content = (
        render_digest_markdown(report)
        if format == "markdown"
        else json.dumps(digest_report_to_dict(report), indent=2) + "\n"
    )
    target.write_text(content, encoding="utf-8")
    return target
