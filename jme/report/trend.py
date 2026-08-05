"""Skill demand trend over a recent window.

--------------------------------------------------------------------------------------
What this actually measures - read before quoting a number from it
--------------------------------------------------------------------------------------
This is **not** a time series of skill demand. The database has no history table; a
posting has exactly one `first_seen_at` and one `last_seen_at`, and its requirement rows
are extracted once. So the only honest statement available is:

    "of the postings this system *first saw* in week W, N of them mention skill S"

Consequences that must not be papered over:

  * `first_seen_at` is when **the ingestor** first saw the posting, not when the company
    published it. Backfilling the feed, or a cold start, dumps thousands of postings into
    one bucket and makes every skill look like it spiked that week. A window that starts
    before the ingestor's own first run is meaningless, and `window_is_trustworthy`
    reports exactly that.
  * A posting contributes to exactly one bucket no matter how long it stays open. This
    measures *flow* (newly discovered demand), never *stock* (open roles right now).
  * Bucketing on `posting.first_seen_at` rather than `posting_requirement.created_at` is
    deliberate: re-running extraction over the backlog would otherwise manufacture a
    trend out of nothing but our own batch schedule.
  * Unlike the gap report, this **includes postings that have since gone inactive**.
    Demand that appeared and then vanished inside the window was still real demand, and
    filtering it out would bias every older bucket downwards (the longer ago a posting
    appeared, the likelier it has since been pulled), manufacturing a downward trend out
    of nothing. So the two reports deliberately count different populations.
  * `dropped_count` uses `last_seen_at` on postings that have gone inactive, i.e. roles
    mentioning the skill that fell out of the feed during the recent half-window. It is
    the closest thing to churn available, and it is a count of *disappearances*, not of
    filled roles.
  * Small numbers are noise. A skill going 1 -> 3 is not a 200% rise. The renderer shows
    raw counts and the delta, never a percentage, for that reason.

Comparison rule: the window is split in half. `recent` is the most recent
`days // 2` days, `prior` is the `days // 2` days before that, `delta = recent - prior`.
Equal-width halves, so the comparison is not skewed by an odd bucket the way a
"last week vs the other three weeks" split would be. Weekly buckets are for display.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass

from sqlalchemy import String, bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Session

from jme.logging import get_logger
from jme.report.gap import eligibility_params

log = get_logger(__name__)

DIRECTION_RISING = "rising"
DIRECTION_FALLING = "falling"
DIRECTION_FLAT = "flat"


@dataclass(frozen=True)
class SkillTrend:
    canonical_skill_id: int
    skill: str
    category: str
    #: weekly counts of newly-seen postings mentioning the skill, oldest bucket first
    weekly_counts: list[int]
    recent_count: int
    prior_count: int
    delta: int
    direction: str
    required_count: int
    dropped_count: int

    @property
    def total_count(self) -> int:
        return sum(self.weekly_counts)


@dataclass(frozen=True)
class TrendReport:
    generated_at: dt.datetime
    days: int
    half_days: int
    bucket_days: int
    bucket_starts: list[dt.datetime]
    window_start: dt.datetime
    earliest_posting_seen_at: dt.datetime | None
    skills: list[SkillTrend]

    @property
    def window_is_trustworthy(self) -> bool:
        """False when the ingestor has not been running for the whole window.

        If the oldest `first_seen_at` in the database is inside the window, the earliest
        buckets contain the cold-start backfill rather than real weekly demand.
        """
        if self.earliest_posting_seen_at is None:
            return False
        return self.earliest_posting_seen_at <= self.window_start


_TREND_SQL = """
WITH eligible AS (
    SELECT p.id, p.first_seen_at, p.last_seen_at, p.inactive_at
    FROM posting p
    WHERE (
            NOT :apply_eligibility
            OR (
                (cardinality(:role_types) = 0
                 OR p.role_type IS NULL
                 OR lower(p.role_type) = ANY(:role_types))
            AND (:allow_sponsorship_required
                 OR p.sponsorship IS NULL
                 OR NOT (p.sponsorship ILIKE ANY(:sponsorship_blocklist)))
            AND (cardinality(:location_patterns) = 0
                 OR p.is_remote
                 OR p.locations IS NULL
                 OR jsonb_typeof(p.locations) <> 'array'
                 OR jsonb_array_length(p.locations) = 0
                 OR EXISTS (
                        SELECT 1
                        FROM jsonb_array_elements_text(p.locations) AS loc(name)
                        WHERE loc.name ILIKE ANY(:location_patterns)
                    ))
            )
      )
),
windowed AS (
    SELECT e.id,
           floor(extract(epoch FROM (CAST(:now AS timestamptz) - e.first_seen_at)) / 86400.0)::int AS days_ago
    FROM eligible e
    WHERE e.first_seen_at >= CAST(:window_start AS timestamptz)
      AND e.first_seen_at <= CAST(:now AS timestamptz)
),
mentions AS (
    SELECT DISTINCT r.canonical_skill_id, w.id AS posting_id, w.days_ago
    FROM posting_requirement r
    JOIN windowed w ON w.id = r.posting_id
    WHERE r.canonical_skill_id IS NOT NULL
),
required AS (
    SELECT DISTINCT r.canonical_skill_id, w.id AS posting_id
    FROM posting_requirement r
    JOIN windowed w ON w.id = r.posting_id
    WHERE r.canonical_skill_id IS NOT NULL
      AND r.importance = 'required'
),
dropped AS (
    SELECT r.canonical_skill_id, count(DISTINCT e.id) AS dropped_count
    FROM eligible e
    JOIN posting_requirement r ON r.posting_id = e.id
    WHERE e.inactive_at IS NOT NULL
      AND e.last_seen_at >= CAST(:half_start AS timestamptz)
      AND r.canonical_skill_id IS NOT NULL
    GROUP BY r.canonical_skill_id
),
agg AS (
    SELECT m.canonical_skill_id AS skill_id,
           count(*)                                                  AS total_count,
           count(*) FILTER (WHERE m.days_ago < :half_days)           AS recent_count,
           count(*) FILTER (WHERE m.days_ago >= :half_days)          AS prior_count,
           array_agg(m.days_ago)                                     AS days_ago_list
    FROM mentions m
    GROUP BY m.canonical_skill_id
)
SELECT s.id                                   AS canonical_skill_id,
       s.name                                 AS skill,
       s.category::text                       AS category,
       a.total_count,
       a.recent_count,
       a.prior_count,
       a.days_ago_list,
       coalesce((SELECT count(*) FROM required q WHERE q.canonical_skill_id = s.id), 0) AS required_count,
       coalesce(d.dropped_count, 0)           AS dropped_count
