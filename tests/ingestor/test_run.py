"""End-to-end ingest runs: summary, run row, metrics, dry run, and enqueue."""

from __future__ import annotations

import datetime as dt
import json

import httpx
import pytest
import respx
from _feedfixtures import ACME, GLOBEX, HOOLI, MALFORMED, RUN1, RUN2_GLOBEX_GONE, UMBRELLA
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from jme.config import STREAM_FETCH
from jme.ingestor.feed import FeedNotFoundError, FeedParseError
from jme.ingestor.run import STAGE, ingest
from jme.models import FetchStatus, IngestRun, Posting, PostingJD, RunMetric

pytestmark = pytest.mark.integration

FEED_URL = "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json"

T0 = dt.datetime(2026, 2, 1, 9, 0, 0, tzinfo=dt.UTC)
T1 = T0 + dt.timedelta(days=1)


def metrics(session: Session, run_id: str) -> dict[str, float]:
    rows = session.execute(
        select(RunMetric.metric, RunMetric.value).where(RunMetric.run_id == run_id)
    ).all()
    return {metric: float(value) for metric, value in rows}


# --------------------------------------------------------------------------------------
# the acceptance criterion
# --------------------------------------------------------------------------------------


def test_running_twice_in_a_row_produces_zero_new_rows(db_session: Session) -> None:
    first = ingest(db_session, file=RUN1, now=T0)
    second = ingest(db_session, file=RUN1, now=T1)

    assert first.new == 4
    assert second.new == 0
    assert second.total_seen == 4
    assert int(db_session.execute(select(func.count(Posting.id))).scalar() or 0) == 4


def test_summary_reports_the_full_diff(db_session: Session) -> None:
    ingest(db_session, file=RUN1, now=T0)
    summary = ingest(db_session, file=RUN2_GLOBEX_GONE, now=T1)

    assert summary.new == 1  # Hooli
    assert summary.deactivated == 1  # Globex swept
    assert summary.reactivated == 0
    assert summary.total_seen == 4
    assert summary.skipped == 0
    assert summary.duration_sec >= 0


def test_no_deactivate_leaves_absent_postings_alone(db_session: Session) -> None:
    ingest(db_session, file=RUN1, now=T0)
    summary = ingest(db_session, file=RUN2_GLOBEX_GONE, now=T1, deactivate=False)

    assert summary.deactivated == 0
    globex = db_session.execute(
        select(Posting).where(Posting.canonical_key == GLOBEX)
    ).scalar_one()
    assert globex.inactive_at is None


# --------------------------------------------------------------------------------------
# run row and metrics
# --------------------------------------------------------------------------------------


def test_ingest_writes_an_ingest_run_row(db_session: Session) -> None:
    summary = ingest(db_session, file=RUN1, now=T0)

    run = db_session.execute(
        select(IngestRun).where(IngestRun.run_id == summary.run_id)
    ).scalar_one()

    assert run.feed_sha256 == summary.feed_sha256
    assert len(run.feed_sha256) == 64
    assert run.total_seen == 4
    assert run.new_count == 4
    assert run.updated_count == 0
    assert run.deactivated_count == summary.deactivated
    assert run.reactivated_count == 0
    assert run.error is None
    assert run.finished_at is not None


def test_ingest_mirrors_counts_into_run_metric(db_session: Session) -> None:
    summary = ingest(db_session, file=RUN2_GLOBEX_GONE, now=T0)

    recorded = metrics(db_session, summary.run_id)

    assert recorded["total_seen"] == summary.total_seen
    assert recorded["new"] == summary.new
    assert recorded["updated"] == summary.updated
    assert recorded["reactivated"] == summary.reactivated
    assert recorded["deactivated"] == summary.deactivated
    assert recorded["enqueued"] == 0
    assert "duration_sec" in recorded

    stages = db_session.execute(
        select(RunMetric.stage).where(RunMetric.run_id == summary.run_id).distinct()
    ).scalars().all()
    assert stages == [STAGE]


def test_feed_sha256_changes_when_the_feed_changes(db_session: Session) -> None:
    a = ingest(db_session, file=RUN1, now=T0)
    b = ingest(db_session, file=RUN2_GLOBEX_GONE, now=T1)
    assert a.feed_sha256 != b.feed_sha256


# --------------------------------------------------------------------------------------
# dry run
# --------------------------------------------------------------------------------------


def test_dry_run_writes_nothing(db_session: Session) -> None:
    summary = ingest(db_session, file=RUN1, now=T0, dry_run=True)
    db_session.flush()

    assert summary.dry_run is True
    assert summary.new == 4
    assert int(db_session.execute(select(func.count(Posting.id))).scalar() or 0) == 0
    assert int(db_session.execute(select(func.count(IngestRun.id))).scalar() or 0) == 0
    assert int(db_session.execute(select(func.count(RunMetric.id))).scalar() or 0) == 0


def test_dry_run_predicts_the_same_counts_as_the_real_run(db_session: Session) -> None:
    ingest(db_session, file=RUN1, now=T0)

    predicted = ingest(db_session, file=RUN2_GLOBEX_GONE, now=T1, dry_run=True)
    actual = ingest(db_session, file=RUN2_GLOBEX_GONE, now=T1)

    assert (predicted.new, predicted.updated, predicted.reactivated, predicted.deactivated) == (
        actual.new,
        actual.updated,
        actual.reactivated,
        actual.deactivated,
    )


# --------------------------------------------------------------------------------------
# failure paths
# --------------------------------------------------------------------------------------


