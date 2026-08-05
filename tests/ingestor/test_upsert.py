"""Upsert and lifecycle tests. These need a real Postgres: the whole design lives in
`INSERT ... ON CONFLICT`, and SQLite would test nothing.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import threading
import time
import uuid
from pathlib import Path

import pytest
from _feedfixtures import (
    ACME,
    GLOBEX,
    HOOLI,
    INITECH,
    RUN1,
    RUN2_GLOBEX_GONE,
    RUN3_GLOBEX_BACK,
    UMBRELLA,
)
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from jme.ingestor.feed import FeedRecord, parse_feed
from jme.ingestor.upsert import deactivate_missing, postings_needing_fetch, upsert_postings
from jme.models import FetchStatus, Posting, PostingJD

pytestmark = pytest.mark.integration

T0 = dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=dt.UTC)
T1 = T0 + dt.timedelta(days=1)
T2 = T0 + dt.timedelta(days=2)
T3 = T0 + dt.timedelta(days=3)


def records(path: Path) -> list[FeedRecord]:
    return parse_feed(path.read_bytes()).records


def posting(session: Session, canonical_key: str) -> Posting:
    return session.execute(
        select(Posting).where(Posting.canonical_key == canonical_key)
    ).scalar_one()


def count_postings(session: Session) -> int:
    return int(session.execute(select(func.count(Posting.id))).scalar() or 0)


# --------------------------------------------------------------------------------------
# idempotence
# --------------------------------------------------------------------------------------


def test_running_twice_produces_zero_new_rows(db_session: Session) -> None:
    first = upsert_postings(db_session, records(RUN1), now=T0)
    assert first.new == 4
    assert first.updated == 0
    assert count_postings(db_session) == 4

    second = upsert_postings(db_session, records(RUN1), now=T1)
    assert second.new == 0
    assert second.total == 4
    assert count_postings(db_session) == 4


def test_running_three_times_still_produces_zero_new_rows(db_session: Session) -> None:
    for stamp in (T0, T1, T2):
        result = upsert_postings(db_session, records(RUN1), now=stamp)
        assert result.new == (4 if stamp is T0 else 0)
    assert count_postings(db_session) == 4


def test_second_run_bumps_last_seen_but_not_first_seen(db_session: Session) -> None:
    upsert_postings(db_session, records(RUN1), now=T0)
    upsert_postings(db_session, records(RUN1), now=T1)

    row = posting(db_session, ACME)
    assert row.first_seen_at == T0
    assert row.last_seen_at == T1
    assert row.repost_count == 0


# --------------------------------------------------------------------------------------
# field mapping
# --------------------------------------------------------------------------------------


def test_extracted_fields_are_persisted(db_session: Session) -> None:
    upsert_postings(db_session, records(RUN1), now=T0)

    acme = posting(db_session, ACME)
    assert acme.company == "Acme Corp."
    assert acme.simplify_id == ACME
    assert acme.url_host == "job-boards.greenhouse.io"
    assert acme.role_type == "swe"
    assert acme.is_remote is False
    assert acme.locations == ["Detroit, MI", "Ann Arbor, MI"]
    assert acme.sponsorship == "Offers Sponsorship"
    assert acme.posted_at == dt.datetime(2025, 12, 10, 11, 14, 41, tzinfo=dt.UTC)
    assert acme.raw["degrees"] == ["Bachelor's"]

    globex = posting(db_session, GLOBEX)
    assert globex.is_remote is True
    assert globex.start_season == "Summer 2026"
    assert globex.url_host == "jobs.lever.co"

    initech = posting(db_session, INITECH)
    assert initech.role_type == "quant"
    assert initech.sponsorship == "U.S. Citizenship is Required"

    umbrella = posting(db_session, UMBRELLA)
    assert umbrella.role_type == "swe"
    # "Spring Boot" is not a start season
    assert umbrella.start_season is None


def test_mutable_fields_are_updated_on_the_second_run(db_session: Session) -> None:
    upsert_postings(db_session, records(RUN1), now=T0)
    assert posting(db_session, ACME).title == "Software Engineer, New Grad"

    upsert_postings(db_session, records(RUN2_GLOBEX_GONE), now=T1)

    acme = posting(db_session, ACME)
    assert acme.title == "Software Engineer II, New Grad"
    assert acme.locations == ["Detroit, MI", "Ann Arbor, MI", "Remote in USA"]
    assert acme.is_remote is True
    assert acme.last_seen_at == T1


def test_posted_at_is_not_regressed_to_null(db_session: Session) -> None:
    [acme] = [r for r in records(RUN1) if r.canonical_key == ACME]
    upsert_postings(db_session, [acme], now=T0)

    thinner = dataclasses.replace(acme, posted_at=None)
    upsert_postings(db_session, [thinner], now=T1)

    assert posting(db_session, ACME).posted_at is not None


# --------------------------------------------------------------------------------------
# lifecycle: disappear, return, repost
# --------------------------------------------------------------------------------------


def test_posting_absent_from_the_feed_is_deactivated_not_deleted(db_session: Session) -> None:
    upsert_postings(db_session, records(RUN1), now=T0)
    assert posting(db_session, GLOBEX).inactive_at is None

    upsert_postings(db_session, records(RUN2_GLOBEX_GONE), now=T1)
    swept = deactivate_missing(db_session, T1)

    assert swept == 1
    assert count_postings(db_session) == 5  # nothing deleted, Hooli added
    assert posting(db_session, GLOBEX).inactive_at == T1


def test_disappear_then_return_clears_inactive_at_and_increments_repost_count(
    db_session: Session,
) -> None:
    """The acceptance test from BUILD_PLAN.md, run over three real feed snapshots."""
    # run 1: Globex is present and active
    upsert_postings(db_session, records(RUN1), now=T0)
    deactivate_missing(db_session, T0)
    globex = posting(db_session, GLOBEX)
    assert globex.inactive_at is None
    assert globex.repost_count == 0
    first_seen = globex.first_seen_at

    # run 2: Globex has vanished from the feed
    result2 = upsert_postings(db_session, records(RUN2_GLOBEX_GONE), now=T1)
    swept2 = deactivate_missing(db_session, T1)
    db_session.expire_all()
    globex = posting(db_session, GLOBEX)
    assert result2.reactivated == 0
    assert swept2 == 1
    assert globex.inactive_at == T1
    assert globex.repost_count == 0
    assert globex.last_seen_at == T0  # not touched by run 2

    # run 3: Globex is back
    result3 = upsert_postings(db_session, records(RUN3_GLOBEX_BACK), now=T2)
    swept3 = deactivate_missing(db_session, T2)
    db_session.expire_all()
    globex = posting(db_session, GLOBEX)
    assert result3.reactivated == 1
    assert result3.new == 0
    assert swept3 == 0
    assert globex.inactive_at is None
    assert globex.repost_count == 1
    assert globex.last_seen_at == T2
    assert globex.first_seen_at == first_seen  # history preserved across the round trip

    # run 4: gone again, the stamp is re-set
    upsert_postings(db_session, records(RUN2_GLOBEX_GONE), now=T3)
    deactivate_missing(db_session, T3)
    db_session.expire_all()
    globex = posting(db_session, GLOBEX)
    assert globex.inactive_at == T3
    assert globex.repost_count == 1


def test_repost_count_increments_once_per_cycle(db_session: Session) -> None:
    present = records(RUN3_GLOBEX_BACK)
    absent = records(RUN2_GLOBEX_GONE)

    stamps = [T0 + dt.timedelta(days=n) for n in range(6)]
    for index, stamp in enumerate(stamps):
        upsert_postings(db_session, present if index % 2 == 0 else absent, now=stamp)
        deactivate_missing(db_session, stamp)

    db_session.expire_all()
    # present at 0, gone at 1, back at 2, gone at 3, back at 4, gone at 5 -> two reposts
    assert posting(db_session, GLOBEX).repost_count == 2


def test_staying_present_does_not_increment_repost_count(db_session: Session) -> None:
    for stamp in (T0, T1, T2, T3):
        upsert_postings(db_session, records(RUN1), now=stamp)
        deactivate_missing(db_session, stamp)

    db_session.expire_all()
    assert posting(db_session, ACME).repost_count == 0


# --------------------------------------------------------------------------------------
# lifecycle: the feed's own inactive flag
# --------------------------------------------------------------------------------------


def test_feed_flagged_inactive_lands_inactive_on_first_sight(db_session: Session) -> None:
    upsert_postings(db_session, records(RUN1), now=T0)
    row = posting(db_session, UMBRELLA)
    assert row.inactive_at == T0
    assert row.repost_count == 0


def test_original_inactive_timestamp_is_preserved_across_runs(db_session: Session) -> None:
    """A posting that has been dead for a month should not look like it died today."""
    upsert_postings(db_session, records(RUN1), now=T0)
    assert posting(db_session, UMBRELLA).inactive_at == T0

    for stamp in (T1, T2, T3):
        upsert_postings(db_session, records(RUN1), now=stamp)
        db_session.expire_all()
        assert posting(db_session, UMBRELLA).inactive_at == T0
        assert posting(db_session, UMBRELLA).last_seen_at == stamp


def test_active_posting_flagged_inactive_by_the_feed_is_counted_as_deactivated(
    db_session: Session,
) -> None:
    [acme] = [r for r in records(RUN1) if r.canonical_key == ACME]
    upsert_postings(db_session, [acme], now=T0)

    result = upsert_postings(db_session, [dataclasses.replace(acme, active=False)], now=T1)

    assert result.deactivated == 1
    assert result.updated == 0
    assert posting(db_session, ACME).inactive_at == T1


def test_feed_flagged_inactive_posting_reactivates_with_a_repost(db_session: Session) -> None:
    [umbrella] = [r for r in records(RUN1) if r.canonical_key == UMBRELLA]
    upsert_postings(db_session, [umbrella], now=T0)
    assert posting(db_session, UMBRELLA).inactive_at == T0

    result = upsert_postings(db_session, [dataclasses.replace(umbrella, active=True)], now=T1)

    db_session.expire_all()
    row = posting(db_session, UMBRELLA)
    assert result.reactivated == 1
    assert row.inactive_at is None
    assert row.repost_count == 1


def test_deactivate_missing_does_not_touch_already_inactive_rows(db_session: Session) -> None:
    upsert_postings(db_session, records(RUN1), now=T0)
    # Umbrella is already inactive at T0; sweeping at T1 must leave its stamp alone
    upsert_postings(db_session, [r for r in records(RUN1) if r.canonical_key == ACME], now=T1)
    swept = deactivate_missing(db_session, T1)

    db_session.expire_all()
    assert posting(db_session, UMBRELLA).inactive_at == T0
    assert swept == 2  # Globex and Initech, not Umbrella


def test_nothing_is_ever_hard_deleted(db_session: Session) -> None:
    upsert_postings(db_session, records(RUN1), now=T0)
    upsert_postings(db_session, [], now=T1)
    deactivate_missing(db_session, T1)

    assert count_postings(db_session) == 4
    assert (
        int(
            db_session.execute(
                select(func.count(Posting.id)).where(Posting.inactive_at.is_(None))
            ).scalar()
            or 0
        )
        == 0
    )


def test_counts_are_disjoint(db_session: Session) -> None:
    upsert_postings(db_session, records(RUN1), now=T0)
    result = upsert_postings(db_session, records(RUN3_GLOBEX_BACK), now=T1)

    assert result.new + result.updated + result.reactivated + result.deactivated == result.total


# --------------------------------------------------------------------------------------
# fetch job selection
# --------------------------------------------------------------------------------------


def test_postings_needing_fetch_covers_active_rows_without_a_jd(db_session: Session) -> None:
    upsert_postings(db_session, records(RUN1), now=T0)
    db_session.flush()

    jobs = postings_needing_fetch(db_session)

    assert {j["canonical_key"] for j in jobs} == {ACME, GLOBEX, INITECH}  # Umbrella is inactive
    assert set(jobs[0]) == {"posting_id", "canonical_key", "url", "company", "title"}
    assert all(isinstance(j["posting_id"], int) and j["posting_id"] > 0 for j in jobs)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (FetchStatus.pending, True),
        (FetchStatus.rate_limited, True),
        (FetchStatus.transient_error, True),
        (FetchStatus.ok, False),
        (FetchStatus.not_found, False),
        (FetchStatus.permanent_error, False),
        (FetchStatus.robots_denied, False),
        (FetchStatus.unsupported, False),
    ],
)
def test_postings_needing_fetch_respects_retryability(
    db_session: Session, status: FetchStatus, expected: bool
) -> None:
    upsert_postings(db_session, records(RUN1), now=T0)
    db_session.flush()
    acme = posting(db_session, ACME)
    db_session.add(PostingJD(posting_id=acme.id, fetch_status=status))
    db_session.flush()

    keys = {j["canonical_key"] for j in postings_needing_fetch(db_session)}
    assert (ACME in keys) is expected


def test_postings_needing_fetch_skips_deactivated_postings(db_session: Session) -> None:
    upsert_postings(db_session, records(RUN1), now=T0)
    upsert_postings(db_session, records(RUN2_GLOBEX_GONE), now=T1)
    deactivate_missing(db_session, T1)
    db_session.flush()

    keys = {j["canonical_key"] for j in postings_needing_fetch(db_session)}
    assert GLOBEX not in keys
    assert HOOLI in keys


# --------------------------------------------------------------------------------------
# concurrency
# --------------------------------------------------------------------------------------


def _record(key: str, *, active: bool = True) -> FeedRecord:
    return FeedRecord(
        canonical_key=key,
        simplify_id=key,
        company="Concurrent Co",
        title="Software Engineer",
        url=f"https://jobs.lever.co/concurrent/{key}",
        url_host="jobs.lever.co",
        locations=["Detroit, MI"],
        sponsorship="Other",
        role_type="swe",
        start_season=None,
        is_remote=False,
        posted_at=T0,
        active=active,
        raw={"id": key},
    )


def _committed_session(db_engine, lock_timeout_ms: int = 20_000) -> Session:
    session = sessionmaker(bind=db_engine, expire_on_commit=False)()
    session.execute(select(1))
    session.connection().exec_driver_sql(f"SET lock_timeout = '{lock_timeout_ms}ms'")
    return session


def test_interleaved_upsert_in_two_sessions_inserts_exactly_one_row(db_engine) -> None:
    """Two ingestors racing on the same key. ON CONFLICT does the work, not a pre-check.

    The second session is started while the first still holds its uncommitted insert, so
    it genuinely blocks on the unique index rather than running after the fact.
    """
    key = f"concurrent-{uuid.uuid4()}"
    session_a = _committed_session(db_engine)
    session_b = _committed_session(db_engine)
    result_b: dict = {}

    def run_b() -> None:
        try:
            result_b["result"] = upsert_postings(session_b, [_record(key)], now=T1)
            session_b.commit()
        except Exception as exc:  # noqa: BLE001 - surfaced as a test failure below
            result_b["error"] = exc
            session_b.rollback()

    try:
        result_a = upsert_postings(session_a, [_record(key)], now=T0)  # not committed yet
        thread = threading.Thread(target=run_b, daemon=True)
        thread.start()
        time.sleep(0.4)  # B is now blocked on the unique index
        assert thread.is_alive(), "second session should be waiting on the first"
        session_a.commit()
        thread.join(timeout=30)
        assert not thread.is_alive()
        assert "error" not in result_b, result_b.get("error")

        assert result_a.new == 1
        assert result_b["result"].new == 0  # B took the DO UPDATE arm
        assert result_b["result"].updated == 1

        with sessionmaker(bind=db_engine)() as check:
            rows = check.execute(select(Posting).where(Posting.canonical_key == key)).scalars().all()
            assert len(rows) == 1
            assert rows[0].repost_count == 0
            assert rows[0].inactive_at is None
            assert rows[0].last_seen_at == T1
    finally:
        session_a.close()
        session_b.close()
        with sessionmaker(bind=db_engine)() as cleanup:
            cleanup.execute(Posting.__table__.delete().where(Posting.canonical_key == key))
            cleanup.commit()


def test_concurrent_reactivation_increments_repost_count_exactly_once(db_engine) -> None:
    """Two runs both seeing an inactive posting come back must not double-count the repost."""
    key = f"concurrent-{uuid.uuid4()}"
    setup = sessionmaker(bind=db_engine)()
    try:
        upsert_postings(setup, [_record(key, active=False)], now=T0)
        setup.commit()
    finally:
        setup.close()

    session_a = _committed_session(db_engine)
    session_b = _committed_session(db_engine)
    result_b: dict = {}

    def run_b() -> None:
        try:
            result_b["result"] = upsert_postings(session_b, [_record(key)], now=T2)
            session_b.commit()
        except Exception as exc:  # noqa: BLE001
            result_b["error"] = exc
            session_b.rollback()

    try:
        result_a = upsert_postings(session_a, [_record(key)], now=T1)
        thread = threading.Thread(target=run_b, daemon=True)
        thread.start()
        time.sleep(0.4)  # B is blocked on A's FOR UPDATE lock
        assert thread.is_alive(), "second session should be waiting on the row lock"
        session_a.commit()
        thread.join(timeout=30)
        assert "error" not in result_b, result_b.get("error")

        assert result_a.reactivated == 1
        # B re-reads the row after A commits: it is already active, so no second bump
        assert result_b["result"].reactivated == 0

        with sessionmaker(bind=db_engine)() as check:
            row = check.execute(select(Posting).where(Posting.canonical_key == key)).scalar_one()
            assert row.repost_count == 1
            assert row.inactive_at is None
    finally:
        session_a.close()
        session_b.close()
        with sessionmaker(bind=db_engine)() as cleanup:
            cleanup.execute(Posting.__table__.delete().where(Posting.canonical_key == key))
            cleanup.commit()
