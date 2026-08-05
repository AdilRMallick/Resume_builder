"""Pydantic response models for the read-only API.

These are the wire contract. ORM objects are never returned directly: the models carry
columns the UI has no business seeing (raw feed JSON, whole JD bodies) and their shape
changes for reasons that have nothing to do with the API.

Everything here is a *response* model. There are no request bodies, because there are no
writes - ARCHITECTURE section 10 rules out multi-user, auth, and tenancy, and a
read-only surface is what makes that safe.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page(BaseModel, Generic[T]):
    """Offset pagination. `total` is the count before limit/offset."""

    items: list[T]
    total: int
    limit: int
    offset: int


# --------------------------------------------------------------------------------------
# health
# --------------------------------------------------------------------------------------


class Health(BaseModel):
    status: str = Field(description="ok when the database answered, degraded otherwise")
    database: str
    evidence_version: int | None = None
    error: str | None = None


# --------------------------------------------------------------------------------------
# postings
# --------------------------------------------------------------------------------------


class PostingSummary(ORMModel):
    id: int
    company: str
    title: str
    url: str
    role_type: str | None = None
    locations: list[Any] | None = None
    is_remote: bool = False
    sponsorship: str | None = None
    posted_at: dt.datetime | None = None
    first_seen_at: dt.datetime
    last_seen_at: dt.datetime
    inactive_at: dt.datetime | None = None
    repost_count: int = 0
    has_jd: bool = False
    jd_adapter: str | None = None
    jd_fetch_status: str | None = None


class RequirementOut(ORMModel):
    id: int
    canonical_skill_id: int | None = None
    skill: str | None = None
    raw_text: str
    importance: str
    confidence: float
    prompt_version: str


class CitationOut(ORMModel):
    canonical_skill_id: int | None = None
    skill: str | None = None
    evidence_chunk_id: int | None = None
    status: str
    reasoning: str | None = None


class MatchOut(ORMModel):
    id: int
    evidence_version: int
    prompt_version: str
    model_id: str
    score: float | None = None
    verdict: str | None = None
    rationale: dict[str, Any] | None = None
    is_stale: bool
    computed_at: dt.datetime
    citations: list[CitationOut] = Field(default_factory=list)


class JDOut(ORMModel):
    adapter: str | None = None
    fetch_status: str
    char_count: int
    attempts: int
    extracted_at: dt.datetime | None = None
    fetch_error: str | None = None
    #: the JD body is deliberately not returned; `char_count` is the useful signal here
    has_text: bool = False


class PostingDetail(PostingSummary):
    jd: JDOut | None = None
    requirements: list[RequirementOut] = Field(default_factory=list)
    latest_match: MatchOut | None = Field(
        default=None,
        description="most recent match by computed_at, with its per-skill citations",
    )


# --------------------------------------------------------------------------------------
# shortlist
# --------------------------------------------------------------------------------------


class ShortlistItem(BaseModel):
    rank: int
    coarse_score: float
    evidence_version: int
    posting: PostingSummary


class ShortlistOut(BaseModel):
    run_id: str | None = None
    created_at: dt.datetime | None = None
    count: int = 0
    items: list[ShortlistItem] = Field(default_factory=list)


# --------------------------------------------------------------------------------------
# gaps
# --------------------------------------------------------------------------------------


class GapCounts(BaseModel):
    eligible_postings: int
    requirements_total: int
    requirements_mapped: int
    requirements_unmapped: int
    taxonomy_coverage: float
    actionable_skills: int
    gaps: int
    covered: int
    gaps_returned: int


class GapSkill(BaseModel):
    canonical_skill_id: int
    skill: str
    category: str
    required_count: int
    preferred_count: int
    mentioned_count: int
    posting_count: int
    status: str
    evidenced_citations: int
    weak_citations: int
    absent_citations: int
    matches_considered: int
    is_gap: bool


class GapReportOut(BaseModel):
    """Mirrors `jme.report.render` schema 1.0 exactly, so the CLI JSON export and the
    API return the same document."""

    schema_version: str
    kind: str
    generated_at: str
    evidence_version: int
    status_rule: str
    counts: GapCounts
    query_seconds: float
    gaps: list[GapSkill]
    covered: list[GapSkill] | None = None


# --------------------------------------------------------------------------------------
# skills
# --------------------------------------------------------------------------------------


class SkillOut(BaseModel):
    id: int
    name: str
    category: str
    is_actionable: bool
    alias_count: int
    requirement_count: int
    posting_count: int
    required_count: int


# --------------------------------------------------------------------------------------
# metrics
# --------------------------------------------------------------------------------------


class MetricOut(BaseModel):
    id: int
    run_id: str
    stage: str
    metric: str
    value: float
    labels: dict[str, Any] | None = None
    recorded_at: dt.datetime


# --------------------------------------------------------------------------------------
# ops
# --------------------------------------------------------------------------------------


class StreamStatus(BaseModel):
    stream: str
    group: str
    depth: int | None = None
    pending: int | None = None
    oldest_pending_age_sec: float | None = None
    consumers: int | None = None
    dead_letter_depth: int | None = None
    error: str | None = Field(
        default=None,
        description="per-stream failure, e.g. the consumer group does not exist yet",
    )


class QueueStatus(BaseModel):
    redis_url: str
    ok: bool
    error: str | None = Field(
        default=None, description="set when Redis itself is unreachable; streams will be empty"
    )
    streams: list[StreamStatus] = Field(default_factory=list)
