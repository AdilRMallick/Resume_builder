"""Aggregate gap reporting: the payoff feature.

`gap.py` owns the rollup query, `trend.py` the 30-day frequency view, `render.py`
the terminal and JSON output, `cli.py` the commands.
"""

from __future__ import annotations

from jme.report.gap import (
    CoverageReport,
    GapReport,
    SkillGap,
    adapter_coverage,
    build_gap_report,
    coverage_report,
    explain_gap_query,
    gap_query_sql,
)
from jme.report.trend import SkillTrend, TrendReport, build_trend_report

__all__ = [
    "CoverageReport",
    "GapReport",
    "SkillGap",
    "SkillTrend",
    "TrendReport",
    "adapter_coverage",
    "build_gap_report",
    "build_trend_report",
    "coverage_report",
    "explain_gap_query",
    "gap_query_sql",
]
