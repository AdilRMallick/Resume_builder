"""Read-only HTTP API over Postgres.

Read-only is a design decision, not a limitation. Every write in this system happens in
a worker with a transaction and idempotency rules around it; exposing writes over HTTP
would mean re-implementing those rules in a second place. So this app opens a session,
selects, and closes.

No auth, no tenancy, no rate limiting: ARCHITECTURE section 10 puts multi-user and
hosting explicitly out of scope. This binds to localhost and serves one person. If that
ever changes, this file is the wrong place to bolt auth onto - put a reverse proxy in
front of it.

Session handling: one session per request via the `get_session` dependency. Tests
override that dependency with the transactional `db_session` fixture, which is why it is
a plain function and not a module-level global.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from jme.api.schemas import (
    CitationOut,
    GapReportOut,
    Health,
    JDOut,
    MatchOut,
    MetricOut,
    Page,
    PostingDetail,
    PostingSummary,
    QueueStatus,
    RequirementOut,
    ShortlistItem,
    ShortlistOut,
    SkillOut,
    StreamStatus,
)
from jme.config import (
    GROUP_ENRICH,
    GROUP_FETCH,
    STREAM_ENRICH,
    STREAM_ENRICH_DEAD,
    STREAM_FETCH,
    STREAM_FETCH_DEAD,
    get_settings,
)
from jme.db import session_scope
from jme.logging import get_logger
from jme.report.gap import build_gap_report
from jme.report.render import gap_report_to_dict

log = get_logger(__name__)


def get_session() -> Iterator[Session]:
    """One short-lived session per request. Overridden in tests."""
    with session_scope() as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]

#: The dashboard. A single file with no build step, served as-is.
DASHBOARD = Path(__file__).with_name("static") / "index.html"


def create_app() -> FastAPI:
    app = FastAPI(
        title="Job Match Engine",
        version="0.1.0",
        description="Read-only view over the postings, requirements, matches, and gap report.",
    )

    # ----------------------------------------------------------------------------------
    # dashboard
    # ----------------------------------------------------------------------------------

    @app.get("/", include_in_schema=False, response_class=HTMLResponse)
    def dashboard() -> HTMLResponse:
        """The read-only dashboard.

        Served as a route rather than a StaticFiles mount, because mounting at `/` would
        shadow every API path below it. The page holds no data of its own: it fetches the
        same JSON endpoints as everything else, so it cannot show a number the API does
        not agree with.
        """
        try:
            return HTMLResponse(DASHBOARD.read_text(encoding="utf-8"))
        except OSError as exc:
            # An install that lost its package data should say so, not 500 blankly.
            log.error("dashboard_missing", path=str(DASHBOARD), error=str(exc))
            raise HTTPException(
                status_code=500, detail=f"dashboard asset missing at {DASHBOARD}"
            ) from exc

    # ----------------------------------------------------------------------------------
    # health
    # ----------------------------------------------------------------------------------

    @app.get("/health", response_model=Health)
    def health(session: SessionDep) -> Health:
        try:
            version = session.execute(
                text("SELECT version FROM evidence_version WHERE id = 1")
            ).scalar()
            return Health(status="ok", database="ok", evidence_version=int(version or 0))
        except Exception as exc:  # noqa: BLE001 - health must report, not raise
            log.error("health_db_failed", error=str(exc))
            return Health(
                status="degraded", database="error", error=f"{type(exc).__name__}: {exc}"
            )

    # ----------------------------------------------------------------------------------
    # postings
    # ----------------------------------------------------------------------------------

    _POSTING_COLUMNS = """
        p.id, p.company, p.title, p.url, p.role_type, p.locations, p.is_remote,
        p.sponsorship, p.posted_at, p.first_seen_at, p.last_seen_at, p.inactive_at,
        p.repost_count,
        (j.posting_id IS NOT NULL AND j.char_count > 0) AS has_jd,
        j.adapter AS jd_adapter,
        j.fetch_status::text AS jd_fetch_status
    """

    # Every optional filter is "param IS NULL OR <predicate>", and every param is CAST
    # explicitly: psycopg sends an untyped NULL for a None parameter, and Postgres
    # rejects `$1 IS NULL` when it cannot infer the type.
    _POSTING_FILTERS = """
        WHERE (CAST(:active AS boolean) IS NULL
               OR (CAST(:active AS boolean) AND p.inactive_at IS NULL)
               OR (NOT CAST(:active AS boolean) AND p.inactive_at IS NOT NULL))
          AND (CAST(:company AS text) IS NULL OR p.company ILIKE CAST(:company_like AS text))
          AND (CAST(:role_type AS text) IS NULL
               OR lower(p.role_type) = lower(CAST(:role_type AS text)))
          AND (CAST(:has_jd AS boolean) IS NULL
               OR (CAST(:has_jd AS boolean)
                   AND j.posting_id IS NOT NULL AND j.char_count > 0)
               OR (NOT CAST(:has_jd AS boolean)
                   AND (j.posting_id IS NULL OR j.char_count = 0)))
          AND (CAST(:q AS text) IS NULL
               OR p.title ILIKE CAST(:q_like AS text)
               OR p.company ILIKE CAST(:q_like AS text))
    """

    @app.get("/postings", response_model=Page[PostingSummary])
    def list_postings(
        session: SessionDep,
        active: Annotated[bool | None, Query(description="true=live, false=deactivated")] = None,
        company: Annotated[str | None, Query(description="substring, case-insensitive")] = None,
        role_type: str | None = None,
        has_jd: Annotated[bool | None, Query(description="resolved to JD text")] = None,
        q: Annotated[str | None, Query(description="substring over title and company")] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> Page[PostingSummary]:
        params: dict[str, Any] = {
            "active": active,
            "company": company,
            "company_like": f"%{company}%" if company else None,
            "role_type": role_type,
            "has_jd": has_jd,
            "q": q,
            "q_like": f"%{q}%" if q else None,
            "limit": limit,
            "offset": offset,
        }
        total = session.execute(
            text(
                "SELECT count(*) FROM posting p "
                "LEFT JOIN posting_jd j ON j.posting_id = p.id " + _POSTING_FILTERS
            ),
            params,
        ).scalar_one()
        rows = (
            session.execute(
                text(
                    f"SELECT {_POSTING_COLUMNS} FROM posting p "
                    "LEFT JOIN posting_jd j ON j.posting_id = p.id "
                    + _POSTING_FILTERS
                    + " ORDER BY p.posted_at DESC NULLS LAST, p.id DESC"
                    " LIMIT :limit OFFSET :offset"
                ),
                params,
            )
            .mappings()
            .all()
        )
        return Page[PostingSummary](
            items=[PostingSummary(**dict(r)) for r in rows],
            total=int(total),
            limit=limit,
            offset=offset,
        )

    @app.get("/postings/{posting_id}", response_model=PostingDetail)
    def get_posting(session: SessionDep, posting_id: int) -> PostingDetail:
        row = (
            session.execute(
                text(
                    f"SELECT {_POSTING_COLUMNS} FROM posting p "
                    "LEFT JOIN posting_jd j ON j.posting_id = p.id "
                    "WHERE p.id = :id"
                ),
                {"id": posting_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise HTTPException(status_code=404, detail=f"posting {posting_id} not found")

        jd_row = (
            session.execute(
                text(
                    "SELECT adapter, fetch_status::text AS fetch_status, char_count, attempts,"
                    " extracted_at, fetch_error, (raw_text IS NOT NULL) AS has_text"
                    " FROM posting_jd WHERE posting_id = :id"
                ),
                {"id": posting_id},
            )
            .mappings()
            .first()
        )
        req_rows = (
            session.execute(
                text(
                    "SELECT r.id, r.canonical_skill_id, s.name AS skill, r.raw_text,"
                    " r.importance::text AS importance, r.confidence, r.prompt_version"
                    " FROM posting_requirement r"
                    " LEFT JOIN canonical_skill s ON s.id = r.canonical_skill_id"
                    " WHERE r.posting_id = :id"
                    " ORDER BY CASE r.importance WHEN 'required' THEN 0"
                    "          WHEN 'preferred' THEN 1 ELSE 2 END, r.id"
                ),
                {"id": posting_id},
            )
            .mappings()
            .all()
        )
        match_row = (
            session.execute(
                text(
                    "SELECT id, evidence_version, prompt_version, model_id, score,"
                    " verdict::text AS verdict, rationale, is_stale, computed_at"
                    " FROM match WHERE posting_id = :id"
                    " ORDER BY computed_at DESC, id DESC LIMIT 1"
                ),
                {"id": posting_id},
            )
            .mappings()
            .first()
        )
        latest_match = None
        if match_row is not None:
            citations = (
                session.execute(
                    text(
                        "SELECT c.canonical_skill_id, s.name AS skill, c.evidence_chunk_id,"
                        " c.status::text AS status, c.reasoning"
                        " FROM match_citation c"
                        " LEFT JOIN canonical_skill s ON s.id = c.canonical_skill_id"
                        " WHERE c.match_id = :mid ORDER BY c.id"
                    ),
                    {"mid": match_row["id"]},
                )
                .mappings()
                .all()
            )
            latest_match = MatchOut(
                **dict(match_row),
                citations=[CitationOut(**dict(c)) for c in citations],
            )

        return PostingDetail(
            **dict(row),
            jd=JDOut(**dict(jd_row)) if jd_row is not None else None,
            requirements=[RequirementOut(**dict(r)) for r in req_rows],
            latest_match=latest_match,
        )

    # ----------------------------------------------------------------------------------
    # shortlist
    # ----------------------------------------------------------------------------------

    @app.get("/shortlist", response_model=ShortlistOut)
    def shortlist(
        session: SessionDep,
        run_id: Annotated[
            str | None, Query(description="omit for the most recent shortlist run")
        ] = None,
    ) -> ShortlistOut:
        if run_id is None:
            run_id = session.execute(
                text(
                    "SELECT run_id FROM shortlist_entry"
                    " GROUP BY run_id ORDER BY max(created_at) DESC LIMIT 1"
                )
            ).scalar()
        if run_id is None:
            return ShortlistOut()

        rows = (
            session.execute(
                text(
                    f"SELECT e.rank, e.coarse_score, e.evidence_version, e.created_at,"
                    f" {_POSTING_COLUMNS}"
                    " FROM shortlist_entry e"
                    " JOIN posting p ON p.id = e.posting_id"
                    " LEFT JOIN posting_jd j ON j.posting_id = p.id"
                    " WHERE e.run_id = :run_id ORDER BY e.rank"
                ),
                {"run_id": run_id},
            )
            .mappings()
            .all()
        )
        items = []
        created_at: dt.datetime | None = None
        for r in rows:
            d = dict(r)
            rank = d.pop("rank")
            score = d.pop("coarse_score")
            version = d.pop("evidence_version")
            created_at = d.pop("created_at")
            items.append(
                ShortlistItem(
                    rank=rank,
                    coarse_score=float(score),
                    evidence_version=version,
                    posting=PostingSummary(**d),
                )
            )
        return ShortlistOut(
            run_id=run_id, created_at=created_at, count=len(items), items=items
        )

    # ----------------------------------------------------------------------------------
    # gaps
    # ----------------------------------------------------------------------------------

    @app.get("/gaps", response_model=GapReportOut)
    def gaps(
        session: SessionDep,
        top: Annotated[int, Query(ge=1, le=500)] = 20,
        include_covered: bool = False,
        all_active: Annotated[
            bool, Query(description="ignore the config eligibility filters")
        ] = False,
    ) -> GapReportOut:
        # Reuses jme.report.gap verbatim: the API and the CLI must never be able to
        # disagree about what a gap is.
        report = build_gap_report(session, apply_eligibility=not all_active)
        return GapReportOut(
            **gap_report_to_dict(report, top=top, include_covered=include_covered)
        )

    # ----------------------------------------------------------------------------------
    # skills
    # ----------------------------------------------------------------------------------

    @app.get("/skills", response_model=Page[SkillOut])
    def skills(
        session: SessionDep,
        actionable: bool | None = None,
        q: Annotated[str | None, Query(description="substring over the skill name")] = None,
        category: str | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> Page[SkillOut]:
        params: dict[str, Any] = {
            "actionable": actionable,
            "q": q,
            "q_like": f"%{q}%" if q else None,
            "category": category,
            "limit": limit,
            "offset": offset,
        }
        where = """
            WHERE (CAST(:actionable AS boolean) IS NULL
                   OR s.is_actionable = CAST(:actionable AS boolean))
              AND (CAST(:q AS text) IS NULL OR s.name ILIKE CAST(:q_like AS text))
              AND (CAST(:category AS text) IS NULL
                   OR s.category::text = CAST(:category AS text))
        """
        total = session.execute(
            text("SELECT count(*) FROM canonical_skill s " + where), params
        ).scalar_one()
        rows = (
            session.execute(
                text(
                    "SELECT s.id, s.name, s.category::text AS category, s.is_actionable,"
                    " (SELECT count(*) FROM skill_alias a WHERE a.canonical_skill_id = s.id)"
                    "   AS alias_count,"
                    " coalesce(r.requirement_count, 0) AS requirement_count,"
                    " coalesce(r.posting_count, 0) AS posting_count,"
                    " coalesce(r.required_count, 0) AS required_count"
                    " FROM canonical_skill s"
                    " LEFT JOIN ("
                    "   SELECT pr.canonical_skill_id AS sid, count(*) AS requirement_count,"
                    "          count(DISTINCT pr.posting_id) AS posting_count,"
                    "          count(*) FILTER (WHERE pr.importance = 'required')"
                    "            AS required_count"
                    "   FROM posting_requirement pr"
                    "   JOIN posting p ON p.id = pr.posting_id AND p.inactive_at IS NULL"
                    "   GROUP BY pr.canonical_skill_id"
                    " ) r ON r.sid = s.id "
                    + where
                    + " ORDER BY required_count DESC, requirement_count DESC, s.name ASC"
                    " LIMIT :limit OFFSET :offset"
                ),
                params,
            )
            .mappings()
            .all()
        )
        return Page[SkillOut](
            items=[SkillOut(**dict(r)) for r in rows],
            total=int(total),
            limit=limit,
            offset=offset,
        )

    # ----------------------------------------------------------------------------------
    # metrics
    # ----------------------------------------------------------------------------------

    @app.get("/metrics", response_model=Page[MetricOut])
    def metrics(
        session: SessionDep,
        stage: str | None = None,
        run_id: str | None = None,
        metric: str | None = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> Page[MetricOut]:
        params = {
            "stage": stage,
            "run_id": run_id,
            "metric": metric,
            "limit": limit,
            "offset": offset,
        }
        where = """
            WHERE (CAST(:stage AS text) IS NULL OR stage = CAST(:stage AS text))
              AND (CAST(:run_id AS text) IS NULL OR run_id = CAST(:run_id AS text))
              AND (CAST(:metric AS text) IS NULL OR metric = CAST(:metric AS text))
        """
        total = session.execute(
            text("SELECT count(*) FROM run_metric " + where), params
        ).scalar_one()
        rows = (
            session.execute(
                text(
                    "SELECT id, run_id, stage, metric, value, labels, recorded_at"
                    " FROM run_metric "
                    + where
                    + " ORDER BY recorded_at DESC, id DESC LIMIT :limit OFFSET :offset"
                ),
                params,
            )
            .mappings()
            .all()
        )
        return Page[MetricOut](
            items=[MetricOut(**dict(r)) for r in rows],
            total=int(total),
            limit=limit,
            offset=offset,
        )

    # ----------------------------------------------------------------------------------
    # ops
    # ----------------------------------------------------------------------------------

    @app.get("/ops/queue", response_model=QueueStatus)
    def queue_status() -> QueueStatus:
        """Stream depth and pending-message age for the fetch and enrich streams.

        Redis being down is an operational fact about one dependency, not a reason for
        the whole API to 500. It comes back as `ok: false` with an `error` string, and
        every other endpoint keeps working.
        """
        from jme import queue as queue_mod

        settings = get_settings()
        pairs = [
            (STREAM_FETCH, GROUP_FETCH, STREAM_FETCH_DEAD),
            (STREAM_ENRICH, GROUP_ENRICH, STREAM_ENRICH_DEAD),
        ]
        try:
            client = queue_mod.connect(settings.redis_url)
            client.ping()
        except Exception as exc:  # noqa: BLE001 - degrade, never 500
            log.warning("ops_queue_redis_unreachable", error=str(exc))
            return QueueStatus(
                redis_url=settings.redis_url,
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
                streams=[
                    StreamStatus(stream=s, group=g, error="redis unreachable")
                    for s, g, _ in pairs
                ],
            )

        now_ms = int(dt.datetime.now(dt.UTC).timestamp() * 1000)
        streams: list[StreamStatus] = []
        for stream, group, dead in pairs:
            status = StreamStatus(stream=stream, group=group)
            try:
                status.depth = int(client.xlen(stream))
                status.dead_letter_depth = int(client.xlen(dead))
                summary = client.xpending(stream, group) or {}
                status.pending = int(summary.get("pending", 0) or 0)
                status.consumers = len(summary.get("consumers") or [])
                oldest = summary.get("min")
                if oldest:
                    # a stream id is "<ms>-<seq>"; the ms half is when it was XADDed, so
                    # now - that is how long the oldest unacked message has been stuck
                    ms = int(str(oldest).split("-")[0])
                    status.oldest_pending_age_sec = max(0.0, (now_ms - ms) / 1000.0)
            except Exception as exc:  # noqa: BLE001 - one bad stream must not sink the rest
                status.error = f"{type(exc).__name__}: {exc}"
            streams.append(status)

        return QueueStatus(redis_url=settings.redis_url, ok=True, streams=streams)

    return app


app = create_app()
