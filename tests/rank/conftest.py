"""Fixtures for the filter/rank tests.

Everything here builds postings and evidence explicitly, one field at a time, so a
failing test names the exact rule it is about. The embedding provider is the `hash`
one -- deterministic, offline, no API key -- which is what makes assertions on
similarity ordering meaningful in CI.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import itertools

import pytest
from sqlalchemy.orm import Session

from jme.config import Settings
from jme.embeddings import HashEmbeddingProvider
from jme.models import EvidenceChunk, Importance, Posting, PostingRequirement
from tests.rank.factories import make_settings

_ids = itertools.count(1)

#: Distinguishes "the test did not care about this field" from "the test explicitly
#: wants NULL here", which matters for every filter whose whole point is NULL handling.
DEFAULT = object()


@pytest.fixture
def provider() -> HashEmbeddingProvider:
    return HashEmbeddingProvider()


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def make_posting(db_session: Session):
    """Insert a posting. Defaults are deliberately eligible so each test varies one field."""

    def _make(
        *,
        company: str = "Acme",
        title: str = "Software Engineer, New Grad",
        locations: list[str] | None | object = DEFAULT,
        sponsorship: str | None = "Offers Sponsorship",
        role_type: str | None = "swe",
        start_season: str | None = "Summer 2027",
        is_remote: bool = False,
        inactive_at: dt.datetime | None = None,
        posted_at: dt.datetime | None = None,
    ) -> Posting:
        n = next(_ids)
        posting = Posting(
            canonical_key=f"test-{n}-{hashlib.md5(title.encode()).hexdigest()[:8]}",
            company=company,
            title=title,
            url=f"https://example.test/jobs/{n}",
            url_host="example.test",
            locations=["Detroit, MI"] if locations is DEFAULT else locations,
            sponsorship=sponsorship,
            role_type=role_type,
            start_season=start_season,
            is_remote=is_remote,
            inactive_at=inactive_at,
            posted_at=posted_at or dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        )
        db_session.add(posting)
        db_session.flush()
        return posting

    return _make


@pytest.fixture
def add_requirement(db_session: Session):
    def _add(
        posting: Posting,
        raw_text: str,
        importance: Importance = Importance.required,
        confidence: float = 0.9,
    ) -> PostingRequirement:
        requirement = PostingRequirement(
            posting_id=posting.id,
            raw_text=raw_text,
            importance=importance,
            confidence=confidence,
            prompt_version="v1",
        )
        db_session.add(requirement)
        db_session.flush()
        return requirement

    return _add


@pytest.fixture
def add_chunk(db_session: Session, provider: HashEmbeddingProvider):
    counter = itertools.count(1)

    def _add(body: str, *, deleted: bool = False) -> EvidenceChunk:
        ordinal = next(counter)
        chunk = EvidenceChunk(
            source_type="test",
            source_ref="tests/evidence.md",
            heading="Evidence",
            ordinal=ordinal,
            text=body,
            text_sha256=hashlib.sha256(body.encode()).hexdigest(),
            token_estimate=len(body) // 4,
            embedding=provider.embed_one(body),
            embedding_model=provider.name,
            evidence_version=1,
            deleted_at=dt.datetime.now(dt.UTC) if deleted else None,
        )
        db_session.add(chunk)
        db_session.flush()
        return chunk

    return _add