@respx.mock
def test_404_raises_and_records_a_failed_run(db_session: Session) -> None:
    respx.get(FEED_URL).mock(return_value=httpx.Response(404))

    with pytest.raises(FeedNotFoundError):
        ingest(db_session, url=FEED_URL, run_id="ingest-test-404", now=T0)

    db_session.flush()
    run = db_session.execute(
        select(IngestRun).where(IngestRun.run_id == "ingest-test-404")
    ).scalar_one()
    assert "FeedNotFoundError" in (run.error or "")
    assert run.total_seen == 0
    assert int(db_session.execute(select(func.count(Posting.id))).scalar() or 0) == 0


@respx.mock
def test_malformed_json_raises_and_records_a_failed_run(db_session: Session) -> None:
    respx.get(FEED_URL).mock(
        return_value=httpx.Response(200, content=MALFORMED.read_bytes())
    )

    with pytest.raises(FeedParseError):
        ingest(db_session, url=FEED_URL, run_id="ingest-test-bad-json", now=T0)

    db_session.flush()
    run = db_session.execute(
        select(IngestRun).where(IngestRun.run_id == "ingest-test-bad-json")
    ).scalar_one()
    assert "FeedParseError" in (run.error or "")


@respx.mock
def test_ingest_over_http_matches_ingest_from_file(db_session: Session) -> None:
    respx.get(FEED_URL).mock(return_value=httpx.Response(200, content=RUN1.read_bytes()))

    summary = ingest(db_session, url=FEED_URL, now=T0)

    assert summary.new == 4
    assert summary.feed_sha256 == ingest(db_session, file=RUN1, now=T1, dry_run=True).feed_sha256


# --------------------------------------------------------------------------------------
# enqueue
# --------------------------------------------------------------------------------------


def _jobs(fake_redis) -> list[dict]:
    return [json.loads(fields["payload"]) for _id, fields in fake_redis.xrange(STREAM_FETCH)]


def test_enqueue_is_off_by_default_and_needs_no_redis(db_session: Session) -> None:
    summary = ingest(db_session, file=RUN1, now=T0)
    assert summary.enqueued == 0


def test_enqueue_publishes_one_job_per_active_posting(db_session: Session, fake_redis) -> None:
    summary = ingest(db_session, file=RUN1, now=T0, enqueue=True, redis_client=fake_redis)

    jobs = _jobs(fake_redis)
    assert summary.enqueued == 3  # Umbrella is inactive in the feed
    assert len(jobs) == 3
    assert {j["canonical_key"] for j in jobs} == {
        ACME,
        GLOBEX,
        "cccccccc-3333-4ccc-8ccc-cccccccccccc",
    }
    assert UMBRELLA not in {j["canonical_key"] for j in jobs}


def test_enqueued_payload_matches_the_go_fetchjob_contract(
    db_session: Session, fake_redis
) -> None:
    """Field names come straight from `fetcher/internal/domain/domain.go:FetchJob`."""
    ingest(db_session, file=RUN1, now=T0, enqueue=True, redis_client=fake_redis)

    job = next(j for j in _jobs(fake_redis) if j["canonical_key"] == ACME)

    assert set(job) == {"posting_id", "canonical_key", "url", "company", "title"}
    assert isinstance(job["posting_id"], int) and job["posting_id"] > 0
    assert job["url"] == "https://job-boards.greenhouse.io/acme/jobs/4597297006"
    assert job["company"] == "Acme Corp."
    assert job["title"] == "Software Engineer, New Grad"
    # `attempt` is owned by the queue harness, producers must not set it
    assert "attempt" not in job


def test_enqueue_skips_postings_that_already_have_job_description_text(
    db_session: Session, fake_redis
) -> None:
    ingest(db_session, file=RUN1, now=T0)
    acme = db_session.execute(select(Posting).where(Posting.canonical_key == ACME)).scalar_one()
    db_session.add(
        PostingJD(posting_id=acme.id, fetch_status=FetchStatus.ok, raw_text="x", char_count=1)
    )
    db_session.flush()

    summary = ingest(db_session, file=RUN1, now=T1, enqueue=True, redis_client=fake_redis)

    assert summary.enqueued == 2
    assert ACME not in {j["canonical_key"] for j in _jobs(fake_redis)}


def test_enqueue_retries_a_transient_failure(db_session: Session, fake_redis) -> None:
    ingest(db_session, file=RUN1, now=T0)
    acme = db_session.execute(select(Posting).where(Posting.canonical_key == ACME)).scalar_one()
    db_session.add(
        PostingJD(posting_id=acme.id, fetch_status=FetchStatus.transient_error, attempts=1)
    )
    db_session.flush()

    ingest(db_session, file=RUN1, now=T1, enqueue=True, redis_client=fake_redis)

    assert ACME in {j["canonical_key"] for j in _jobs(fake_redis)}


def test_enqueue_covers_postings_added_by_this_run(db_session: Session, fake_redis) -> None:
    ingest(db_session, file=RUN1, now=T0)
    ingest(db_session, file=RUN2_GLOBEX_GONE, now=T1, enqueue=True, redis_client=fake_redis)

    keys = {j["canonical_key"] for j in _jobs(fake_redis)}
    assert HOOLI in keys  # inserted moments earlier in the same transaction
    assert GLOBEX not in keys  # swept inactive by the same run


def test_enqueue_count_is_recorded_as_a_metric(db_session: Session, fake_redis) -> None:
    summary = ingest(db_session, file=RUN1, now=T0, enqueue=True, redis_client=fake_redis)
    assert metrics(db_session, summary.run_id)["enqueued"] == summary.enqueued == 3
