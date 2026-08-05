"""Per-requirement retrieval against a real Postgres with pgvector."""

from __future__ import annotations

import datetime as dt

import pytest

from jme.matcher.retrieval import (
    DEFAULT_TOP_K,
    default_top_k,
    load_requirements,
    retrieve_for_posting,
    retrieve_for_requirement,
)
from jme.models import Importance
from tests.matcher.conftest import tag_chunk

pytestmark = pytest.mark.integration


def test_default_top_k_is_three_and_env_configurable(monkeypatch):
    monkeypatch.delenv("JME_MATCH_TOP_K", raising=False)
    assert default_top_k() == DEFAULT_TOP_K == 3
    monkeypatch.setenv("JME_MATCH_TOP_K", "7")
    assert default_top_k() == 7
    monkeypatch.setenv("JME_MATCH_TOP_K", "not-a-number")
    assert default_top_k() == 3


def test_requirements_are_ordered_by_importance(db_session, seed):
    reqs = load_requirements(db_session, seed.posting_id)
    assert [r.importance for r in reqs] == [
        Importance.required,
        Importance.required,
        Importance.preferred,
    ]
    assert [r.skill_name for r in reqs] == ["Go", "PostgreSQL", "Kubernetes"]


def test_top_k_per_requirement_and_dedup(db_session, seed):
    result = retrieve_for_posting(db_session, seed.posting_id, top_k=2)

    assert result.top_k == 2
    assert result.evidence_version == 1
    for req in seed.requirements:
        assert len(result.per_requirement[req.id]) == 2

    # candidate set is the deduplicated union, so it is smaller than 3 requirements x 2
    assert len(result.chunks) <= 6
    assert len(result.chunks) == len({c.chunk_id for c in result.chunks})
    assert result.valid_chunk_ids <= set(seed.chunk_ids)


def test_similarity_puts_the_obvious_chunk_first(db_session, seed):
    """The hash provider is lexical, so a Postgres requirement should pull the Postgres chunk."""
    result = retrieve_for_posting(db_session, seed.posting_id, top_k=1)
    postgres_req = next(r for r in result.requirements if r.skill_name == "PostgreSQL")
    top = result.per_requirement[postgres_req.requirement_id][0]
    assert top == seed.chunks[1].id


def test_deleted_chunks_are_never_retrieved(db_session, seed):
    for chunk in seed.chunks[1:]:
        chunk.deleted_at = dt.datetime.now(dt.UTC)
    db_session.flush()

    result = retrieve_for_posting(db_session, seed.posting_id, top_k=3)
    assert result.valid_chunk_ids == {seed.chunks[0].id}


def test_manual_tag_boost_promotes_a_tagged_chunk(db_session, seed):
    """A manual tag is allowed to outrank raw similarity: tag the *worst* chunk and watch
    it come first. Written against the observed ranking rather than a guess about the
    embedding, so it tests the boost and nothing else."""
    k8s = next(
        r for r in load_requirements(db_session, seed.posting_id) if r.skill_name == "Kubernetes"
    )
    ranked = retrieve_for_requirement(db_session, k8s, top_k=len(seed.chunks), tag_boost=0.0)
    assert all(chunk.tagged is False for chunk in ranked)
    worst = ranked[-1]
    assert worst.chunk_id != ranked[0].chunk_id

    tagged_chunk = next(c for c in seed.chunks if c.id == worst.chunk_id)
    tag_chunk(db_session, tagged_chunk, seed.skills["Kubernetes"])

    unboosted = retrieve_for_requirement(db_session, k8s, top_k=1, tag_boost=0.0)
    assert unboosted[0].chunk_id == ranked[0].chunk_id

    boosted = retrieve_for_requirement(db_session, k8s, top_k=1, tag_boost=2.0)
    assert boosted[0].chunk_id == worst.chunk_id
    assert boosted[0].tagged is True
    assert boosted[0].effective_score < boosted[0].distance


def test_chunks_without_an_embedding_are_skipped(db_session, seed):
    seed.chunks[0].embedding = None
    db_session.flush()
    result = retrieve_for_posting(db_session, seed.posting_id, top_k=4)
    assert seed.chunks[0].id not in result.valid_chunk_ids
