"""Terminal tables and the JSON export.

--------------------------------------------------------------------------------------
JSON export schema (stable; `schema_version` is bumped on any breaking change)
--------------------------------------------------------------------------------------
```jsonc
{
  "schema_version": "1.0",          // string, semver-ish. minor = additive fields only
  "kind": "gap_report",
  "generated_at": "2026-08-03T21:00:00+00:00",   // ISO-8601, always UTC, always offset-aware
  "evidence_version": 7,            // corpus version the statuses were computed against
  "status_rule": "best-status-...", // one-line restatement of the derivation rule
  "counts": {
    "eligible_postings": 3000,      // active postings passing the config eligibility filters
    "requirements_total": 41230,    // requirement rows on those postings
    "requirements_mapped": 37110,   // ... with a canonical_skill_id
    "requirements_unmapped": 4120,  // ... without one. these are invisible to the ranking
    "taxonomy_coverage": 0.9001,    // mapped / total, 0.0 when total is 0
    "actionable_skills": 214,       // actionable skills appearing at least once
    "gaps": 180,                    // skills whose status is absent or weak
    "covered": 34,                  // skills whose status is evidenced
    "gaps_returned": 20             // length of the "gaps" array after --top
  },
  "query_seconds": 0.184,           // wall time of the single rollup statement
  "gaps": [ <skill>, ... ],         // ranked: required_count desc, then preferred, mentioned
  "covered": [ <skill>, ... ]       // present only when include_covered is true
}

<skill> = {
  "canonical_skill_id": 42, "skill": "Kubernetes", "category": "infra",
  "required_count": 118, "preferred_count": 31, "mentioned_count": 4,
  "posting_count": 140,
  "status": "absent",               // evidenced | weak | absent
  "evidenced_citations": 0, "weak_citations": 2, "absent_citations": 19,
  "matches_considered": 21,         // 0 means never evaluated, not "evaluated and empty"
  "is_gap": true
}
```
Consumers may rely on: field names, the ordering of `gaps`, and UTC ISO-8601 timestamps.
Consumers may not rely on: `canonical_skill_id` values across taxonomy rebuilds.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

from jme.report.gap import CoverageReport, GapReport, SkillGap
from jme.report.trend import TrendReport

SCHEMA_VERSION = "1.0"

STATUS_RULE = (
    "best status achieved for the skill across all non-stale matches "
    "(evidenced > weak > absent); no citations at all reads as absent"
)

_STATUS_STYLE = {
    "evidenced": "green",
    "weak": "yellow",
    "absent": "red",
}


# --------------------------------------------------------------------------------------
# JSON
# --------------------------------------------------------------------------------------


def gap_report_to_dict(
    report: GapReport, *, top: int | None = None, include_covered: bool = False
) -> dict[str, Any]:
    gaps = report.top_gaps(top)
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": "gap_report",
        "generated_at": report.generated_at.isoformat(),
        "evidence_version": report.evidence_version,
        "status_rule": STATUS_RULE,
        "counts": {
            "eligible_postings": report.eligible_posting_count,
            "requirements_total": report.total_requirement_count,
            "requirements_mapped": report.mapped_requirement_count,
            "requirements_unmapped": report.unmapped_requirement_count,
            "taxonomy_coverage": round(report.taxonomy_coverage, 4),
            "actionable_skills": report.actionable_skill_count,
            "gaps": len(report.gaps),
            "covered": len(report.covered),
            "gaps_returned": len(gaps),
        },
        "query_seconds": round(report.query_seconds, 4),
        "gaps": [g.as_dict() for g in gaps],
    }
    if include_covered:
        payload["covered"] = [g.as_dict() for g in report.covered]
    return payload


def write_gap_json(
    report: GapReport, path: str | Path, *, top: int | None = None, include_covered: bool = False
) -> Path:
    target = Path(path)
    if target.parent != Path(""):
        target.parent.mkdir(parents=True, exist_ok=True)
    payload = gap_report_to_dict(report, top=top, include_covered=include_covered)
    target.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return target


def trend_report_to_dict(report: TrendReport, *, top: int | None = None) -> dict[str, Any]:
    skills = report.skills if top is None else report.skills[:top]
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "trend_report",
        "generated_at": report.generated_at.isoformat(),
        "window": {
            "days": report.days,
            "half_days": report.half_days,
            "bucket_days": report.bucket_days,
            "window_start": report.window_start.isoformat(),
            "bucket_starts": [b.isoformat() for b in report.bucket_starts],
            "is_trustworthy": report.window_is_trustworthy,
            "measures": (
                "postings first seen by the ingestor in each bucket that mention the "
                "skill; flow of newly discovered demand, not open-role stock"
            ),
        },
        "skills": [
            {
                "canonical_skill_id": s.canonical_skill_id,
                "skill": s.skill,
                "category": s.category,
                "weekly_counts": s.weekly_counts,
                "total_count": s.total_count,
                "recent_count": s.recent_count,
                "prior_count": s.prior_count,
                "delta": s.delta,
                "direction": s.direction,
                "required_count": s.required_count,
                "dropped_count": s.dropped_count,
            }
            for s in skills
        ],
    }


# --------------------------------------------------------------------------------------
# terminal
# --------------------------------------------------------------------------------------


def _status_cell(item: SkillGap) -> str:
    style = _STATUS_STYLE.get(item.status, "white")
    suffix = "" if item.matches_considered else " (unseen)"
    return f"[{style}]{item.status}{suffix}[/{style}]"


def gap_table(items: list[SkillGap], title: str) -> Table:
    table = Table(title=title, header_style="bold", title_justify="left")
    table.add_column("#", justify="right", style="dim")
    table.add_column("skill")
    table.add_column("category", style="dim")
    table.add_column("required", justify="right", style="bold")
    table.add_column("preferred", justify="right")
    table.add_column("mentioned", justify="right")
    table.add_column("postings", justify="right")
    table.add_column("status")
    table.add_column("citations e/w/a", justify="right", style="dim")
    for i, item in enumerate(items, start=1):
        table.add_row(
            str(i),
            item.skill,
            item.category,
            str(item.required_count),
            str(item.preferred_count),
            str(item.mentioned_count),
            str(item.posting_count),
            _status_cell(item),
            f"{item.evidenced_citations}/{item.weak_citations}/{item.absent_citations}",
        )
    if not items:
        table.add_row("-", "[dim]nothing to show[/dim]", "", "", "", "", "", "", "")
    return table


def render_gap(
    console: Console, report: GapReport, *, top: int | None = 20, include_covered: bool = False
) -> None:
    console.print(
        f"[bold]gap report[/bold]  generated {report.generated_at.isoformat(timespec='seconds')}"
        f"  evidence_version={report.evidence_version}"
    )
    console.print(
        f"[dim]{report.eligible_posting_count} eligible active postings, "
        f"{report.total_requirement_count} requirements, "
        f"taxonomy coverage {report.taxonomy_coverage:.1%} "
        f"({report.unmapped_requirement_count} unmapped and therefore invisible below), "
        f"query {report.query_seconds * 1000:.0f} ms[/dim]"
    )
    console.print(f"[dim]status rule: {STATUS_RULE}[/dim]")
    console.print()
    console.print(gap_table(report.top_gaps(top), "ranked gaps (absent or weak, by required)"))
    if include_covered:
        console.print()
        console.print(gap_table(report.covered, "covered (evidenced)"))


def render_trend(console: Console, report: TrendReport, *, top: int | None = 20) -> None:
    n_buckets = len(report.bucket_starts)
    console.print(
        f"[bold]skill trend[/bold]  last {report.days} days, {n_buckets} weekly buckets, "
        f"recent {report.half_days}d vs prior {report.half_days}d"
    )
    console.print(
        "[dim]measures postings the ingestor first saw in each bucket that mention the "
        "skill - flow of new demand, not open-role stock[/dim]"
    )
    if not report.window_is_trustworthy:
        console.print(
            "[yellow]warning: the ingestor has not been running for this whole window; "
            "the oldest buckets are cold-start backfill, not real weekly demand[/yellow]"
        )
    table = Table(header_style="bold", title_justify="left")
    table.add_column("skill")
    table.add_column("category", style="dim")
    for start in report.bucket_starts:
        table.add_column(start.strftime("%m-%d"), justify="right", style="dim")
    table.add_column("total", justify="right")
    table.add_column("recent", justify="right")
    table.add_column("prior", justify="right")
    table.add_column("delta", justify="right", style="bold")
    table.add_column("direction")
    table.add_column("dropped", justify="right", style="dim")

    skills = report.skills if top is None else report.skills[:top]
    for s in skills:
        style = {"rising": "green", "falling": "red"}.get(s.direction, "dim")
        table.add_row(
            s.skill,
            s.category,
            *[str(c) for c in s.weekly_counts],
            str(s.total_count),
            str(s.recent_count),
            str(s.prior_count),
            f"{s.delta:+d}",
            f"[{style}]{s.direction}[/{style}]",
            str(s.dropped_count),
        )
    if not skills:
        console.print("[dim]no postings first seen inside the window[/dim]")
        return
    console.print(table)


def render_coverage(console: Console, report: CoverageReport) -> None:
    console.print(
        f"[bold]coverage[/bold]  generated {report.generated_at.isoformat(timespec='seconds')}"
    )
    console.print(
        f"adapter coverage [bold]{report.adapter_coverage:.1%}[/bold] "
        f"({report.resolved_count}/{report.active_posting_count} active postings resolved "
        f"to JD text)"
    )
    console.print(
        f"taxonomy coverage [bold]{report.taxonomy_coverage:.1%}[/bold] "
        f"({report.mapped_requirement_count}/{report.total_requirement_count} requirements "
        f"resolved to a canonical skill)"
    )
    console.print()

    table = Table(title="by adapter", header_style="bold", title_justify="left")
    table.add_column("adapter")
    table.add_column("postings", justify="right")
    table.add_column("resolved", justify="right")
    table.add_column("coverage", justify="right", style="bold")
    for row in report.by_adapter:
        table.add_row(row.adapter, str(row.postings), str(row.resolved), f"{row.coverage:.1%}")
    if not report.by_adapter:
        table.add_row("-", "0", "0", "0.0%")
    console.print(table)

    status_table = Table(title="by fetch status", header_style="bold", title_justify="left")
    status_table.add_column("fetch_status")
    status_table.add_column("postings", justify="right")
    for status, count in sorted(report.by_fetch_status.items(), key=lambda kv: -kv[1]):
        status_table.add_row(status, str(count))
    if not report.by_fetch_status:
        status_table.add_row("-", "0")
    console.print()
    console.print(status_table)


def metrics_table(rows: list[dict[str, Any]]) -> Table:
    table = Table(title="run_metric", header_style="bold", title_justify="left")
    table.add_column("recorded_at", style="dim")
    table.add_column("run_id")
    table.add_column("stage")
    table.add_column("metric")
    table.add_column("value", justify="right")
    table.add_column("labels", style="dim")
    for row in rows:
        recorded: dt.datetime | None = row.get("recorded_at")
        table.add_row(
            recorded.isoformat(timespec="seconds") if recorded else "",
            str(row.get("run_id", "")),
            str(row.get("stage", "")),
            str(row.get("metric", "")),
            f"{float(row.get('value', 0)):g}",
            json.dumps(row.get("labels")) if row.get("labels") else "",
        )
    if not rows:
        table.add_row("-", "no metrics recorded", "", "", "", "")
    return table