FROM agg a
JOIN canonical_skill s ON s.id = a.skill_id
LEFT JOIN dropped d ON d.canonical_skill_id = s.id
WHERE s.is_actionable
ORDER BY (a.recent_count - a.prior_count) DESC, a.total_count DESC, s.name ASC
"""

_EARLIEST_SQL = "SELECT min(first_seen_at) FROM posting"


def build_trend_report(
    session: Session,
    *,
    days: int = 30,
    now: dt.datetime | None = None,
    apply_eligibility: bool = True,
) -> TrendReport:
    """Weekly demand buckets over the last `days`, plus a recent-vs-prior half delta."""
    if days < 2:
        raise ValueError("days must be at least 2 so the window can be split in half")

    now = now or dt.datetime.now(dt.UTC)
    half_days = days // 2
    window_start = now - dt.timedelta(days=days)
    half_start = now - dt.timedelta(days=half_days)
    bucket_days = 7
    n_buckets = max(1, math.ceil(days / bucket_days))

    params = dict(eligibility_params(apply_eligibility))
    params.update(
        {
            "now": now,
            "window_start": window_start,
            "half_start": half_start,
            "half_days": half_days,
        }
    )
    stmt = text(_TREND_SQL).bindparams(
        bindparam("role_types", type_=ARRAY(String)),
        bindparam("sponsorship_blocklist", type_=ARRAY(String)),
        bindparam("location_patterns", type_=ARRAY(String)),
    )
    rows = session.execute(stmt, params).mappings().all()
    earliest = session.execute(text(_EARLIEST_SQL)).scalar()

    skills: list[SkillTrend] = []
    for row in rows:
        buckets = [0] * n_buckets
        for days_ago in row["days_ago_list"] or []:
            idx = min(int(days_ago) // bucket_days, n_buckets - 1)
            buckets[idx] += 1
        delta = int(row["recent_count"]) - int(row["prior_count"])
        skills.append(
            SkillTrend(
                canonical_skill_id=row["canonical_skill_id"],
                skill=row["skill"],
                category=row["category"],
                # stored newest-bucket-first by construction; flip to oldest-first for display
                weekly_counts=list(reversed(buckets)),
                recent_count=int(row["recent_count"]),
                prior_count=int(row["prior_count"]),
                delta=delta,
                direction=(
                    DIRECTION_RISING
                    if delta > 0
                    else DIRECTION_FALLING
                    if delta < 0
                    else DIRECTION_FLAT
                ),
                required_count=int(row["required_count"]),
                dropped_count=int(row["dropped_count"]),
            )
        )

    # oldest first. the oldest bucket is clamped to the window start, since a 30-day
    # window split into 7-day buckets leaves the last one partial.
    bucket_starts = [
        max(window_start, now - dt.timedelta(days=bucket_days * (i + 1)))
        for i in reversed(range(n_buckets))
    ]
    report = TrendReport(
        generated_at=now,
        days=days,
        half_days=half_days,
        bucket_days=bucket_days,
        bucket_starts=bucket_starts,
        window_start=window_start,
        earliest_posting_seen_at=earliest,
        skills=skills,
    )
    log.info(
        "trend_report_built",
        days=days,
        skills=len(skills),
        trustworthy=report.window_is_trustworthy,
    )
    return report
