"""The 30-day trend view.

The fixture puts postings 1..12 three days ago and 13..24 twenty days ago, so with a
30-day window (halved at 15 days) every skill's recent/prior split is arithmetic.
"""

from __future__ import annotations

import datetime as dt

import pytest

from jme.report.render import trend_report_to_dict
from jme.report.trend import build_trend_report

pytestmark = pytest.mark.integration


def _by_name(report) -> dict:
    return {s.skill: s for s in report.skills}


def test_recent_versus_prior_split_is_arithmetic(db_session, seeded) -> None:
    trend = _by_name(build_trend_report(db_session, days=30, now=seeded.now))

    # Kubernetes is required on postings 1..12 (first seen 3 days ago) plus the one
    # inactive posting (first seen 5 days ago). The trend counts postings that have since
    # gone inactive on purpose - see the module docstring - so the recent count is 13,
    # one higher than the gap report's required_count of 12.
    kubernetes = trend["Kubernetes"]
    assert (kubernetes.recent_count, kubernetes.prior_count) == (13, 0)
    assert kubernetes.delta == 13
    assert kubernetes.direction == "rising"

    # Python spans postings 1..20: 12 recent, 8 prior
    python = trend["Python"]
    assert (python.recent_count, python.prior_count) == (12, 8)
    assert python.delta == 4
    assert python.direction == "rising"


def test_weekly_buckets_are_oldest_first_and_sum_to_the_total(db_session, seeded) -> None:
    report = build_trend_report(db_session, days=30, now=seeded.now)
    python = _by_name(report)["Python"]

    assert len(python.weekly_counts) == len(report.bucket_starts)
    assert sum(python.weekly_counts) == python.total_count == 20
    # 3 days ago lands in the newest bucket, 20 days ago in the third-newest
    assert python.weekly_counts[-1] == 12
    assert python.weekly_counts[-3] == 8
    assert report.bucket_starts == sorted(report.bucket_starts)


def test_non_actionable_skills_are_excluded_here_too(db_session, seeded) -> None:
    assert "Excellent Communication" not in _by_name(build_trend_report(db_session, days=30,
                                                                       now=seeded.now))


def test_ordering_is_by_delta(db_session, seeded) -> None:
    report = build_trend_report(db_session, days=30, now=seeded.now)
    deltas = [s.delta for s in report.skills]
    assert deltas == sorted(deltas, reverse=True)


def test_window_excludes_postings_older_than_the_window(db_session, seeded) -> None:
    """A 10-day window (halved at 5 days) sees only the 3-day-old postings."""
    trend = _by_name(build_trend_report(db_session, days=10, now=seeded.now))
    python = trend["Python"]
    assert python.total_count == 12
    assert (python.recent_count, python.prior_count) == (12, 0)


def test_window_is_not_trustworthy_when_ingestion_is_younger_than_the_window(
    db_session, seeded
) -> None:
    """The oldest posting is 20 days old, so a 30-day window is mostly cold start.

    Reporting that honestly is the difference between a trend and a graph of when I
    happened to start running the ingestor.
    """
    assert build_trend_report(db_session, days=30, now=seeded.now).window_is_trustworthy is False
    assert build_trend_report(db_session, days=10, now=seeded.now).window_is_trustworthy is True


def test_dropped_count_uses_last_seen_at_on_inactive_postings(db_session, seeded) -> None:
    report = build_trend_report(db_session, days=30, now=seeded.now)
    # the one inactive fixture posting requires Kubernetes and was last seen yesterday
    assert _by_name(report)["Kubernetes"].dropped_count == 1
    assert _by_name(report)["Python"].dropped_count == 0


def test_empty_window_is_not_an_error(db_session, seeded) -> None:
    future = seeded.now + dt.timedelta(days=365)
    report = build_trend_report(db_session, days=30, now=future)
    assert report.skills == []


def test_days_must_allow_a_split(db_session) -> None:
    with pytest.raises(ValueError, match="at least 2"):
        build_trend_report(db_session, days=1)


def test_trend_json_shape(db_session, seeded) -> None:
    payload = trend_report_to_dict(build_trend_report(db_session, days=30, now=seeded.now), top=2)
    assert payload["kind"] == "trend_report"
    assert payload["window"]["days"] == 30
    assert payload["window"]["half_days"] == 15
    assert payload["window"]["is_trustworthy"] is False
    assert len(payload["skills"]) == 2
    assert set(payload["skills"][0]) == {
        "canonical_skill_id", "skill", "category", "weekly_counts", "total_count",
        "recent_count", "prior_count", "delta", "direction", "required_count",
        "dropped_count",
    }
