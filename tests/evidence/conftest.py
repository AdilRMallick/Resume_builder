"""Fixtures for the evidence suite.

Everything here runs on the deterministic `hash` embedding provider, so the whole
suite is offline and repeatable. The counting wrapper is what lets us assert the
expensive thing (embedding) did not happen.
"""

from __future__ import annotations

import datetime as dt

import pytest

from jme.embeddings import EmbeddingProvider, HashEmbeddingProvider
from jme.models import (
    CanonicalSkill,
    CitationStatus,
    Match,
    MatchCitation,
    Posting,
    SkillCategory,
    Verdict,
)


class CountingProvider(EmbeddingProvider):
    """Real hash embeddings, plus a record of exactly what was embedded."""

    name = "hash-v1"

    def __init__(self) -> None:
        self._inner = HashEmbeddingProvider()
        self.dim = self._inner.dim
        self.calls = 0
        self.texts: list[str] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        self.texts.extend(texts)
        return self._inner.embed(texts)


@pytest.fixture
def provider() -> CountingProvider:
    return CountingProvider()


@pytest.fixture
def evidence_dir(tmp_path):
    """A small corpus on disk. Returns the root; write more files to change it."""
    (tmp_path / "projects").mkdir()
    (tmp_path / "resume.md").write_text(
        "# Resume\n\n"
        "## Experience\n\n"
        "- Built a Redis Streams pipeline with consumer groups and XAUTOCLAIM recovery.\n"
        "- Designed the Postgres schema, including the pgvector index on the corpus.\n",
        encoding="utf-8",
    )
    (tmp_path / "projects" / "redis-pipeline.md").write_text(
        "# Redis pipeline\n\n"
        "A fetcher in Go that consumes a Redis stream with a per-host token bucket.\n",
        encoding="utf-8",
    )
    return tmp_path


def make_posting(session, *, key: str = "p1", inactive: bool = False) -> Posting:
    now = dt.datetime.now(dt.UTC)
    posting = Posting(
        canonical_key=key,
        company="Acme",
        title="New Grad SWE",
        url=f"https://boards.example.com/{key}",
        url_host="boards.example.com",
        locations=["Detroit, MI"],
        first_seen_at=now,
        last_seen_at=now,
        inactive_at=now if inactive else None,
    )
    session.add(posting)
    session.flush()
    return posting


def make_match(session, posting: Posting, *, evidence_version: int, chunk_id: int | None = None):
    match = Match(
        posting_id=posting.id,
        evidence_version=evidence_version,
        prompt_version="v1",
        model_id="claude-opus-5",
        jd_sha256="a" * 64,
        score=0.82,
        verdict=Verdict.plausible,
        rationale={"summary": "solid overlap on Redis and Postgres"},
        is_stale=False,
    )
    session.add(match)
    session.flush()
    session.add(
        MatchCitation(
            match_id=match.id,
            canonical_skill_id=None,
            evidence_chunk_id=chunk_id,
            status=CitationStatus.evidenced if chunk_id else CitationStatus.absent,
            reasoning="cited from the resume bullet",
        )
    )
    session.flush()
    return match


def make_skill(session, name: str, category: SkillCategory = SkillCategory.database):
    skill = CanonicalSkill(name=name, category=category, is_actionable=True)
    session.add(skill)
    session.flush()
    return skill
