"""The aggregate gap rollup.

What this answers: *across every eligible active posting, which actionable skills are
demanded most often that my evidence corpus cannot back up?* That ranked list is the
entire point of the project.

--------------------------------------------------------------------------------------
The "my status" derivation rule (read this before trusting a number)
--------------------------------------------------------------------------------------
Per canonical skill, status is the **best status ever achieved for that skill across
all non-stale matches**, on the ordering ``evidenced > weak > absent``. A skill with no
citation rows at all is reported as ``absent``.

Why "best ever" and not "most recent" or "majority":

  * Evidence is a property of *me*, not of a posting. If any match found a real
    evidence chunk for Kubernetes and cited it, I have Kubernetes evidence. Another
    posting's match saying ``absent`` for the same skill is far more likely a retrieval
    miss (top-K did not surface the chunk, or the JD phrased it oddly) than proof the
    evidence stopped existing.
  * ``evidenced`` is the only status that is *hard*: the schema check constraint
    ``ck_evidenced_requires_chunk`` makes an evidenced citation impossible without a
    concrete ``evidence_chunk_id``. So "best ever" only ever promotes a skill out of the
    gap list on the strength of a real citation, never on the strength of a model's
    optimism.
  * The failure mode this rule accepts, stated honestly: one lucky citation is enough to
    drop a skill off the gap list even if twenty other matches said absent. That is the
    right trade for a *personal* report whose cost of a false gap (I spend a weekend
    relearning something I can already prove) is higher than the cost of a false
    non-gap. ``evidenced_citations`` / ``weak_citations`` / ``absent_citations`` are
    returned alongside the status so the thin cases are visible rather than hidden.
  * "No citations at all" collapses to ``absent`` because for a *gap* report the honest
    default is "nothing backs this up". ``matches_considered == 0`` distinguishes
    "never even looked" from "looked and found nothing", and the renderer surfaces it.

Staleness: only matches with ``is_stale = false`` count. Task 7 marks matches stale on
an ``evidence_version`` bump, so this is what keeps the report from claiming gaps I have
already closed (ARCHITECTURE section 4). Matches for inactive postings still contribute
status - they are evidence about me, and the recompute sweep deliberately ignores them.

--------------------------------------------------------------------------------------
Shape of the work
--------------------------------------------------------------------------------------
One SQL statement does the whole rollup: eligibility, the per-skill importance
histogram, the per-skill status fold, taxonomy coverage, and the corpus version. Not a
Python loop over postings - at a few thousand postings and tens of thousands of
requirements that loop would be the slowest thing in the system, and the ranking would
be untestable as a unit.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import time
from dataclasses import dataclass

from sqlalchemy import String, bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Session

from jme.config import get_settings
from jme.logging import get_logger

log = get_logger(__name__)

STATUS_EVIDENCED = "evidenced"
STATUS_WEAK = "weak"
STATUS_ABSENT = "absent"

#: Statuses that put a skill on the actionable gap list.
GAP_STATUSES = (STATUS_ABSENT, STATUS_WEAK)

#: `sponsorship` is free text from the feed. These are the phrases that mean "not me".
#: Deliberately conservative: a posting whose sponsorship field is NULL is kept, because
#: dropping unknowns silently shrinks every count in the report.
SPONSORSHIP_BLOCKLIST = [
    "%does not offer%",
    "%no sponsorship%",
    "%not provide sponsorship%",
    "%citizen%",
    "%clearance%",
    "%offshore%",
]


# --------------------------------------------------------------------------------------
# result types
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SkillGap:
    """One actionable canonical skill, with demand counts and my evidence status."""

    canonical_skill_id: int
    skill: str
    category: str
    required_count: int
    preferred_count: int
    mentioned_count: int
    posting_count: int
    status: str
    evidenced_citations: int
    weak_citations: int
    absent_citations: int
    matches_considered: int

    @property
    def is_gap(self) -> bool:
        return self.status in GAP_STATUSES

    def as_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["is_gap"] = self.is_gap
        return d


@dataclass(frozen=True)
class GapReport:
    generated_at: dt.datetime
    evidence_version: int
    eligible_posting_count: int
    total_requirement_count: int
    mapped_requirement_count: int
    unmapped_requirement_count: int
    actionable_skill_count: int
    gaps: list[SkillGap]
    covered: list[SkillGap]
    query_seconds: float

    @property
    def taxonomy_coverage(self) -> float:
        """Fraction of requirements on eligible postings resolved to a canonical skill.

        The honest measure of how much of the report is missing: every unmapped
        requirement is demand the ranking below simply cannot see.
        """
        if self.total_requirement_count == 0:
            return 0.0
        return self.mapped_requirement_count / self.total_requirement_count

    def top_gaps(self, n: int | None = None) -> list[SkillGap]:
        return self.gaps if n is None else self.gaps[:n]


@dataclass(frozen=True)
class AdapterCoverage:
    adapter: str
    postings: int
    resolved: int

    @property
    def coverage(self) -> float:
        return self.resolved / self.postings if self.postings else 0.0


@dataclass(frozen=True)
class CoverageReport:
    generated_at: dt.datetime
    active_posting_count: int
    with_jd_row: int
    resolved_count: int
    by_adapter: list[AdapterCoverage]
    by_fetch_status: dict[str, int]
    total_requirement_count: int
    mapped_requirement_count: int

    @property
    def adapter_coverage(self) -> float:
        """Percent of active postings successfully resolved to JD text.

        ARCHITECTURE section 8 calls this a first-class metric: it is the honest measure
        of whether the fetch half of the system works. Denominator is *all* active
        postings, including ones that never got a `posting_jd` row at all - anything
        else flatters the number.
        """
        if self.active_posting_count == 0:
            return 0.0
        return self.resolved_count / self.active_posting_count

    @property
    def taxonomy_coverage(self) -> float:
        if self.total_requirement_count == 0:
            return 0.0
        return self.mapped_requirement_count / self.total_requirement_count


# --------------------------------------------------------------------------------------
# the query
# --------------------------------------------------------------------------------------

# One statement. CTE by CTE:
#   eligible  - active postings passing the config eligibility filters (section 6)
#   req       - their requirement rows, including the ones with no canonical skill
#   agg       - per-skill importance histogram + distinct posting count
#   status    - per-skill fold over non-stale match citations (the rule documented above)
#   totals    - the scalars: eligible postings, taxonomy coverage, evidence version
# The final SELECT is `totals LEFT JOIN ranked ON true` so the scalars survive even when
# no skill qualifies; the caller drops the null-skill row.
_GAP_SQL = """
WITH eligible AS (
    SELECT p.id
    FROM posting p
    WHERE p.inactive_at IS NULL
      AND (
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
req AS (
    SELECT r.canonical_skill_id, r.importance, r.posting_id
    FROM posting_requirement r
    JOIN eligible e ON e.id = r.posting_id
),
agg AS (
    SELECT r.canonical_skill_id AS skill_id,
           count(*) FILTER (WHERE r.importance = 'required')  AS required_count,
           count(*) FILTER (WHERE r.importance = 'preferred') AS preferred_count,
           count(*) FILTER (WHERE r.importance = 'mentioned') AS mentioned_count,
           count(DISTINCT r.posting_id)                       AS posting_count
    FROM req r
    WHERE r.canonical_skill_id IS NOT NULL
    GROUP BY r.canonical_skill_id
),
status AS (
    SELECT c.canonical_skill_id AS skill_id,
           max(CASE c.status
                   WHEN 'evidenced' THEN 3
                   WHEN 'weak'      THEN 2
                   ELSE 1
               END)                                            AS best_rank,
           count(*) FILTER (WHERE c.status = 'evidenced')       AS evidenced_citations,
           count(*) FILTER (WHERE c.status = 'weak')            AS weak_citations,
           count(*) FILTER (WHERE c.status = 'absent')          AS absent_citations,
           count(DISTINCT c.match_id)                           AS matches_considered
    FROM match_citation c
    JOIN match m ON m.id = c.match_id
    WHERE m.is_stale = false
      AND c.canonical_skill_id IS NOT NULL
    GROUP BY c.canonical_skill_id
),
totals AS (
    SELECT (SELECT count(*) FROM eligible)                                    AS eligible_posting_count,
           (SELECT count(*) FROM req)                                         AS total_requirement_count,
           (SELECT count(*) FROM req WHERE canonical_skill_id IS NOT NULL)    AS mapped_requirement_count,
           (SELECT count(*) FROM req WHERE canonical_skill_id IS NULL)        AS unmapped_requirement_count,
           coalesce((SELECT v.version FROM evidence_version v WHERE v.id = 1), 0) AS evidence_version
),
ranked AS (
    SELECT s.id                                     AS canonical_skill_id,
           s.name                                   AS skill,
           s.category::text                         AS category,
           a.required_count,
           a.preferred_count,
           a.mentioned_count,
           a.posting_count,
           CASE coalesce(st.best_rank, 1)
               WHEN 3 THEN 'evidenced'
               WHEN 2 THEN 'weak'
               ELSE 'absent'
           END                                      AS status,
           coalesce(st.evidenced_citations, 0)      AS evidenced_citations,
           coalesce(st.weak_citations, 0)           AS weak_citations,
           coalesce(st.absent_citations, 0)         AS absent_citations,
           coalesce(st.matches_considered, 0)       AS matches_considered
    FROM agg a
    JOIN canonical_skill s ON s.id = a.skill_id
    LEFT JOIN status st ON st.skill_id = a.skill_id
    WHERE s.is_actionable
)
SELECT t.eligible_posting_count,
       t.total_requirement_count,
       t.mapped_requirement_count,
       t.unmapped_requirement_count,
       t.evidence_version,
       r.canonical_skill_id,
       r.skill,
       r.category,
       r.required_count,
       r.preferred_count,
       r.mentioned_count,
       r.posting_count,
       r.status,
       r.evidenced_citations,
       r.weak_citations,
       r.absent_citations,
       r.matches_considered
FROM totals t
LEFT JOIN ranked r ON true
ORDER BY r.required_count DESC NULLS LAST,
         r.preferred_count DESC NULLS LAST,
         r.mentioned_count DESC NULLS LAST,
         r.posting_count DESC NULLS LAST,
         r.skill ASC NULLS LAST
"""


def gap_query_sql() -> str:
    """The literal rollup SQL, so docs and `EXPLAIN ANALYZE` cannot drift from the code."""
    return _GAP_SQL


def _statement():
    return text(_GAP_SQL).bindparams(
        bindparam("role_types", type_=ARRAY(String)),
        bindparam("sponsorship_blocklist", type_=ARRAY(String)),
        bindparam("location_patterns", type_=ARRAY(String)),
    )


def eligibility_params(apply_eligibility: bool) -> dict:
    """Bind values for the shared eligibility predicate (ARCHITECTURE section 6).

    Shared with `trend.py` so the two reports can never disagree about which postings
    count.
    """
    settings = get_settings()
    return {
        "apply_eligibility": apply_eligibility,
        "role_types": [r.strip().lower() for r in settings.role_types],
        "allow_sponsorship_required": settings.allow_sponsorship_required,
        "sponsorship_blocklist": list(SPONSORSHIP_BLOCKLIST),
        "location_patterns": [f"%{loc}%" for loc in settings.location_allowlist],
    }


def build_gap_report(session: Session, *, apply_eligibility: bool = True) -> GapReport:
    """Run the rollup and split it into gaps (absent/weak) and covered (evidenced).

    Both lists come back in the same ranking: `required_count` first, so a skill that is
    only ever `mentioned` can never outrank a `required` skill no matter how many
    postings mention it.

    `apply_eligibility=False` drops the config filters and rolls up every active posting.
    Useful when the feed's sponsorship/location fields are too sparse to trust yet.
    """
    started = time.perf_counter()
    rows = session.execute(_statement(), eligibility_params(apply_eligibility)).mappings().all()
    elapsed = time.perf_counter() - started

    if not rows:  # pragma: no cover - `totals` is always one row
        return GapReport(
            generated_at=dt.datetime.now(dt.UTC),
            evidence_version=0,
            eligible_posting_count=0,
            total_requirement_count=0,
            mapped_requirement_count=0,
            unmapped_requirement_count=0,
            actionable_skill_count=0,
            gaps=[],
            covered=[],
            query_seconds=elapsed,
        )

    head = rows[0]
    gaps: list[SkillGap] = []
    covered: list[SkillGap] = []
    for row in rows:
        if row["canonical_skill_id"] is None:
            continue  # the totals-only row when no skill qualifies
        item = SkillGap(
            canonical_skill_id=row["canonical_skill_id"],
            skill=row["skill"],
            category=row["category"],
            required_count=row["required_count"],
            preferred_count=row["preferred_count"],
            mentioned_count=row["mentioned_count"],
            posting_count=row["posting_count"],
            status=row["status"],
            evidenced_citations=row["evidenced_citations"],
            weak_citations=row["weak_citations"],
            absent_citations=row["absent_citations"],
            matches_considered=row["matches_considered"],
        )
        (gaps if item.is_gap else covered).append(item)

    report = GapReport(
        generated_at=dt.datetime.now(dt.UTC),
        evidence_version=head["evidence_version"],
        eligible_posting_count=head["eligible_posting_count"],
        total_requirement_count=head["total_requirement_count"],
        mapped_requirement_count=head["mapped_requirement_count"],
        unmapped_requirement_count=head["unmapped_requirement_count"],
        actionable_skill_count=len(gaps) + len(covered),
        gaps=gaps,
        covered=covered,
        query_seconds=elapsed,
    )
    log.info(
        "gap_report_built",
        eligible_postings=report.eligible_posting_count,
        gaps=len(gaps),
        covered=len(covered),
        taxonomy_coverage=round(report.taxonomy_coverage, 4),
        query_seconds=round(elapsed, 4),
    )
    return report


def explain_gap_query(session: Session, *, apply_eligibility: bool = True, analyze: bool = True) -> str:
    """`EXPLAIN [ANALYZE] ` the rollup, verbatim, for docs/explain/gap_report.md."""
    options = "ANALYZE, BUFFERS, VERBOSE" if analyze else "VERBOSE"
    stmt = text(f"EXPLAIN ({options}) {_GAP_SQL}").bindparams(
        bindparam("role_types", type_=ARRAY(String)),
        bindparam("sponsorship_blocklist", type_=ARRAY(String)),
        bindparam("location_patterns", type_=ARRAY(String)),
    )
    rows = session.execute(stmt, eligibility_params(apply_eligibility)).all()
    return "\n".join(str(r[0]) for r in rows)


# --------------------------------------------------------------------------------------
# coverage
# --------------------------------------------------------------------------------------

_ADAPTER_COVERAGE_SQL = """
SELECT coalesce(j.adapter, '(none)')                       AS adapter,
       count(*)                                            AS postings,
       count(*) FILTER (
           WHERE j.fetch_status = 'ok'
             AND j.raw_text IS NOT NULL
             AND j.char_count > 0
       )                                                   AS resolved
FROM posting p
LEFT JOIN posting_jd j ON j.posting_id = p.id
WHERE p.inactive_at IS NULL
GROUP BY 1
ORDER BY postings DESC, adapter ASC
"""

_FETCH_STATUS_SQL = """
SELECT coalesce(j.fetch_status::text, '(no row)') AS fetch_status, count(*) AS n
FROM posting p
LEFT JOIN posting_jd j ON j.posting_id = p.id
WHERE p.inactive_at IS NULL
GROUP BY 1
ORDER BY n DESC
"""

_TAXONOMY_COVERAGE_SQL = """
SELECT count(*)                                                   AS total,
       count(*) FILTER (WHERE r.canonical_skill_id IS NOT NULL)   AS mapped
FROM posting_requirement r
JOIN posting p ON p.id = r.posting_id
WHERE p.inactive_at IS NULL
"""


def adapter_coverage(session: Session) -> list[AdapterCoverage]:
    rows = session.execute(text(_ADAPTER_COVERAGE_SQL)).mappings().all()
    return [
        AdapterCoverage(adapter=r["adapter"], postings=r["postings"], resolved=r["resolved"])
        for r in rows
    ]


def coverage_report(session: Session) -> CoverageReport:
    """Adapter coverage (section 8) plus taxonomy coverage (section 3) in one object."""
    by_adapter = adapter_coverage(session)
    statuses = {
        r["fetch_status"]: r["n"]
        for r in session.execute(text(_FETCH_STATUS_SQL)).mappings().all()
    }
    tax = session.execute(text(_TAXONOMY_COVERAGE_SQL)).mappings().one()
    active = sum(a.postings for a in by_adapter)
    return CoverageReport(
        generated_at=dt.datetime.now(dt.UTC),
        active_posting_count=active,
        with_jd_row=sum(a.postings for a in by_adapter if a.adapter != "(none)"),
        resolved_count=sum(a.resolved for a in by_adapter),
        by_adapter=by_adapter,
        by_fetch_status=statuses,
        total_requirement_count=tax["total"],
        mapped_requirement_count=tax["mapped"],
    )
