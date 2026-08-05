"""The funnel end to end: persistence, metrics, and determinism.

Determinism is the one worth staring at. `test_two_runs_produce_identical_rankings`
builds a field of exact ties on purpose, because ties are where a ranking quietly
stops being reproducible -- an unordered SQL result reaching a sort with no
tie-breaker gives a different answer on a different day, and nothing in the output
tells you it happened.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from jme.models import Importance, RunMetric, ShortlistEntry
from jme.rank import pipeline
from jme.rank.filters import DROP_REASONS
from tests.rank.factories import make_settings

pytestmark = pytest.mark.integration

EVIDENCE = (
    "Built a Python service on Postgres with SQLAlchemy and pgvector, tuned with "
    "EXPLAIN ANALYZE, plus a Redis Streams consumer group in Go."
)


def _metrics(session, run_id: str) -> dict[str, float]:
    rows = session.execute(
        select(RunMetric.metric, RunMetric.value).where(
            RunMetric.run_id == run_id, RunMetric.stage == "rank"
        )
    ).all()
    return {metric: float(value) for metric, value in rows}


def test_writes_shortlist_entries_in_rank_order(
    db_session, make_posting, add_requirement, add_chunk, settings, provider
):
    add_chunk(EVIDENCE)
    strong = make_posting(company="Strong")
    weak = make_posting(company="Weak")
    add_requirement(strong, "Experience with Python, Postgres and pgvector", Importance.required)
    add_requirement(weak, "Fluency in Sumerian cuneiform", Importance.required)

    entries = pipeline.run(db_session, "run-a", settings=settings, provider=provider)

    assert [e.rank for e in entries] == [1, 2]
    assert entries[0].posting_id == strong.id
    assert entries[1].posting_id == weak.id
    assert all(e.run_id == "run-a" for e in entries)
    assert all(e.evidence_version == 1 for e in entries)

    persisted = db_session.execute(
        select(ShortlistEntry).where(ShortlistEntry.run_id == "run-a").order_by(ShortlistEntry.rank)
    ).scalars().all()
    assert [p.posting_id for p in persisted] == [strong.id, weak.id]


def test_two_runs_produce_identical_rankings_including_ties(
    db_session, make_posting, add_requirement, add_chunk, settings, provider
):
    """Same inputs, same evidence version -> identical output, tie order included."""
    add_chunk(EVIDENCE)
    twins = [make_posting(company="Twin") for _ in range(6)]
    for twin in twins:
        add_requirement(twin, "Experience with Python and Postgres", Importance.required)
    distinct = make_posting(company="Distinct")
    add_requirement(distinct, "Experience with Redis Streams consumer groups", Importance.required)

    first = pipeline.run(db_session, "run-1", settings=settings, provider=provider)
    second = pipeline.run(db_session, "run-2", settings=settings, provider=provider)

    signature = lambda entries: [(e.rank, e.posting_id, float(e.coarse_score)) for e in entries]  # noqa: E731
    assert signature(first) == signature(second)

    tied = [e.posting_id for e in first if e.posting_id in {t.id for t in twins}]
    assert tied == sorted(tied), "ties must fall back to ascending posting_id"


def test_rerunning_the_same_run_id_replaces_rather_than_duplicates(
    db_session, make_posting, add_chunk, settings, provider
):
    add_chunk(EVIDENCE)
    make_posting()

    pipeline.run(db_session, "run-same", settings=settings, provider=provider)
    pipeline.run(db_session, "run-same", settings=settings, provider=provider)

    count = len(
        db_session.execute(
            select(ShortlistEntry).where(ShortlistEntry.run_id == "run-same")
        ).scalars().all()
    )
    assert count == 1


def test_shortlist_size_comes_from_config_and_is_overridable(
    db_session, make_posting, add_chunk, provider
):
    add_chunk(EVIDENCE)
    for _ in range(5):
        make_posting()

    from_config = pipeline.run(
        db_session, "run-cfg", settings=make_settings(shortlist_size=2), provider=provider
    )
    from_limit = pipeline.run(
        db_session, "run-lim", 3, settings=make_settings(shortlist_size=2), provider=provider
    )

    assert len(from_config) == 2
    assert len(from_limit) == 3


def test_records_stage_counts_and_every_drop_reason(
    db_session, make_posting, add_chunk, settings, provider
):
    import datetime as dt

    add_chunk(EVIDENCE)
    make_posting()
    make_posting(inactive_at=dt.datetime(2026, 6, 1, tzinfo=dt.UTC))
    make_posting(role_type="quant")

    pipeline.run(db_session, "run-metrics", settings=settings, provider=provider)
    metrics = _metrics(db_session, "run-metrics")

    assert metrics["total_postings"] == 3
    assert metrics["after_filters"] == 1
    assert metrics["after_ranking"] == 1
    assert metrics["drop_inactive"] == 1
    assert metrics["drop_role_type"] == 1
    # every bucket present, zeros included: a missing bucket reads as a zero and lies
    for reason in DROP_REASONS:
        assert f"drop_{reason}" in metrics
    assert metrics["evidence_version"] == 1


def test_funnel_reads_back_in_stage_order(db_session, make_posting, add_chunk, settings, provider):
    add_chunk(EVIDENCE)
    make_posting()
    pipeline.run(db_session, "run-funnel", settings=settings, provider=provider)

    rows = pipeline.funnel(db_session, "run-funnel")
    names = [metric for metric, _, _ in rows]

    assert names[0] == "total_postings"
    assert names.index("after_filters") < names.index("after_ranking")
    assert pipeline.latest_run_id(db_session) == "run-funnel"


def test_dry_run_writes_nothing(db_session, make_posting, add_chunk, settings, provider):
    add_chunk(EVIDENCE)
    make_posting()

    result = pipeline.rank(
        db_session, "run-dry", settings=settings, provider=provider, dry_run=True
    )

    assert len(result.entries) == 1
    assert result.entries[0].id is None  # never flushed
    assert _metrics(db_session, "run-dry") == {}
    assert (
        db_session.execute(
            select(ShortlistEntry).where(ShortlistEntry.run_id == "run-dry")
        ).scalars().all()
        == []
    )


def test_empty_database_is_not_an_error(db_session, settings, provider):
    entries = pipeline.run(db_session, "run-empty", settings=settings, provider=provider)

    assert entries == []
    assert _metrics(db_session, "run-empty")["total_postings"] == 0
