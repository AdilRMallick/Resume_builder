"""A deliberately hand-computed fixture for the gap report.

The point of this file is that every number the report can produce is knowable by
reading it. Nothing is random, no skill ties on `required_count`, and each interesting
behaviour of the rollup has exactly one skill dedicated to demonstrating it:

  skill          actionable  demand                       evidence            expectation
  -------------  ----------  ---------------------------  ------------------  --------------------------
  Kubernetes     yes         required x12                 only a STALE match  gap rank 1 (stale ignored)
  Go             yes         required x8                  weak citation       gap rank 2
  Terraform      yes         required x6                  absent citation     gap rank 3
  Kafka          yes         required x4 + preferred x2    none                gap rank 4
  GraphQL        yes         required x2 + preferred x5    none                gap rank 5
  Docker         yes         mentioned x12 (required x0)   none                gap, but below all of the
                                                                               above despite tying
                                                                               Kubernetes on posting_count
  Python         yes         required x20                 evidenced           covered, never a gap
  SQL            yes         required x10                 absent THEN         covered - "best ever" rule
                                                          evidenced
  Communication  NO          required x24                 none                absent from every list

Plus three postings that must not contribute at all: one inactive, one whose sponsorship
line disqualifies it, and one with the wrong role_type.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import pytest

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
    SkillAlias,
    SkillCategory,
    Verdict,
)

PROMPT_VERSION = "v1"
MODEL_ID = "claude-opus-5"
EVIDENCE_VERSION = 3

#: eligible active postings, ids 1..N in insertion order
ELIGIBLE_POSTINGS = 24
#: postings 1..12 were first seen recently, 13..24 in the prior half of the window
RECENT_CUTOFF = 12
RECENT_DAYS_AGO = 3
PRIOR_DAYS_AGO = 20

UNMAPPED_REQUIREMENTS = 7

# jd resolution, for the adapter-coverage assertions
GREENHOUSE_RESOLVED = 12  # postings 1..12
LEVER_RESOLVED = 6  # postings 13..18
FALLBACK_FAILED = 3  # postings 19..21, fetched but never resolved to text
NO_JD_ROW = ELIGIBLE_POSTINGS - GREENHOUSE_RESOLVED - LEVER_RESOLVED - FALLBACK_FAILED


@dataclass
class Seeded:
    """Everything a test needs to assert against, computed here rather than in the test."""

    postings: list[Posting]
    skills: dict[str, CanonicalSkill]
    chunks: list[EvidenceChunk]
    now: dt.datetime
    expected_gap_order: list[str] = field(default_factory=list)
    expected_covered: set[str] = field(default_factory=set)
    #: active postings including the ones excluded by the eligibility filters
    active_postings: int = 0

    def skill_id(self, name: str) -> int:
        return self.skills[name].id


def _posting(
    session,
    idx: int,
    *,
    now: dt.datetime,
    days_ago: int,
    company: str = "Acme",
    role_type: str = "swe",
    sponsorship: str | None = "Offers sponsorship",
    inactive: bool = False,
) -> Posting:
    first_seen = now - dt.timedelta(days=days_ago)
    posting = Posting(
        canonical_key=f"key-{idx:04d}",
        simplify_id=f"simplify-{idx:04d}",
        company=f"{company} {idx}",
        title=f"Software Engineer New Grad {idx}",
        url=f"https://boards.greenhouse.io/acme/jobs/{idx}",
        url_host="boards.greenhouse.io",
        locations=["Remote", "Chicago, IL"],
        sponsorship=sponsorship,
        role_type=role_type,
        is_remote=True,
        posted_at=first_seen,
        first_seen_at=first_seen,
        last_seen_at=now - dt.timedelta(days=1),
        inactive_at=(now - dt.timedelta(days=2)) if inactive else None,
        repost_count=0,
    )
    session.add(posting)
    return posting


def _require(session, posting: Posting, skill: CanonicalSkill | None, importance: Importance,
             label: str) -> None:
    session.add(
        PostingRequirement(
            posting_id=posting.id,
            canonical_skill_id=skill.id if skill else None,
            raw_text=f"{label} ({importance.value})",
            importance=importance,
            confidence=0.9,
            prompt_version=PROMPT_VERSION,
            model_id=MODEL_ID,
        )
    )


def _match(session, posting: Posting, *, stale: bool, jd_sha: str) -> Match:
    match = Match(
        posting_id=posting.id,
        evidence_version=EVIDENCE_VERSION,
        prompt_version=PROMPT_VERSION,
        model_id=MODEL_ID,
        jd_sha256=jd_sha,
        score=0.75,
        verdict=Verdict.plausible,
        rationale={"summary": "seeded"},
        is_stale=stale,
    )
    session.add(match)
    session.flush()
    return match


def seed_report_fixture(session) -> Seeded:
    now = dt.datetime.now(dt.UTC)

    session.add(EvidenceVersion(id=1, version=EVIDENCE_VERSION, reason="seeded"))

    specs = [
        ("Kubernetes", SkillCategory.infra, True),
        ("Go", SkillCategory.language, True),
        ("Terraform", SkillCategory.infra, True),
        ("Kafka", SkillCategory.infra, True),
        ("GraphQL", SkillCategory.framework, True),
        ("Docker", SkillCategory.infra, True),
        ("Python", SkillCategory.language, True),
        ("SQL", SkillCategory.language, True),
        # the whole reason `is_actionable` exists: a gap here is not something I can act on
        ("Excellent Communication", SkillCategory.soft, False),
    ]
    skills: dict[str, CanonicalSkill] = {}
    for name, category, actionable in specs:
        skill = CanonicalSkill(name=name, category=category, is_actionable=actionable)
        session.add(skill)
        skills[name] = skill
    session.flush()
    session.add(SkillAlias(canonical_skill_id=skills["Go"].id, alias="Golang", alias_norm="golang"))
    session.add(SkillAlias(canonical_skill_id=skills["SQL"].id, alias="Postgres",
                           alias_norm="postgres"))

    postings: list[Posting] = []
    for i in range(1, ELIGIBLE_POSTINGS + 1):
        days_ago = RECENT_DAYS_AGO if i <= RECENT_CUTOFF else PRIOR_DAYS_AGO
        postings.append(_posting(session, i, now=now, days_ago=days_ago))

    # must not contribute to the rollup
    excluded_inactive = _posting(session, 900, now=now, days_ago=5, inactive=True)
    excluded_sponsorship = _posting(
        session, 901, now=now, days_ago=5, sponsorship="Does not offer sponsorship"
    )
    excluded_role = _posting(session, 902, now=now, days_ago=5, role_type="quant")
    session.flush()

    def demand(name: str, importance: Importance, first: int, last: int) -> None:
        for p in postings[first - 1 : last]:
            _require(session, p, skills[name], importance, name)

    demand("Kubernetes", Importance.required, 1, 12)
    demand("Go", Importance.required, 1, 8)
    demand("Terraform", Importance.required, 1, 6)
    demand("Kafka", Importance.required, 1, 4)
    demand("Kafka", Importance.preferred, 5, 6)
    demand("GraphQL", Importance.required, 1, 2)
    demand("GraphQL", Importance.preferred, 3, 7)
    # mentioned-only, on exactly the same 12 postings as Kubernetes: identical
    # posting_count, zero required_count. it must never outrank Kubernetes.
    demand("Docker", Importance.mentioned, 1, 12)
    demand("Python", Importance.required, 1, 20)
    demand("SQL", Importance.required, 1, 10)
    demand("Excellent Communication", Importance.required, 1, ELIGIBLE_POSTINGS)

    # requirements the taxonomy could not resolve: invisible to the ranking, and the
    # honest measure of how much the report is missing
    for p in postings[:UNMAPPED_REQUIREMENTS]:
        _require(session, p, None, Importance.required, "5+ years of unicorn wrangling")

    # demand on excluded postings, to prove the eligibility filters actually bite
    for p in (excluded_inactive, excluded_sponsorship, excluded_role):
        _require(session, p, skills["Kubernetes"], Importance.required, "Kubernetes")

    # jd rows for the adapter coverage number
    for i, p in enumerate(postings, start=1):
        if i <= GREENHOUSE_RESOLVED:
            session.add(PostingJD(posting_id=p.id, adapter="greenhouse", raw_text="jd text",
                                  fetch_status=FetchStatus.ok, char_count=1200,
                                  extracted_at=now, text_sha256=f"sha{i}"))
        elif i <= GREENHOUSE_RESOLVED + LEVER_RESOLVED:
            session.add(PostingJD(posting_id=p.id, adapter="lever", raw_text="jd text",
                                  fetch_status=FetchStatus.ok, char_count=900,
                                  extracted_at=now, text_sha256=f"sha{i}"))
        elif i <= GREENHOUSE_RESOLVED + LEVER_RESOLVED + FALLBACK_FAILED:
            session.add(PostingJD(posting_id=p.id, adapter="fallback",
                                  fetch_status=FetchStatus.permanent_error, char_count=0,
                                  attempts=3, fetch_error="404"))

    chunks = [
        EvidenceChunk(source_type="markdown", source_ref="resume.md", ordinal=i,
                      text=f"chunk {i}", text_sha256=f"chunk-sha-{i}",
                      evidence_version=EVIDENCE_VERSION)
        for i in range(2)
    ]
    for c in chunks:
        session.add(c)
    session.flush()

    # ---- matches -------------------------------------------------------------------
    # Python: a live match with a real citation -> covered.
    m_python = _match(session, postings[0], stale=False, jd_sha="sha-python")
    session.add(MatchCitation(match_id=m_python.id, canonical_skill_id=skills["Python"].id,
                              evidence_chunk_id=chunks[0].id, status=CitationStatus.evidenced,
                              reasoning="ships python daily"))
    # SQL: absent in one match, evidenced in another. "best status ever" makes it covered.
    session.add(MatchCitation(match_id=m_python.id, canonical_skill_id=skills["SQL"].id,
                              status=CitationStatus.absent, reasoning="not surfaced here"))
    m_sql = _match(session, postings[1], stale=False, jd_sha="sha-sql")
    session.add(MatchCitation(match_id=m_sql.id, canonical_skill_id=skills["SQL"].id,
                              evidence_chunk_id=chunks[1].id, status=CitationStatus.evidenced,
                              reasoning="wrote the gap rollup"))
    # Go: weak is the best it ever gets -> stays on the gap list.
    session.add(MatchCitation(match_id=m_sql.id, canonical_skill_id=skills["Go"].id,
                              status=CitationStatus.weak, reasoning="one small PR"))
    m_go = _match(session, postings[2], stale=False, jd_sha="sha-go")
    session.add(MatchCitation(match_id=m_go.id, canonical_skill_id=skills["Go"].id,
                              status=CitationStatus.absent))
    # Terraform: looked at, nothing found.
    session.add(MatchCitation(match_id=m_go.id, canonical_skill_id=skills["Terraform"].id,
                              status=CitationStatus.absent))
    # Kubernetes: an evidenced citation that is STALE. It must be ignored entirely,
    # otherwise a corpus edit would silently close a gap that is still open.
    m_stale = _match(session, postings[3], stale=True, jd_sha="sha-stale")
    session.add(MatchCitation(match_id=m_stale.id, canonical_skill_id=skills["Kubernetes"].id,
                              evidence_chunk_id=chunks[0].id, status=CitationStatus.evidenced,
                              reasoning="stale, must not count"))

    # ---- ops rows used by the API and the metrics command ---------------------------
    for rank, p in enumerate(postings[:5], start=1):
        session.add(ShortlistEntry(run_id="rank-0002", posting_id=p.id, rank=rank,
                                   coarse_score=0.9 - rank / 100,
                                   evidence_version=EVIDENCE_VERSION,
                                   created_at=now - dt.timedelta(hours=1)))
    for rank, p in enumerate(postings[:3], start=1):
        session.add(ShortlistEntry(run_id="rank-0001", posting_id=p.id, rank=rank,
                                   coarse_score=0.5,
                                   evidence_version=EVIDENCE_VERSION,
                                   created_at=now - dt.timedelta(days=2)))
    session.add(RunMetric(run_id="rank-0002", stage="rank", metric="postings_after_filters",
                          value=24, labels={"note": "seeded"}))
    session.add(RunMetric(run_id="rank-0002", stage="fetch", metric="adapter_success_rate",
                          value=0.75))
    session.flush()

    return Seeded(
        postings=postings,
        skills=skills,
        chunks=chunks,
        now=now,
        expected_gap_order=["Kubernetes", "Go", "Terraform", "Kafka", "GraphQL", "Docker"],
        expected_covered={"Python", "SQL"},
        active_postings=ELIGIBLE_POSTINGS + 2,  # sponsorship- and role-excluded are still active
    )


@pytest.fixture
def seeded(db_session) -> Seeded:
    data = seed_report_fixture(db_session)
    db_session.flush()
    return data
