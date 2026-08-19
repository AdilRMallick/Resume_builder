"""Pydantic response models for the read-only API.

These are the wire contract. ORM objects are never returned directly: the models carry
columns the UI has no business seeing (raw feed JSON, whole JD bodies) and their shape
changes for reasons that have nothing to do with the API.

Most models are response contracts. The resume request bodies drive stateless local
computations and are never persisted. ARCHITECTURE section 10 still rules out multi-user
hosting, auth, and remote writes.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Generic, Literal, TypeVar

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
# daily digest
# --------------------------------------------------------------------------------------


class DigestCounts(BaseModel):
    shortlisted: int
    roles_returned: int
    matched: int
    stale_matches: int
    unmatched: int
    live_evidence_chunks: int
    placeholder_evidence_chunks: int
    gaps: int
    gaps_returned: int
    taxonomy_coverage: float


class DigestCitationOut(BaseModel):
    skill: str
    status: str
    evidence_chunk_id: int | None = None
    source_ref: str | None = None
    reasoning: str | None = None


class DigestRoleOut(BaseModel):
    rank: int
    posting_id: int
    company: str
    title: str
    url: str
    coarse_score: float
    match_id: int | None = None
    verdict: str | None = None
    match_score: float | None = None
    rationale: str | None = None
    match_stale: bool
    computed_at: str | None = None
    evidenced_count: int
    weak_count: int
    absent_count: int
    citations: list[DigestCitationOut] = Field(default_factory=list)


class DigestOut(BaseModel):
    schema_version: str
    kind: str
    generated_at: str
    shortlist_run_id: str | None = None
    evidence_version: int
    counts: DigestCounts
    warnings: list[str] = Field(default_factory=list)
    roles: list[DigestRoleOut] = Field(default_factory=list)
    gaps: list[GapSkill] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------------------
# resume studio
# --------------------------------------------------------------------------------------


class ResumeContactOut(BaseModel):
    label: str
    value: str
    url: str | None = None


class ResumeBulletOut(BaseModel):
    text: str
    tags: list[str] = Field(default_factory=list)
    source_text: str | None = None
    ai_rewritten: bool = False


class ResumeEntryOut(BaseModel):
    organization: str
    location: str = ""
    title: str
    dates: str
    url: str | None = None
    bullets: list[ResumeBulletOut] = Field(default_factory=list)


class ResumeProfileOut(BaseModel):
    name: str
    headline: str
    contact: list[ResumeContactOut] = Field(default_factory=list)
    education: list[ResumeEntryOut] = Field(default_factory=list)
    experience: list[ResumeEntryOut] = Field(default_factory=list)
    projects: list[ResumeEntryOut] = Field(default_factory=list)
    leadership: list[ResumeEntryOut] = Field(default_factory=list)
    skills: dict[str, list[str]] = Field(default_factory=dict)
    certifications: list[str] = Field(default_factory=list)


class TailorResumeRequest(BaseModel):
    job_description: str = Field(min_length=50, max_length=100_000)
    title: str = Field(default="", max_length=512)
    company: str = Field(default="", max_length=512)
    url: str = Field(default="", max_length=4096)
    steering_prompt: str = Field(default="", max_length=4000)
    render_pdf: bool = False
    customization_mode: Literal[
        "verified", "openai", "anthropic", "gemini", "kimi"
    ] = "verified"


class ResumeChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=2000)


class ResumeChatRequest(BaseModel):
    job_description: str = Field(min_length=50, max_length=100_000)
    title: str = Field(default="", max_length=512)
    company: str = Field(default="", max_length=512)
    url: str = Field(default="", max_length=4096)
    steering_prompt: str = Field(default="", max_length=4000)
    provider: Literal["openai", "anthropic", "gemini", "kimi"]
    messages: list[ResumeChatMessage] = Field(min_length=1, max_length=12)
    render_pdf: bool = True


class ResumeTargetOut(BaseModel):
    company: str = ""
    title: str = ""
    url: str = ""


class ResumeCustomizationOut(BaseModel):
    requested_mode: Literal[
        "verified", "openai", "anthropic", "gemini", "kimi"
    ] = "verified"
    applied_mode: Literal["verified", "ai"] = "verified"
    provider: Literal["openai", "anthropic", "gemini", "kimi"] | None = None
    model: str | None = None
    rewritten_bullets: int = 0
    rejected_rewrites: int = 0
    warning: str | None = None


class ResumeProviderOut(BaseModel):
    id: Literal["verified", "openai", "anthropic", "gemini", "kimi"]
    label: str
    available: bool
    model: str | None = None


class ResumeProvidersOut(BaseModel):
    providers: list[ResumeProviderOut]


class TailoredResumeOut(ResumeProfileOut):
    template_id: str
    role_focus: str
    target: ResumeTargetOut
    matched_skills: list[str] = Field(default_factory=list)
    customization: ResumeCustomizationOut
    source_rule: str
    latex: str
    pdf_base64: str | None = None
    pdf_error: str | None = None
    pdf_pages: int | None = None
    pdf_omitted_bullets: int = 0


class ResumeChatOut(TailoredResumeOut):
    chat_reply: str
    chat_removed_bullets: int = 0
    chat_prioritized_bullets: int = 0


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
