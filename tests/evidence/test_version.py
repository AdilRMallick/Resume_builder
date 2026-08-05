"""Version bumps, the staleness sweep, and manual tagging."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select, text

from jme.evidence.ingest import (
    TaxonomyEmptyError,
    UnknownSkillError,
    chunk_skills,
    ingest,
    live_chunks,
    skills_by_chunk,
    tag_chunk,
    untag_chunk,
)
from jme.evidence.sources import SOURCE_MARKDOWN, SourceRecord
from jme.evidence.version import (
    bump_version,
    current_version,
    ensure_version_row,
    mark_active_matches_stale,
    version_row,
)
from jme.models import EvidenceSkill, EvidenceVersion, Match, MatchCitation
from tests.evidence.conftest import make_match, make_posting, make_skill

pytestmark = pytest.mark.integration

DOC = "# Resume\n\n## Experience\n\n- Built a Redis Streams pipeline.\n- Designed the schema.\n"


def _rec(ref: str, body: str) -> SourceRecord:
    return SourceRecord(SOURCE_MARKDOWN, ref, body)


# --------------------------------------------------------------------------------------
# the counter itself
# --------------------------------------------------------------------------------------


def test_bump_is_monotonic_and_records_why(db_session):
    start = current_version(db_session)

    assert bump_version(db_session, "first") == start + 1
    assert bump_version(db_session, "second") == start + 2

    row = version_row(db_session)
    assert row.version == start + 2
    assert row.reason == "second"
    assert row.bumped_at is not None


def test_version_row_is_a_singleton(db_session):
    current_version(db_session)
    count = db_session.execute(select(func.count()).select_from(version_row(db_session).__table__))
    assert count.scalar_one() == 1


def test_bump_takes_a_row_lock(db_engine):
    """`SELECT ... FOR UPDATE` is what stops two ingests both writing version N+1.

    Without the lock both transactions read N and both write N+1, and one corpus
    change silently disappears from the cache key.
    """
    from sqlalchemy import delete
    from sqlalchemy.exc import DBAPIError
    from sqlalchemy.orm import Session

    # This is the one test that cannot use the rollback-per-test `db_session`: two
    # transactions contending for the same row have to be two real connections, and
    # the seed row has to be genuinely committed for the holder to lock it. That means
    # this test also owns its cleanup - anything it leaves behind is visible to every
    # later test in the session.
    seed = Session(bind=db_engine)
    ensure_version_row(seed)
    seed.commit()
    seed.close()

    try:
        holder = Session(bind=db_engine.connect())
        waiter = Session(bind=db_engine.connect())
        try:
            holder.begin()
            bump_version(holder, "holder")  # holds the row lock, transaction still open

            waiter.begin()
            waiter.execute(text("SET LOCAL lock_timeout = '400ms'"))
            with pytest.raises(DBAPIError) as caught:
                bump_version(waiter, "waiter")
            assert "lock timeout" in str(caught.value)
        finally:
            waiter.rollback()
            waiter.close()
            holder.rollback()
            holder.close()

        # the holder rolled back, so the version is untouched
        after = Session(bind=db_engine)
        assert current_version(after) == 1
        after.close()
    finally:
        cleanup = Session(bind=db_engine)
        cleanup.execute(delete(EvidenceVersion))
        cleanup.commit()
        cleanup.close()


# --------------------------------------------------------------------------------------
# ACCEPTANCE: staleness sweep
# --------------------------------------------------------------------------------------


def test_edit_marks_matches_stale_but_does_not_delete_them(db_session, provider):
    ingest(db_session, [_rec("resume.md", DOC)], provider=provider)
    chunk = live_chunks(db_session)[0]
    posting = make_posting(db_session, key="active-1")
    match = make_match(
        db_session, posting, evidence_version=current_version(db_session), chunk_id=chunk.id
    )
    match_id = match.id
    assert match.is_stale is False

    edited = DOC.replace("Designed the schema.", "Designed the pgvector schema.")
    stats = ingest(db_session, [_rec("resume.md", edited)], provider=provider)
    assert stats.updated == 1

    reloaded = db_session.get(Match, match_id)
    assert reloaded is not None, "the match row must survive a corpus edit"
    assert reloaded.is_stale is True
    assert reloaded.verdict is not None
    assert reloaded.score is not None
    assert reloaded.rationale == {"summary": "solid overlap on Redis and Postgres"}

    citations = list(
        db_session.execute(
            select(MatchCitation).where(MatchCitation.match_id == match_id)
        ).scalars()
    )
    assert len(citations) == 1, "citations survive too"
    assert citations[0].evidence_chunk_id == chunk.id
    assert citations[0].reasoning == "cited from the resume bullet"


def test_matches_for_inactive_postings_are_left_alone(db_session, provider):
    ingest(db_session, [_rec("resume.md", DOC)], provider=provider)
    active = make_posting(db_session, key="active-2")
    dead = make_posting(db_session, key="dead-1", inactive=True)
    version = current_version(db_session)
    active_match = make_match(db_session, active, evidence_version=version)
    dead_match = make_match(db_session, dead, evidence_version=version)

    bump_version(db_session, "manual")

    assert db_session.get(Match, active_match.id).is_stale is True
    assert db_session.get(Match, dead_match.id).is_stale is False


def test_sweep_reports_how_many_rows_it_touched(db_session):
    posting = make_posting(db_session, key="active-3")
    make_match(db_session, posting, evidence_version=1)
    assert mark_active_matches_stale(db_session) == 1
    # already stale rows are not counted twice
    assert mark_active_matches_stale(db_session) == 0


def test_unchanged_reingest_leaves_matches_fresh(db_session, provider):
    records = [_rec("resume.md", DOC)]
    ingest(db_session, records, provider=provider)
    posting = make_posting(db_session, key="active-4")
    match = make_match(db_session, posting, evidence_version=current_version(db_session))

    ingest(db_session, records, provider=provider)

    assert db_session.get(Match, match.id).is_stale is False


def test_soft_delete_also_marks_matches_stale(db_session, provider):
    ingest(db_session, [_rec("a.md", DOC), _rec("b.md", "# B\n\nAnother note.\n")], provider=provider)
    posting = make_posting(db_session, key="active-5")
    match = make_match(db_session, posting, evidence_version=current_version(db_session))

    ingest(db_session, [_rec("a.md", DOC)], provider=provider)

    assert db_session.get(Match, match.id).is_stale is True


# --------------------------------------------------------------------------------------
# manual tags
# --------------------------------------------------------------------------------------


def test_tagging_a_chunk_bumps_the_version(db_session, provider):
    ingest(db_session, [_rec("resume.md", DOC)], provider=provider)
    chunk = live_chunks(db_session)[0]
    make_skill(db_session, "Redis")
    make_skill(db_session, "PostgreSQL")
    version = current_version(db_session)

    added = tag_chunk(db_session, chunk.id, ["Redis", "PostgreSQL"])

    assert sorted(added) == ["PostgreSQL", "Redis"]
    assert current_version(db_session) == version + 1
    assert chunk_skills(db_session, chunk.id) == ["PostgreSQL", "Redis"]


def test_tagging_marks_matches_stale(db_session, provider):
    ingest(db_session, [_rec("resume.md", DOC)], provider=provider)
    chunk = live_chunks(db_session)[0]
    make_skill(db_session, "Redis")
    posting = make_posting(db_session, key="active-6")
    match = make_match(db_session, posting, evidence_version=current_version(db_session))

    tag_chunk(db_session, chunk.id, ["Redis"])

    assert db_session.get(Match, match.id).is_stale is True


def test_retagging_an_existing_tag_is_a_noop(db_session, provider):
    ingest(db_session, [_rec("resume.md", DOC)], provider=provider)
    chunk = live_chunks(db_session)[0]
    make_skill(db_session, "Redis")
    tag_chunk(db_session, chunk.id, ["Redis"])
    version = current_version(db_session)

    assert tag_chunk(db_session, chunk.id, ["Redis"]) == []
    assert current_version(db_session) == version
    assert (
        db_session.execute(
            select(func.count()).select_from(EvidenceSkill).where(
                EvidenceSkill.evidence_chunk_id == chunk.id
            )
        ).scalar_one()
        == 1
    )


def test_tag_lookup_is_case_insensitive(db_session, provider):
    ingest(db_session, [_rec("resume.md", DOC)], provider=provider)
    chunk = live_chunks(db_session)[0]
    make_skill(db_session, "PostgreSQL")
    assert tag_chunk(db_session, chunk.id, ["postgresql"]) == ["PostgreSQL"]


def test_untag_removes_and_bumps(db_session, provider):
    ingest(db_session, [_rec("resume.md", DOC)], provider=provider)
    chunk = live_chunks(db_session)[0]
    make_skill(db_session, "Redis")
    tag_chunk(db_session, chunk.id, ["Redis"])
    version = current_version(db_session)

    assert untag_chunk(db_session, chunk.id, ["Redis"]) == ["Redis"]
    assert current_version(db_session) == version + 1
    assert chunk_skills(db_session, chunk.id) == []
    # removing something that is not there changes nothing
    assert untag_chunk(db_session, chunk.id, ["Redis"]) == []
    assert current_version(db_session) == version + 1


def test_tagging_before_the_taxonomy_is_seeded_fails_with_guidance(db_session, provider):
    ingest(db_session, [_rec("resume.md", DOC)], provider=provider)
    chunk = live_chunks(db_session)[0]
    with pytest.raises(TaxonomyEmptyError, match="jme taxonomy seed"):
        tag_chunk(db_session, chunk.id, ["Redis"])


def test_unknown_skill_is_rejected_and_never_created(db_session, provider):
    ingest(db_session, [_rec("resume.md", DOC)], provider=provider)
    chunk = live_chunks(db_session)[0]
    make_skill(db_session, "Redis")
    version = current_version(db_session)

    with pytest.raises(UnknownSkillError, match="Kubernetes"):
        tag_chunk(db_session, chunk.id, ["Redis", "Kubernetes"])

    assert current_version(db_session) == version
    assert chunk_skills(db_session, chunk.id) == []


def test_tagging_a_missing_chunk_is_an_error(db_session):
    with pytest.raises(ValueError, match="no evidence chunk"):
        tag_chunk(db_session, 10_000_000, ["Redis"])


def test_skills_by_chunk_batches_the_lookup(db_session, provider):
    ingest(db_session, [_rec("a.md", DOC), _rec("b.md", "# B\n\nAnother note.\n")], provider=provider)
    chunks = live_chunks(db_session)
    make_skill(db_session, "Redis")
    make_skill(db_session, "Go")
    tag_chunk(db_session, chunks[0].id, ["Redis"])
    tag_chunk(db_session, chunks[1].id, ["Go"])

    tags = skills_by_chunk(db_session, [c.id for c in chunks])
    assert tags == {chunks[0].id: ["Redis"], chunks[1].id: ["Go"]}
    assert skills_by_chunk(db_session, []) == {}
