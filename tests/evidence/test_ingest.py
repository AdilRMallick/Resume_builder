"""Ingestion against a real Postgres: upsert, hash-gated re-embedding, soft delete."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from jme.evidence.ingest import ingest, ingest_from_config, live_chunks, reembed, sha256
from jme.evidence.sources import SOURCE_MARKDOWN, SOURCE_REPO_README, SourceRecord
from jme.evidence.version import current_version
from jme.models import EvidenceChunk

pytestmark = pytest.mark.integration


def _rec(ref: str, text: str, source_type: str = SOURCE_MARKDOWN) -> SourceRecord:
    return SourceRecord(source_type, ref, text)


def _all_rows(session, ref: str | None = None) -> list[EvidenceChunk]:
    stmt = select(EvidenceChunk).order_by(EvidenceChunk.source_ref, EvidenceChunk.ordinal)
    if ref:
        stmt = stmt.where(EvidenceChunk.source_ref == ref)
    return list(session.execute(stmt).scalars())


DOC = "# Resume\n\n## Experience\n\n- Built a Redis Streams pipeline.\n- Designed the schema.\n"


# --------------------------------------------------------------------------------------
# first run
# --------------------------------------------------------------------------------------


def test_first_ingest_inserts_embeds_and_bumps(db_session, provider):
    before = current_version(db_session)
    stats = ingest(db_session, [_rec("resume.md", DOC)], provider=provider)

    assert stats.added == 1
    assert stats.unchanged == 0
    assert stats.version_after == before + 1
    assert stats.embedded == 1

    rows = _all_rows(db_session)
    assert len(rows) == 1
    row = rows[0]
    assert row.source_type == SOURCE_MARKDOWN
    assert row.source_ref == "resume.md"
    assert row.ordinal == 0
    assert row.heading == "Experience"
    assert row.text_sha256 == sha256(row.text)
    assert row.embedding is not None
    assert row.embedding_model == "hash-v1"
    assert row.evidence_version == stats.version_after
    assert row.token_estimate > 0


def test_ingest_from_a_directory(db_session, provider, evidence_dir):
    stats = ingest_from_config(db_session, directory=evidence_dir, repos=[], provider=provider)
    assert stats.sources == 2
    refs = {row.source_ref for row in _all_rows(db_session)}
    assert refs == {"resume.md", "projects/redis-pipeline.md"}


# --------------------------------------------------------------------------------------
# ACCEPTANCE: idempotence
# --------------------------------------------------------------------------------------


def test_reingest_unchanged_does_not_bump_or_reembed(db_session, provider):
    records = [_rec("resume.md", DOC), _rec("octo/repo", "# Repo\n\nA Go fetcher.\n", SOURCE_REPO_README)]
    first = ingest(db_session, records, provider=provider)
    assert first.changed
    calls_after_first = provider.calls
    version_after_first = current_version(db_session)

    second = ingest(db_session, records, provider=provider)

    assert second.added == 0
    assert second.updated == 0
    assert second.deleted == 0
    assert second.revived == 0
    assert second.unchanged == first.added
    assert second.changed is False
    assert second.embedded == 0
    assert provider.calls == calls_after_first, "unchanged input must not re-embed"
    assert current_version(db_session) == version_after_first
    assert second.version_after == version_after_first


def test_third_run_is_also_a_noop(db_session, provider):
    records = [_rec("resume.md", DOC)]
    ingest(db_session, records, provider=provider)
    ingest(db_session, records, provider=provider)
    version = current_version(db_session)
    stats = ingest(db_session, records, provider=provider)
    assert stats.changed is False
    assert current_version(db_session) == version


def test_dry_run_writes_nothing(db_session, provider):
    records = [_rec("resume.md", DOC)]
    before = current_version(db_session)
    stats = ingest(db_session, records, provider=provider, dry_run=True)

    assert stats.added == 1
    assert stats.changed is True
    assert stats.version_after == before + 1, "dry run reports the bump it would make"
    assert stats.embedded == 0
    assert provider.calls == 0
    assert _all_rows(db_session) == []
    assert current_version(db_session) == before


def test_dry_run_on_an_unchanged_corpus_reports_no_bump(db_session, provider):
    records = [_rec("resume.md", DOC)]
    ingest(db_session, records, provider=provider)
    version = current_version(db_session)
    stats = ingest(db_session, records, provider=provider, dry_run=True)
    assert stats.changed is False
    assert stats.version_after == version


# --------------------------------------------------------------------------------------
# edits
# --------------------------------------------------------------------------------------


def test_edited_chunk_is_updated_in_place_and_reembedded(db_session, provider):
    ingest(db_session, [_rec("resume.md", DOC)], provider=provider)
    row_id = _all_rows(db_session)[0].id
    old_vector = list(_all_rows(db_session)[0].embedding)
    version = current_version(db_session)

    edited = DOC.replace("Designed the schema.", "Designed the pgvector schema and HNSW index.")
    stats = ingest(db_session, [_rec("resume.md", edited)], provider=provider)

    assert (stats.added, stats.updated, stats.deleted) == (0, 1, 0)
    rows = _all_rows(db_session)
    assert len(rows) == 1, "an edit updates the row, it does not create a second one"
    assert rows[0].id == row_id, "chunk identity survives an edit, so citations stay valid"
    assert "HNSW" in rows[0].text
    assert rows[0].text_sha256 == sha256(rows[0].text)
    assert list(rows[0].embedding) != old_vector
    assert current_version(db_session) == version + 1
    assert rows[0].evidence_version == version + 1


def test_only_the_edited_chunk_is_reembedded(db_session, provider):
    doc = "".join(f"## S{i}\n\n{'word ' * 900}\n\n" for i in range(4))
    ingest(db_session, [_rec("big.md", doc)], provider=provider)
    total = len(_all_rows(db_session))
    assert total >= 4
    provider.texts.clear()

    edited = doc.replace("## S2", "## S2 revised")
    stats = ingest(db_session, [_rec("big.md", edited)], provider=provider)

    assert stats.updated < total, "a one-section edit must not rewrite the whole corpus"
    assert stats.unchanged > 0
    assert all("S2 revised" in text for text in provider.texts)


# --------------------------------------------------------------------------------------
# ACCEPTANCE: soft delete
# --------------------------------------------------------------------------------------


def test_vanished_source_is_soft_deleted_not_removed(db_session, provider):
    records = [_rec("resume.md", DOC), _rec("notes.md", "# Notes\n\nSome evidence note.\n")]
    ingest(db_session, records, provider=provider)
    assert len(live_chunks(db_session)) == 2

    stats = ingest(db_session, [_rec("resume.md", DOC)], provider=provider)
    assert stats.deleted == 1

    live = live_chunks(db_session)
    assert [c.source_ref for c in live] == ["resume.md"]

    # still in the table, so match_citation rows pointing at it remain readable
    all_rows = _all_rows(db_session)
    assert len(all_rows) == 2
    tombstoned = [r for r in all_rows if r.source_ref == "notes.md"]
    assert len(tombstoned) == 1
    assert tombstoned[0].deleted_at is not None
    assert tombstoned[0].text  # text preserved


def test_live_chunk_query_excludes_soft_deleted_rows(db_session, provider):
    ingest(db_session, [_rec("gone.md", "# Gone\n\nEvidence that will be removed.\n")], provider=provider)
    chunk_id = live_chunks(db_session)[0].id

    ingest(db_session, [_rec("kept.md", "# Kept\n\nEvidence that stays.\n")], provider=provider)

    assert chunk_id not in {c.id for c in live_chunks(db_session)}
    assert db_session.get(EvidenceChunk, chunk_id) is not None
    assert db_session.get(EvidenceChunk, chunk_id).deleted_at is not None


def test_shortened_document_soft_deletes_the_trailing_ordinals(db_session, provider):
    long_doc = "".join(f"## S{i}\n\n{'word ' * 900}\n\n" for i in range(4))
    ingest(db_session, [_rec("doc.md", long_doc)], provider=provider)
    before = len(live_chunks(db_session))

    short_doc = "".join(f"## S{i}\n\n{'word ' * 900}\n\n" for i in range(2))
    stats = ingest(db_session, [_rec("doc.md", short_doc)], provider=provider)

    assert stats.deleted > 0
    assert len(live_chunks(db_session)) < before
    assert len(_all_rows(db_session)) == before, "nothing is hard deleted"


def test_soft_deleted_chunk_is_revived_when_its_text_returns(db_session, provider):
    records = [_rec("resume.md", DOC), _rec("notes.md", "# Notes\n\nSome evidence note.\n")]
    ingest(db_session, records, provider=provider)
    note_id = [c.id for c in live_chunks(db_session) if c.source_ref == "notes.md"][0]

    ingest(db_session, [_rec("resume.md", DOC)], provider=provider)
    assert db_session.get(EvidenceChunk, note_id).deleted_at is not None

    stats = ingest(db_session, records, provider=provider)

    assert stats.revived == 1
    assert stats.added == 0, "revival reuses the row, so old citations become valid again"
    revived = db_session.get(EvidenceChunk, note_id)
    assert revived.deleted_at is None
    assert note_id in {c.id for c in live_chunks(db_session)}


def test_deletion_is_scoped_to_source_types_that_were_scanned(db_session, provider):
    ingest(
        db_session,
        [
            _rec("resume.md", DOC),
            _rec("octo/repo", "# Repo\n\nA Go fetcher with a token bucket.\n", SOURCE_REPO_README),
        ],
        provider=provider,
    )
    assert len(live_chunks(db_session)) == 2

    # a markdown-only run must not tombstone repo READMEs it never looked at
    stats = ingest(
        db_session,
        [_rec("resume.md", DOC)],
        scanned_types={SOURCE_MARKDOWN},
        provider=provider,
    )
    assert stats.deleted == 0
    assert len(live_chunks(db_session)) == 2


def test_an_empty_source_type_does_not_wipe_the_corpus(db_session, provider):
    ingest(db_session, [_rec("resume.md", DOC)], provider=provider)
    version = current_version(db_session)

    # a mistyped --dir yields zero records: refuse to tombstone everything
    stats = ingest(db_session, [], scanned_types={SOURCE_MARKDOWN}, provider=provider)

    assert stats.deleted == 0
    assert stats.changed is False
    assert len(live_chunks(db_session)) == 1
    assert current_version(db_session) == version


# --------------------------------------------------------------------------------------
# re-embedding
# --------------------------------------------------------------------------------------


def test_reembed_fills_missing_vectors_without_bumping(db_session, provider):
    ingest(db_session, [_rec("resume.md", DOC)], provider=provider)
    row = live_chunks(db_session)[0]
    row.embedding = None
    db_session.flush()
    version = current_version(db_session)

    count = reembed(db_session, provider=provider)

    assert count == 1
    assert live_chunks(db_session)[0].embedding is not None
    assert current_version(db_session) == version, "a backfill does not change what the corpus says"


def test_reembed_skips_chunks_that_already_have_a_current_vector(db_session, provider):
    ingest(db_session, [_rec("resume.md", DOC)], provider=provider)
    assert reembed(db_session, provider=provider) == 0


def test_reembed_force_rewrites_everything_and_bumps(db_session, provider):
    ingest(db_session, [_rec("resume.md", DOC)], provider=provider)
    version = current_version(db_session)

    count = reembed(db_session, force=True, provider=provider)

    assert count == 1
    assert current_version(db_session) == version + 1


def test_reembed_picks_up_chunks_embedded_by_another_model(db_session, provider):
    ingest(db_session, [_rec("resume.md", DOC)], provider=provider)
    row = live_chunks(db_session)[0]
    row.embedding_model = "voyage:voyage-3"
    db_session.flush()

    assert reembed(db_session, provider=provider) == 1
    assert live_chunks(db_session)[0].embedding_model == "hash-v1"
