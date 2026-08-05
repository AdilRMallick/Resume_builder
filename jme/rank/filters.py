"""Stage 1: hard eligibility filters, evaluated in SQL.

Design rules, in priority order:

1. **Cheap.** This runs over every active posting on every run. It is one indexed
   scan plus row-local predicates; nothing here joins to requirements, embeddings or
   job description text.
2. **Degrade rather than drop** (ARCHITECTURE.md section 6). Missing data is not
   evidence of ineligibility. A posting with no `start_season`, no `sponsorship`
   string, or an unrecognised `role_type` survives the filter and is *flagged*. Only
   a value we positively understand and that positively fails is allowed to drop a
   posting.
3. **Observable.** Every dropped posting is attributed to exactly one rule, in a
   fixed evaluation order, so the drop counts sum to the number of postings removed
   and the funnel is honest. Both queries below are generated from the same CTE text
   so the survivor set and the funnel counts cannot drift apart.

Index notes (see docs/explain/stage1_filters.md for the recorded plan):

* `inactive_at IS NULL` is the only predicate placed in the driving `WHERE`, so the
  planner can use `ix_posting_active_feed (inactive_at, posted_at)` -- Postgres btrees
  index NULLs, so `IS NULL` is a usable index qualifier, not just a filter.
* The location predicate leads with `locations @> :remote_json::jsonb`, the
  containment operator, because it is the cheapest arm: a single jsonb containment
  test against a literal. The ILIKE arm that follows it is a substring test
  ("Detroit, MI" must match the allowlist entry "Detroit"), so it is deliberately
  second and only runs on rows the cheap arms did not already accept. Note that `@>`
  is *also* the operator a `gin (locations jsonb_path_ops)` index could answer, but no
  such index exists today and this query could not use one anyway -- the test sits
  inside a CASE over a CTE, evaluated per row after the driving scan.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import TextClause

from jme.config import Settings, get_settings
from jme.logging import get_logger

logger = get_logger(__name__)


# --------------------------------------------------------------------------------------
# tunables
# --------------------------------------------------------------------------------------

#: How long before `grad_date` a start date may fall and still count as eligible.
#: A May-2027 graduate can realistically start a role advertised as beginning a few
#: weeks earlier (finals end before commencement, start dates slip), but not a full
#: season earlier. 45 days keeps "Summer 2027" comfortably in and "Spring 2027" out.
START_SLACK_DAYS = 45

#: Feed role types we recognise. A value in this set that is not in
#: `settings.role_types` is a real mismatch and is dropped. A value outside this set
#: is something the feed started emitting that we have not taught the filter about --
#: that is our bug, not the posting's fault, so it survives with a flag.
KNOWN_ROLE_TYPES: tuple[str, ...] = (
    "swe",
    "software",
    "software engineering",
    "engineering",
    "quant",
    "quantitative finance",
    "pm",
    "product",
    "product management",
    "hardware",
    "data",
    "data science",
    "ml",
    "ai",
    "research",
    "design",
    "ux",
    "security",
    "it",
    "business",
    "finance",
    "consulting",
    "other",
)

#: Ordered drop reasons. Evaluation order is fixed so attribution is stable, and the
#: pipeline emits a metric for every one of these even when the count is zero -- a
#: missing bucket in a funnel is indistinguishable from a zero and that ambiguity is
#: how funnels start lying.
DROP_REASONS: tuple[str, ...] = (
    "inactive",
    "sponsorship_offshore",
    "sponsorship_citizenship",
    "sponsorship_not_offered",
    "location",
    "role_type",
    "start_season",
)

#: Flags recorded on surviving postings: the "we did not drop you, but we are not sure
#: about you" set. Task 9 can use these to discount its own confidence.
FLAG_NAMES: tuple[str, ...] = (
    "sponsorship_unknown",
    "role_type_unknown",
    "start_season_unknown",
    "start_season_partial",
)


# --------------------------------------------------------------------------------------
# results
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class FilteredPosting:
    """A posting that survived stage 1, plus the classifications that let it through."""

    posting_id: int
    company: str
    title: str
    url: str
    locations: list[str]
    is_remote: bool
    sponsorship: str | None
    sponsorship_class: str
    role_type: str | None
    role_state: str
    start_season: str | None
    start_state: str
    posted_at: dt.datetime | None

    @property
    def flags(self) -> tuple[str, ...]:
        out: list[str] = []
        if self.sponsorship_class == "unknown":
            out.append("sponsorship_unknown")
        if self.role_state == "unknown":
            out.append("role_type_unknown")
        if self.start_state == "unknown":
            out.append("start_season_unknown")
        elif self.start_state == "partial":
            out.append("start_season_partial")
        return tuple(out)


@dataclass(frozen=True)
class FilterOutcome:
    total_postings: int
    active_postings: int
    kept: list[FilteredPosting]
    drop_counts: dict[str, int] = field(default_factory=dict)
    flag_counts: dict[str, int] = field(default_factory=dict)

    @property
    def posting_ids(self) -> list[int]:
        return [p.posting_id for p in self.kept]


# --------------------------------------------------------------------------------------
# SQL
# --------------------------------------------------------------------------------------

# Normalised sponsorship string: lowercase, punctuation collapsed to single spaces.
# "U.S. Citizenship is Required" -> "u s citizenship is required", which is why the
# citizenship pattern below matches on 'citizen' rather than an exact literal.
_SPONSORSHIP_NORM = "btrim(regexp_replace(lower(coalesce(a.sponsorship, '')), '[^a-z0-9]+', ' ', 'g'))"

# The classification is a five-branch CASE over the *normalised* string. Postgres does
# not common-subexpression-eliminate, so writing the regexp_replace inline would run it
# once per branch; it is computed once in the `active` CTE instead (measured: 135ms ->
# 82ms over 4k active rows).
_SPONSORSHIP_CLASS = """
        CASE
            WHEN a.sponsorship_norm = '' THEN 'unknown'
            WHEN a.sponsorship_norm ~ '(offshore|outside the us|non us only)' THEN 'offshore'
            WHEN a.sponsorship_norm ~ '(citizen|clearance|green card|permanent resid)'
                THEN 'citizenship'
            WHEN a.sponsorship_norm ~ '(does not offer|no sponsorship|not offer sponsorship|sponsorship not (offered|available)|without sponsorship)'
                THEN 'not_offered'
            WHEN a.sponsorship_norm ~ 'offers sponsorship' THEN 'offers'
            ELSE 'unknown'
        END"""

# Leading arm is jsonb containment (GIN-answerable); trailing arms are substring tests
# that no index can serve. See the module docstring.
_LOCATION_OK = """
        (
            a.is_remote
            OR a.locations @> :remote_json ::jsonb
            OR EXISTS (
                SELECT 1
                FROM jsonb_array_elements_text(
                    CASE WHEN jsonb_typeof(a.locations) = 'array' THEN a.locations
                         ELSE '[]'::jsonb END
                ) AS loc(v)
                WHERE loc.v ILIKE ANY(:location_patterns)
            )
            OR (
                jsonb_typeof(a.locations) = 'string'
                AND (a.locations #>> '{}') ILIKE ANY(:location_patterns)
            )
        )"""

_ROLE_STATE = """
        CASE
            WHEN a.role_type IS NULL OR btrim(a.role_type) = '' THEN 'unknown'
            WHEN lower(btrim(a.role_type)) = ANY(:role_types) THEN 'allowed'
            WHEN lower(btrim(a.role_type)) = ANY(:known_role_types) THEN 'excluded'
            ELSE 'unknown'
        END"""

# Season -> nominal start month. Winter is the ambiguous one (co-op "Winter 2027"
# usually means January 2027, not December); January is the conservative reading.
# A year with no season word is scored at December, i.e. the most permissive month of
# that year, and flagged 'partial' -- we only know enough to rule out earlier years.
_START_MONTH = """
            CASE
                WHEN a.start_season ~* 'spring' THEN 3
                WHEN a.start_season ~* 'summer' THEN 6
                WHEN a.start_season ~* '(fall|autumn)' THEN 9
                WHEN a.start_season ~* 'winter' THEN 1
                ELSE 12
            END"""

_START_STATE = f"""
        CASE
            WHEN a.start_season IS NULL OR btrim(a.start_season) = '' THEN 'unknown'
            WHEN substring(a.start_season from '(20[0-9]{{2}})') IS NULL THEN 'unknown'
            WHEN make_date(
                    substring(a.start_season from '(20[0-9]{{2}})')::int,
                    {_START_MONTH},
                    1
                 ) >= :earliest_start
                THEN CASE
                        WHEN a.start_season ~* '(spring|summer|fall|autumn|winter)' THEN 'eligible'
                        ELSE 'partial'
                     END
            ELSE 'too_early'
        END"""

_DROP_REASON = """
        CASE
            WHEN NOT :allow_sponsorship_required AND c.sponsorship_class = 'offshore'
                THEN 'sponsorship_offshore'
            WHEN NOT :allow_sponsorship_required AND c.sponsorship_class = 'citizenship'
                THEN 'sponsorship_citizenship'
            WHEN NOT :allow_sponsorship_required AND c.sponsorship_class = 'not_offered'
                THEN 'sponsorship_not_offered'
            WHEN NOT c.location_ok THEN 'location'
            WHEN c.role_state = 'excluded' THEN 'role_type'
            WHEN c.start_state = 'too_early' THEN 'start_season'
            ELSE NULL
        END"""

def _ctes(materialized: bool) -> str:
    """The shared classification pipeline.

    `MATERIALIZED` on `active` is deliberate for the bulk queries: it pins the
    expensive-once normalisation to a single evaluation per row and keeps the plan
    readable (one index scan node, one filter node). The single-posting variant turns
    it off so `WHERE id = :posting_id` can be pushed down to a primary key lookup
    instead of classifying every active posting to answer a question about one.
    """
    keyword = " MATERIALIZED" if materialized else ""
    return f"""
WITH active AS{keyword} (
    -- the only indexable predicate: ix_posting_active_feed (inactive_at, posted_at)
    SELECT a.id, a.company, a.title, a.url, a.locations, a.sponsorship,
           a.role_type, a.start_season, a.is_remote, a.posted_at,
           {_SPONSORSHIP_NORM} AS sponsorship_norm
    FROM posting a
    WHERE a.inactive_at IS NULL
),
classified AS (
    SELECT a.*,
           {_SPONSORSHIP_CLASS} AS sponsorship_class,
           {_LOCATION_OK} AS location_ok,
           {_ROLE_STATE} AS role_state,
           {_START_STATE} AS start_state
    FROM active a
),
judged AS (
    SELECT c.*, {_DROP_REASON} AS drop_reason
    FROM classified c
)"""


_SELECT_COLUMNS = """
SELECT id, company, title, url, locations, sponsorship, role_type, start_season,
       is_remote, posted_at, sponsorship_class, role_state, start_state"""

_SURVIVORS_SQL = f"""{_ctes(True)}{_SELECT_COLUMNS}
FROM judged
WHERE drop_reason IS NULL
ORDER BY id
"""

_FUNNEL_SQL = f"""{_ctes(True)}
SELECT coalesce(drop_reason, '__kept__') AS reason, count(*) AS n
FROM judged
GROUP BY 1
"""

# Same classification for a single posting, including the reason it *would* be
# dropped. `jme rank show` uses this so you can interrogate a posting that never made
# the shortlist, which is the case you actually want to debug.
_ONE_SQL = f"""{_ctes(False)}{_SELECT_COLUMNS}, drop_reason
FROM judged
WHERE id = :posting_id
"""


def bind_arrays(sql: str) -> TextClause:
    """`text()` with the array parameters typed, so psycopg sends real Postgres arrays."""
    return text(sql).bindparams(
        bindparam("location_patterns"),
        bindparam("role_types"),
        bindparam("known_role_types"),
    )


def filter_params(settings: Settings | None = None) -> dict[str, Any]:
    """Bind parameters for the stage-1 query, derived entirely from config."""
    settings = settings or get_settings()

    patterns = {f"%{term.strip().lower()}%" for term in settings.location_allowlist if term.strip()}
    # Remote is always acceptable regardless of the allowlist: a remote role has no
    # location constraint to violate.
    patterns.add("%remote%")

    return {
        "location_patterns": sorted(patterns),
        "remote_json": '["Remote"]',
        "role_types": sorted({r.strip().lower() for r in settings.role_types if r.strip()}),
        "known_role_types": list(KNOWN_ROLE_TYPES),
        "earliest_start": settings.grad_date - dt.timedelta(days=START_SLACK_DAYS),
        "allow_sponsorship_required": settings.allow_sponsorship_required,
    }


def stage1_sql() -> str:
    """The survivor query. Exposed so `jme rank explain` can EXPLAIN exactly this text."""
    return _SURVIVORS_SQL


def funnel_sql() -> str:
    """The per-rule drop-count query. Same CTEs, different projection."""
    return _FUNNEL_SQL


# --------------------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------------------


def _to_locations(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(item) for item in raw]
    if isinstance(raw, str):
        return [raw]
    return []


def _row_to_posting(row: Any) -> FilteredPosting:
    return FilteredPosting(
        posting_id=int(row["id"]),
        company=row["company"],
        title=row["title"],
        url=row["url"],
        locations=_to_locations(row["locations"]),
        is_remote=bool(row["is_remote"]),
        sponsorship=row["sponsorship"],
        sponsorship_class=row["sponsorship_class"],
        role_type=row["role_type"],
        role_state=row["role_state"],
        start_season=row["start_season"],
        start_state=row["start_state"],
        posted_at=row["posted_at"],
    )


def classify_posting(
    session: Session, posting_id: int, settings: Settings | None = None
) -> tuple[FilteredPosting | None, str | None]:
    """(posting, drop_reason) for one posting. `drop_reason` is None when it survives.

    Returns `(None, 'inactive')` for a deactivated posting and `(None, None)` when the
    id does not exist at all.
    """
    settings = settings or get_settings()
    params = dict(filter_params(settings), posting_id=posting_id)
    row = session.execute(bind_arrays(_ONE_SQL), params).mappings().first()
    if row is not None:
        return _row_to_posting(row), row["drop_reason"]
    exists = session.execute(
        text("SELECT 1 FROM posting WHERE id = :posting_id"), {"posting_id": posting_id}
    ).scalar()
    return None, ("inactive" if exists else None)


def apply_filters(session: Session, settings: Settings | None = None) -> FilterOutcome:
    """Run stage 1. Returns survivors, per-rule drop counts, and flag counts."""
    settings = settings or get_settings()
    params = filter_params(settings)

    total = int(session.execute(text("SELECT count(*) FROM posting")).scalar_one())

    counts = {
        str(row["reason"]): int(row["n"])
        for row in session.execute(bind_arrays(_FUNNEL_SQL), params).mappings().all()
    }
    active = sum(counts.values())

    drop_counts = {reason: 0 for reason in DROP_REASONS}
    drop_counts["inactive"] = total - active
    for reason, n in counts.items():
        if reason == "__kept__":
            continue
        drop_counts[reason] = drop_counts.get(reason, 0) + n

    rows = session.execute(bind_arrays(_SURVIVORS_SQL), params).mappings().all()
    kept = [_row_to_posting(row) for row in rows]

    flag_counts = {name: 0 for name in FLAG_NAMES}
    for posting in kept:
        for flag in posting.flags:
            flag_counts[flag] += 1

    logger.info(
        "stage1.filters",
        total=total,
        active=active,
        kept=len(kept),
        drops=drop_counts,
        flags=flag_counts,
    )
    return FilterOutcome(
        total_postings=total,
        active_postings=active,
        kept=kept,
        drop_counts=drop_counts,
        flag_counts=flag_counts,
    )
