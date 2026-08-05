"""Rendering is pure: no database, no fixtures, just shapes in and text out."""

from __future__ import annotations

import datetime as dt

from rich.console import Console

from jme.report.gap import AdapterCoverage, CoverageReport, GapReport, SkillGap
from jme.report.render import (
    SCHEMA_VERSION,
    gap_report_to_dict,
    metrics_table,
    render_coverage,
    render_gap,
    render_trend,
)
from jme.report.trend import SkillTrend, TrendReport

NOW = dt.datetime(2026, 8, 3, 12, 0, tzinfo=dt.UTC)


def _skill(name: str, *, required: int, status: str, matches: int = 1, **kw) -> SkillGap:
    return SkillGap(
        canonical_skill_id=abs(hash(name)) % 1000,
        skill=name,
        category=kw.get("category", "infra"),
        required_count=required,
        preferred_count=kw.get("preferred", 0),
        mentioned_count=kw.get("mentioned", 0),
        posting_count=kw.get("postings", required),
        status=status,
        evidenced_citations=kw.get("evidenced", 0),
        weak_citations=kw.get("weak", 0),
        absent_citations=kw.get("absent", 0),
        matches_considered=matches,
    )


def _report(**kw) -> GapReport:
    return GapReport(
        generated_at=NOW,
        evidence_version=kw.get("evidence_version", 3),
        eligible_posting_count=kw.get("eligible", 24),
        total_requirement_count=kw.get("total", 112),
        mapped_requirement_count=kw.get("mapped", 105),
        unmapped_requirement_count=kw.get("unmapped", 7),
        actionable_skill_count=len(kw.get("gaps", [])) + len(kw.get("covered", [])),
        gaps=kw.get("gaps", []),
        covered=kw.get("covered", []),
        query_seconds=0.1234,
    )


def test_json_counts_are_derived_not_restated() -> None:
    gaps = [_skill("Kubernetes", required=12, status="absent")]
    covered = [_skill("Python", required=20, status="evidenced", evidenced=1)]
    payload = gap_report_to_dict(_report(gaps=gaps, covered=covered), top=10)

    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["counts"]["taxonomy_coverage"] == 0.9375
    assert payload["counts"]["gaps"] == 1
    assert payload["counts"]["covered"] == 1
    assert payload["counts"]["gaps_returned"] == 1
    assert payload["query_seconds"] == 0.1234


def test_top_truncates_the_gap_list_but_not_the_counts() -> None:
    gaps = [_skill(f"s{i}", required=10 - i, status="absent") for i in range(10)]
    payload = gap_report_to_dict(_report(gaps=gaps), top=3)
    assert len(payload["gaps"]) == 3
    assert payload["counts"]["gaps"] == 10
    assert payload["counts"]["gaps_returned"] == 3


def test_zero_requirements_does_not_divide_by_zero() -> None:
    payload = gap_report_to_dict(_report(total=0, mapped=0, unmapped=0))
    assert payload["counts"]["taxonomy_coverage"] == 0.0


def test_gap_table_renders_empty_and_populated(capsys) -> None:
    console = Console(width=200)
    render_gap(console, _report(gaps=[]), top=5)
    assert "nothing to show" in capsys.readouterr().out

    render_gap(
        console,
        _report(gaps=[_skill("Kubernetes", required=12, status="absent", matches=0)]),
        top=5,
        include_covered=True,
    )
    out = capsys.readouterr().out
    assert "Kubernetes" in out
    # matches_considered == 0 must be visibly different from "evaluated and found nothing"
    assert "unseen" in out
    assert "taxonomy coverage 93.8%" in out


def test_trend_table_warns_when_the_window_predates_ingestion(capsys) -> None:
    console = Console(width=200)
    report = TrendReport(
        generated_at=NOW,
        days=30,
        half_days=15,
        bucket_days=7,
        bucket_starts=[NOW - dt.timedelta(days=7 * i) for i in (4, 3, 2, 1)],
        window_start=NOW - dt.timedelta(days=30),
        earliest_posting_seen_at=NOW - dt.timedelta(days=5),
        skills=[
            SkillTrend(
                canonical_skill_id=1,
                skill="Kubernetes",
                category="infra",
                weekly_counts=[0, 1, 2, 9],
                recent_count=11,
                prior_count=1,
                delta=10,
                direction="rising",
                required_count=8,
                dropped_count=2,
            )
        ],
    )
    render_trend(console, report)
    out = capsys.readouterr().out
    assert "warning" in out
    assert "Kubernetes" in out
    assert "+10" in out


def test_coverage_table_reports_the_honest_denominator(capsys) -> None:
    console = Console(width=200)
    render_coverage(
        console,
        CoverageReport(
            generated_at=NOW,
            active_posting_count=26,
            with_jd_row=21,
            resolved_count=18,
            by_adapter=[
                AdapterCoverage(adapter="greenhouse", postings=12, resolved=12),
                AdapterCoverage(adapter="(none)", postings=5, resolved=0),
            ],
            by_fetch_status={"ok": 18, "permanent_error": 3},
            total_requirement_count=112,
            mapped_requirement_count=105,
        ),
    )
    out = capsys.readouterr().out
    assert "18/26" in out
    assert "69.2%" in out
    assert "greenhouse" in out


def test_metrics_table_handles_no_rows() -> None:
    table = metrics_table([])
    assert table.row_count == 1
