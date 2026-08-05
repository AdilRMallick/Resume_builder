"""The monotonic corpus version and the staleness sweep it triggers.

Why this exists: match results are cached on
`sha256(jd_text) + evidence_version + prompt_version + model_id`. If I add a project
and the version does not move, every cached match keeps reporting gaps I have already
closed, silently and forever. So any real change to the corpus bumps the version, and
the bump marks affected matches stale.

Two rules that matter more than they look:

  * **Only a real change bumps.** Re-running ingestion over unchanged files must be a
    no-op, otherwise a cron'd ingest invalidates the whole match cache daily and the
    LLM bill is unbounded.
  * **Stale, never deleted.** Marking `is_stale` keeps the old verdict and its
    citations readable until a recompute replaces them. Deleting would mean the UI
    shows nothing between the bump and the next scheduled run. Recompute is lazy and
    bounded: active postings only, on the next run.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from jme.logging import get_logger
from jme.models import EvidenceVersion, Match, Posting

log = get_logger(__name__)

SINGLETON_ID = 1


def ensure_version_row(session: Session) -> None:
    """Idempotently seed the singleton.

    The migration seeds it, but schemas created straight from ORM metadata (the test
    harness) do not, and a missing row should not be a crash in either place.
    """
    stmt = (
        pg_insert(EvidenceVersion)
        .values(id=SINGLETON_ID, version=1, bumped_at=_now(), reason="initial")
        .on_conflict_do_nothing(index_elements=[EvidenceVersion.id])
    )
    session.execute(stmt)
    session.flush()


def current_version(session: Session) -> int:
    row = session.execute(
        select(EvidenceVersion.version).where(EvidenceVersion.id == SINGLETON_ID)
    ).scalar_one_or_none()
    if row is None:
        ensure_version_row(session)
        return 1
    return row


def bump_version(session: Session, reason: str) -> int:
    """Increment the corpus version and mark active-posting matches stale.

    `SELECT ... FOR UPDATE` on the singleton serializes concurrent bumps: two ingest
    processes racing produce two distinct versions rather than both reading N and both
    writing N+1. The staleness sweep runs inside the same transaction as the bump, so
    a caller can never observe a new version with matches not yet marked.
    """
    row = session.execute(
        select(EvidenceVersion)
        .where(EvidenceVersion.id == SINGLETON_ID)
        .with_for_update()
    ).scalar_one_or_none()
    if row is None:
        # Only ever hit on a schema built from ORM metadata rather than the migration.
        ensure_version_row(session)
        row = session.execute(
            select(EvidenceVersion)
            .where(EvidenceVersion.id == SINGLETON_ID)
            .with_for_update()
        ).scalar_one()

    row.version += 1
    row.bumped_at = _now()
    row.reason = reason
    session.flush()

    stale = mark_active_matches_stale(session)
    log.info("evidence.version_bumped", version=row.version, reason=reason, matches_stale=stale)
    return row.version


def mark_active_matches_stale(session: Session) -> int:
    """Set `is_stale` on matches for ACTIVE postings only. Returns rows touched.

    Inactive postings are excluded deliberately: recomputing a match for a job that no
    longer exists is pure cost, and their historical verdicts stay meaningful as a
    record of what I looked like at that evidence version.
    """
    active = select(Posting.id).where(Posting.inactive_at.is_(None))
    result = session.execute(
        update(Match)
        .where(Match.posting_id.in_(active), Match.is_stale.is_(False))
        .values(is_stale=True)
        .execution_options(synchronize_session=False)
    )
    session.flush()
    session.expire_all()
    return int(result.rowcount or 0)


def version_row(session: Session) -> EvidenceVersion:
    ensure_version_row(session)
    return session.execute(
        select(EvidenceVersion).where(EvidenceVersion.id == SINGLETON_ID)
    ).scalar_one()


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)
