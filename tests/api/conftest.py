"""API fixtures.

A small, self-contained seed - deliberately not shared with `tests/report`, which owns a
much larger fixture tuned for ranking assertions. The API tests care about *shape*, and a
fixture you can hold in your head makes a shape assertion readable.

The session dependency is overridden with the transactional `db_session`, so every
request in a test runs inside the same rolled-back transaction.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

from jme.api.app import create_app, get_session
from jme.models import (
    CanonicalSkill,
    CitationStatus,
    EvidenceChunk,
    EvidenceVersion,
    FetchStatus,
    Importance,
    Match,
    MatchCitation,
    Posting,
    PostingJD,
    PostingRequirement,
    RunMetric,
    ShortlistEntry,
    SkillCategory,
    Verdict,
)

EVIDENCE_VERSION = 5
LATEST_RUN = "rank-0002"
OLDER_RUN = "rank-0001"


@dataclass
class ApiSeed:
    with_jd_id: int
    without_jd_id: int
    inactive_id: int
    missing_id: int
    skill_ids: dict[str, int]


def _posting(session, idx: int, *, now, company: str, inactive: bool = False) -> Posting:
    posting = Posting(
        canonical_key=f"api-key-{idx}",
        company=company,
        title=f"Backend Engineer {idx}",
        url=f"https://jobs.lever.co/acme/{idx}",
        url_host="jobs.lever.co",
        locations=["Chicago, IL"],
        sponsorship="Offers sponsorship",
        role_type="swe",
        is_remote=False,
        posted_at=now - dt.timedelta(days=idx),
        first_seen_at=now - dt.timedelta(days=idx),
        last_seen_at=now,
        inactive_at=now if inactive else None,
    )
    session.add(posting)
    return posting


@pytest.fixture
def seed(db_session) -> ApiSeed:
    now = dt.datetime.now(dt.UTC)
    db_session.add(EvidenceVersion(id=1, version=EVIDENCE_VERSION, reason="api seed"))

    skills = {
        "Kubernetes": CanonicalSkill(name="Kubernetes", category=SkillCategory.infra),
        "Python": CanonicalSkill(name="Python", category=SkillCategory.language),
        "Excellent Communication": CanonicalSkill(
            name="Excellent Communication", category=SkillCategory.soft, is_actionable=False
        ),
    }
    for skill in skills.values():
        db_session.add(skill)

    with_jd = _posting(db_session, 1, now=now, company="Acme Corp")
    without_jd = _posting(db_session, 2, now=now, company="Globex")
    inactive = _posting(db_session, 3, now=now, company="Initech", inactive=True)
    db_session.flush()

    db_session.add(
        PostingJD(
            posting_id=with_jd.id,
            adapter="greenhouse",
            raw_text="we need kubernetes and python",
            text_sha256="sha-api-1",
            fetch_status=FetchStatus.ok,
            char_count=30,
            extracted_at=now,
        )
    )
    for name, importance in (
        ("Kubernetes", Importance.required),
        ("Python", Importance.preferred),
        ("Excellent Communication", Importance.required),
    ):
        db_session.add(
            PostingRequirement(
                posting_id=with_jd.id,
                canonical_skill_id=skills[name].id,
                raw_text=f"{name} experience",
                importance=importance,
                confidence=0.8,
                prompt_version="v1",
            )
        )
    # one requirement the taxonomy could not resolve
    db_session.add(
        PostingRequirement(
            posting_id=with_jd.id,
            canonical_skill_id=None,
            raw_text="ability to thrive in ambiguity",
            importance=Importance.mentioned,
            confidence=0.4,
            prompt_version="v1",
        )
    )

    chunk = EvidenceChunk(
        source_type="markdown",
        source_ref="resume.md",
        ordinal=0,
        text="built a python service",
        text_sha256="chunk-api-1",
        evidence_version=EVIDENCE_VERSION,
    )
    db_session.add(chunk)
    db_session.flush()

    # two matches on the same posting; the API must return the newer one
    db_session.add(
        Match(
            posting_id=with_jd.id,
            evidence_version=EVIDENCE_VERSION - 1,
            prompt_version="v0",
            model_id="claude-opus-5",
            jd_sha256="sha-old",
            score=0.1,
            verdict=Verdict.no,
            is_stale=True,
            computed_at=now - dt.timedelta(days=3),
        )
    )
    latest = Match(
        posting_id=with_jd.id,
        evidence_version=EVIDENCE_VERSION,
        prompt_version="v1",
        model_id="claude-opus-5",
        jd_sha256="sha-new",
        score=0.82,
        verdict=Verdict.plausible,
        rationale={"summary": "close"},
        is_stale=False,
        computed_at=now,
    )
    db_session.add(latest)
    db_session.flush()
    db_session.add_all(
        [
            MatchCitation(
                match_id=latest.id,
                canonical_skill_id=skills["Python"].id,
                evidence_chunk_id=chunk.id,
                status=CitationStatus.evidenced,
                reasoning="shipped it",
            ),
            MatchCitation(
                match_id=latest.id,
                canonical_skill_id=skills["Kubernetes"].id,
                status=CitationStatus.absent,
                reasoning="nothing found",
            ),
        ]
    )

    db_session.add_all(
        [
            ShortlistEntry(run_id=OLDER_RUN, posting_id=with_jd.id, rank=1, coarse_score=0.4,
                           evidence_version=EVIDENCE_VERSION,
                           created_at=now - dt.timedelta(days=2)),
            ShortlistEntry(run_id=LATEST_RUN, posting_id=with_jd.id, rank=1, coarse_score=0.9,
                           evidence_version=EVIDENCE_VERSION, created_at=now),
            ShortlistEntry(run_id=LATEST_RUN, posting_id=without_jd.id, rank=2, coarse_score=0.7,
                           evidence_version=EVIDENCE_VERSION, created_at=now),
        ]
    )
    db_session.add_all(
        [
            RunMetric(run_id=LATEST_RUN, stage="rank", metric="postings_after_filters", value=2),
            RunMetric(run_id=LATEST_RUN, stage="fetch", metric="adapter_success_rate",
                      value=0.5, labels={"adapter": "greenhouse"}),
        ]
    )
    db_session.flush()

    return ApiSeed(
        with_jd_id=with_jd.id,
        without_jd_id=without_jd.id,
        inactive_id=inactive.id,
        missing_id=with_jd.id + 10_000,
        skill_ids={name: skill.id for name, skill in skills.items()},
    )


@pytest.fixture
def client(db_session) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_session] = lambda: db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
