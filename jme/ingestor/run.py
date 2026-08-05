"""Orchestration: fetch the feed, upsert, sweep, record the run, enqueue fetch jobs.

This module owns the sequencing and nothing else. It takes a Session rather than opening
one so that tests can run the whole pipeline inside a transaction that is rolled back,
and so the CLI controls the commit boundary.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from jme.config import STREAM_FETCH, get_settings
from jme.ingestor.feed import (
    FeedError,
    ParsedFeed,
    fetch_feed,
    load_feed_file,
    parse_feed,
)
from jme.ingestor.upsert import (
    BATCH_SIZE,
    deactivate_missing,
    postings_needing_fetch,
    upsert_postings,
)
from jme.logging import get_logger
from jme.metrics import Timer, new_run_id, record
from jme.models import IngestRun, Posting

if TYPE_CHECKING:  # pragma: no cover - typing only
    import httpx
    import redis

log = get_logger(__name__)

STAGE = "ingest"

# Sentinel: `None` is a meaningful value for `inactive_at`, so absence needs its own token.
_MISSING = object()


@dataclass(slots=True)
class IngestSummary:
    """The run summary. `new + updated + reactivated + deactivated` covers every record
    seen, and `deactivated` additionally includes the sweep of keys absent from the feed.
    """

    run_id: str
    feed_sha256: str
    total_seen: int = 0
    new: int = 0
    updated: int = 0
    reactivated: int = 0
    deactivated: int = 0
    skipped: int = 0
    enqueued: int = 0
    duration_sec: float = 0.0
    dry_run: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _load(
    *,
    url: str | None,
    file: str | Path | None,
    client: httpx.Client | None,
    timeout: float,
    user_agent: str | None,
) -> tuple[bytes, str]:
    if file is not None:
        return load_feed_file(file), str(file)
    settings = get_settings()
    feed_url = url or settings.feed_url
    return (
        fetch_feed(
            feed_url,
            client=client,
            timeout=timeout,
            user_agent=user_agent or settings.user_agent,
        ),
        feed_url,
    )


def _dry_run_counts(
    session: Session, parsed: ParsedFeed, summary: IngestSummary, *, deactivate: bool
) -> None:
    """Classify against the current table without writing anything."""
    table = Posting.__table__
    records = parsed.records
    existing: dict[str, dt.datetime | None] = {}
    for start in range(0, len(records), BATCH_SIZE):
        keys = [r.canonical_key for r in records[start : start + BATCH_SIZE]]
        existing.update(
            dict(
                session.execute(
                    select(table.c.canonical_key, table.c.inactive_at).where(
                        table.c.canonical_key.in_(keys)
                    )
                ).all()
            )
        )

    active_in_feed = 0
    for r in records:
        prior = existing.get(r.canonical_key, _MISSING)
        if prior is _MISSING:
            summary.new += 1
            continue
        if prior is None:
            active_in_feed += 1
        if prior is not None and r.active:
            summary.reactivated += 1
        elif prior is None and not r.active:
            summary.deactivated += 1
        else:
            summary.updated += 1

    if deactivate:
        # Everything currently active that the feed did not mention would be swept.
        # Counted by subtraction rather than a NOT IN over eighteen thousand keys.
        active_total = int(
            session.execute(
                select(func.count(table.c.id)).where(table.c.inactive_at.is_(None))
            ).scalar()
            or 0
        )
        summary.deactivated += max(active_total - active_in_feed, 0)


def _enqueue_fetch_jobs(
    session: Session, redis_client: redis.Redis | None, run_id: str
) -> int:
    """XADD a FetchJob for every active posting that still needs job description text.

    Flushed, not committed: the caller owns the transaction boundary. The producer runs
    after the flush so the SELECT sees this run's rows.
    """
    from jme.queue import Producer, connect

    session.flush()
    jobs = postings_needing_fetch(session)
    if not jobs:
        log.info("no_fetch_jobs", run_id=run_id)
        return 0

    client = redis_client or connect(get_settings().redis_url)
    producer = Producer(client, STREAM_FETCH)
    for start in range(0, len(jobs), 500):
        producer.publish_many(jobs[start : start + 500])

    log.info("fetch_jobs_enqueued", run_id=run_id, count=len(jobs), stream=STREAM_FETCH)
    return len(jobs)


def ingest(
    session: Session,
    *,
    url: str | None = None,
    file: str | Path | None = None,
    client: httpx.Client | None = None,
    redis_client: redis.Redis | None = None,
    enqueue: bool = False,
    dry_run: bool = False,
    deactivate: bool = True,
    now: dt.datetime | None = None,
    run_id: str | None = None,
    timeout: float = 60.0,
    user_agent: str | None = None,
) -> IngestSummary:
    """One full ingest pass.

    The caller commits. Nothing here calls `session.commit()`, so an integration test can
    run the whole thing inside a transaction it later rolls back.
    """
    run_id = run_id or new_run_id(STAGE)
    stamp = now or dt.datetime.now(dt.UTC)

    with Timer() as timer:
        try:
            body, source = _load(
                url=url, file=file, client=client, timeout=timeout, user_agent=user_agent
            )
            parsed = parse_feed(body, source=source)
        except FeedError as exc:
            if not dry_run:
                session.add(
                    IngestRun(
                        run_id=run_id,
                        started_at=stamp,
                        finished_at=dt.datetime.now(dt.UTC),
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
            log.error("ingest_failed", run_id=run_id, error=str(exc))
            raise

        summary = IngestSummary(
            run_id=run_id,
            feed_sha256=parsed.sha256,
            total_seen=len(parsed.records),
            skipped=parsed.skipped,
            dry_run=dry_run,
        )

        if dry_run:
            _dry_run_counts(session, parsed, summary, deactivate=deactivate)
        else:
            result = upsert_postings(session, parsed.records, now=stamp)
            summary.new = result.new
            summary.updated = result.updated
            summary.reactivated = result.reactivated
            summary.deactivated = result.deactivated

            if deactivate:
                summary.deactivated += deactivate_missing(session, stamp)

            if enqueue:
                summary.enqueued = _enqueue_fetch_jobs(session, redis_client, run_id)

    summary.duration_sec = timer.seconds

    if dry_run:
        # Nothing is written, not even the run row: a dry run must leave no trace.
        log.info("ingest_dry_run", **summary.as_dict())
        return summary

    session.add(
        IngestRun(
            run_id=run_id,
            started_at=stamp,
            finished_at=dt.datetime.now(dt.UTC),
            feed_sha256=summary.feed_sha256,
            total_seen=summary.total_seen,
            new_count=summary.new,
            updated_count=summary.updated,
            deactivated_count=summary.deactivated,
            reactivated_count=summary.reactivated,
        )
    )

    for metric in (
        "total_seen",
        "new",
        "updated",
        "reactivated",
        "deactivated",
        "skipped",
        "enqueued",
        "duration_sec",
    ):
        record(session, run_id, STAGE, metric, float(getattr(summary, metric)))

    session.flush()
    log.info("ingest_complete", **summary.as_dict())
    return summary
