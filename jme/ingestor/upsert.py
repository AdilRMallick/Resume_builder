"""All the SQL for feed ingestion. Every function takes a Session so it is testable.

The lifecycle rules from ARCHITECTURE.md, expressed as one statement:

  * a key never seen before is INSERTed
  * a key seen again UPDATEs its mutable fields and bumps `last_seen_at`
  * a key that was inactive and is active again clears `inactive_at` and increments
    `repost_count`
  * a key the feed flags inactive keeps its *original* `inactive_at`, it does not get
    re-stamped on every run
  * a key absent from the feed entirely is swept inactive afterwards
  * nothing is ever deleted

All of that lives inside `INSERT ... ON CONFLICT (canonical_key) DO UPDATE`, so two
ingestors racing on the same feed converge on the same rows rather than duplicating or
losing them.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from sqlalchemy import and_, case, func, literal_column, null, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from jme.ingestor.feed import FeedRecord
from jme.logging import get_logger
from jme.models import FetchStatus, Posting, PostingJD

log = get_logger(__name__)

# Postgres caps a statement at 65535 bind parameters. Fourteen columns a row leaves
# plenty of headroom at 500 and keeps the multi-VALUES statement a sane size.
BATCH_SIZE = 500

# A posting is worth (re)queueing for fetch when it has never been attempted or when the
# last attempt failed in a way the Go fetcher considers retryable. Mirrors
# `domain.Retryable`: not_found, permanent_error and robots_denied are terminal, and
# `unsupported` means no adapter exists yet, so retrying it changes nothing.
RETRYABLE_FETCH_STATUSES = (
    FetchStatus.pending,
    FetchStatus.rate_limited,
    FetchStatus.transient_error,
)

_MUTABLE_TEXT_LIMITS = {
    "company": 512,
    "title": 512,
    "url_host": 255,
    "sponsorship": 128,
    "role_type": 64,
    "start_season": 64,
    "simplify_id": 128,
    "canonical_key": 128,
}


def _clip(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    return value[:limit]


@dataclass(slots=True)
class UpsertResult:
    """Counts from one upsert pass.

    `new`, `updated`, `reactivated` and `deactivated` are disjoint: every record lands in
    exactly one bucket, which is what makes the run summary read like a diff rather than
    like overlapping totals.
    """

    total: int = 0
    new: int = 0
    updated: int = 0
    reactivated: int = 0
    deactivated: int = 0
    posting_ids: dict[str, int] = field(default_factory=dict)

    def merge(self, other: UpsertResult) -> None:
        self.total += other.total
        self.new += other.new
        self.updated += other.updated
        self.reactivated += other.reactivated
        self.deactivated += other.deactivated
        self.posting_ids.update(other.posting_ids)


def _row_values(record: FeedRecord, now: dt.datetime) -> dict:
    inactive_at = None if record.active else now
    return {
        "canonical_key": _clip(record.canonical_key, _MUTABLE_TEXT_LIMITS["canonical_key"]),
        "simplify_id": _clip(record.simplify_id, _MUTABLE_TEXT_LIMITS["simplify_id"]),
        "company": _clip(record.company, _MUTABLE_TEXT_LIMITS["company"]),
        "title": _clip(record.title, _MUTABLE_TEXT_LIMITS["title"]),
        "url": record.url,
        "url_host": _clip(record.url_host, _MUTABLE_TEXT_LIMITS["url_host"]),
        "locations": record.locations,
        "sponsorship": _clip(record.sponsorship, _MUTABLE_TEXT_LIMITS["sponsorship"]),
        "role_type": _clip(record.role_type, _MUTABLE_TEXT_LIMITS["role_type"]),
        "start_season": _clip(record.start_season, _MUTABLE_TEXT_LIMITS["start_season"]),
        "is_remote": record.is_remote,
        "posted_at": record.posted_at,
        "first_seen_at": now,
        "last_seen_at": now,
        "inactive_at": inactive_at,
        "repost_count": 0,
        "raw": record.raw,
    }


def _upsert_batch(session: Session, batch: Sequence[FeedRecord], now: dt.datetime) -> UpsertResult:
    table = Posting.__table__
    keys = [r.canonical_key for r in batch]

    # Lock the rows we are about to touch, and read the lifecycle state we need *before*
    # the update overwrites it. `FOR UPDATE` is what makes the reactivation count exact
    # when two ingestors overlap: the second one waits rather than reading stale state.
    existing: dict[str, dt.datetime | None] = dict(
        session.execute(
            select(table.c.canonical_key, table.c.inactive_at)
            .where(table.c.canonical_key.in_(keys))
            .with_for_update()
        ).all()
    )

    stmt = pg_insert(table).values([_row_values(r, now) for r in batch])
    excluded = stmt.excluded

    stmt = stmt.on_conflict_do_update(
        index_elements=[table.c.canonical_key],
        set_={
            # mutable fields: the feed is authoritative
            "company": excluded.company,
            "title": excluded.title,
            "url": excluded.url,
            "url_host": excluded.url_host,
            "locations": excluded.locations,
            "sponsorship": excluded.sponsorship,
            "role_type": excluded.role_type,
            "start_season": excluded.start_season,
            "is_remote": excluded.is_remote,
            "raw": excluded.raw,
            "last_seen_at": excluded.last_seen_at,
            # never regress a known posted_at / simplify_id to NULL on a thinner record
            "posted_at": func.coalesce(excluded.posted_at, table.c.posted_at),
            "simplify_id": func.coalesce(excluded.simplify_id, table.c.simplify_id),
            # lifecycle. Coming back alive clears the stamp; staying dead preserves the
            # original one so "inactive since" stays meaningful across runs.
            "inactive_at": case(
                (excluded.inactive_at.is_(None), null()),
                else_=func.coalesce(table.c.inactive_at, excluded.inactive_at),
            ),
            "repost_count": table.c.repost_count
            + case(
                (
                    and_(table.c.inactive_at.is_not(None), excluded.inactive_at.is_(None)),
                    1,
                ),
                else_=0,
            ),
        },
    ).returning(
        table.c.canonical_key,
        table.c.id,
        # The standard Postgres idiom for "did this row come from the INSERT arm?".
        # A freshly inserted tuple has xmax = 0; one updated by the DO UPDATE arm carries
        # the updating transaction's xid. This stays correct even when a concurrent run
        # inserted the row between our SELECT above and this statement.
        literal_column("(xmax = 0)").label("inserted"),
    )

    result = UpsertResult(total=len(batch))
    active_by_key = {r.canonical_key: r.active for r in batch}

    for canonical_key, posting_id, inserted in session.execute(stmt).all():
        result.posting_ids[canonical_key] = posting_id
        if inserted:
            result.new += 1
            continue
        was_inactive = existing.get(canonical_key) is not None
        now_active = active_by_key.get(canonical_key, True)
        if was_inactive and now_active:
            result.reactivated += 1
        elif not was_inactive and not now_active:
            result.deactivated += 1
        else:
            result.updated += 1

    return result


def upsert_postings(
    session: Session,
    records: Iterable[FeedRecord],
    *,
    now: dt.datetime | None = None,
    batch_size: int = BATCH_SIZE,
) -> UpsertResult:
    """Upsert every record. Idempotent: re-running with the same feed inserts nothing.

    `now` is stamped identically onto every row's `last_seen_at`, which is what lets
    `deactivate_missing` identify untouched rows with a single indexed comparison
    instead of a NOT IN over eighteen thousand keys.
    """
    stamp = now or dt.datetime.now(dt.UTC)
    batch: list[FeedRecord] = []
    total = UpsertResult()

    for record in records:
        batch.append(record)
        if len(batch) >= batch_size:
            total.merge(_upsert_batch(session, batch, stamp))
            batch = []
    if batch:
        total.merge(_upsert_batch(session, batch, stamp))

    log.info(
        "postings_upserted",
        total=total.total,
        new=total.new,
        updated=total.updated,
        reactivated=total.reactivated,
        deactivated=total.deactivated,
    )
    return total


def deactivate_missing(session: Session, run_stamp: dt.datetime) -> int:
    """Mark every active posting the feed did not mention this run as inactive.

    Rows touched by this run all carry `last_seen_at == run_stamp` exactly, so anything
    strictly older was absent from the feed. Uses `ix_posting_last_seen`.
    """
    result = session.execute(
        update(Posting.__table__)
        .where(
            Posting.__table__.c.inactive_at.is_(None),
            Posting.__table__.c.last_seen_at < run_stamp,
        )
        .values(inactive_at=run_stamp)
    )
    count = int(result.rowcount or 0)
    if count:
        log.info("postings_deactivated_missing", count=count)
    return count


def postings_needing_fetch(session: Session) -> list[dict]:
    """Active postings with no JD yet, or whose last fetch failed retryably.

    Shape matches `fetcher/internal/domain.FetchJob` exactly. `attempt` is omitted
    deliberately: the queue harness owns it, producers never set it.
    """
    table = Posting.__table__
    jd = PostingJD.__table__

    rows = session.execute(
        select(table.c.id, table.c.canonical_key, table.c.url, table.c.company, table.c.title)
        .select_from(table.outerjoin(jd, jd.c.posting_id == table.c.id))
        .where(
            table.c.inactive_at.is_(None),
            (jd.c.posting_id.is_(None)) | (jd.c.fetch_status.in_(RETRYABLE_FETCH_STATUSES)),
        )
        .order_by(table.c.id)
    ).all()

    return [
        {
            "posting_id": int(row.id),
            "canonical_key": row.canonical_key,
            "url": row.url,
            "company": row.company,
            "title": row.title,
        }
        for row in rows
    ]
