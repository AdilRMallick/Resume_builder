"""SQLAlchemy models. This module is the schema contract every service codes against.

Conventions:
  * surrogate integer PKs everywhere except where a natural key is genuinely stable
  * all timestamps are timezone-aware UTC
  * nothing is ever hard deleted from `posting`; lifecycle is expressed via `inactive_at`
"""

from __future__ import annotations

import datetime as dt
import enum

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

EMBEDDING_DIM = 1536


class Base(DeclarativeBase):
    pass


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


TS = DateTime(timezone=True)


# --------------------------------------------------------------------------------------
# enums
# --------------------------------------------------------------------------------------


class Importance(str, enum.Enum):
    required = "required"
    preferred = "preferred"
    mentioned = "mentioned"


class FetchStatus(str, enum.Enum):
    pending = "pending"
    ok = "ok"
    not_found = "not_found"
    rate_limited = "rate_limited"
    transient_error = "transient_error"
    permanent_error = "permanent_error"
    robots_denied = "robots_denied"
    unsupported = "unsupported"


class CitationStatus(str, enum.Enum):
    evidenced = "evidenced"
    weak = "weak"
    absent = "absent"


class Verdict(str, enum.Enum):
    strong = "strong"
    plausible = "plausible"
    stretch = "stretch"
    no = "no"


class CandidateStatus(str, enum.Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    promoted = "promoted"


class SkillCategory(str, enum.Enum):
    language = "language"
    database = "database"
    cloud = "cloud"
    infra = "infra"
    ml = "ml"
    framework = "framework"
    practice = "practice"
    domain = "domain"
    soft = "soft"


ImportanceEnum = Enum(Importance, name="importance", values_callable=lambda e: [m.value for m in e])
FetchStatusEnum = Enum(
    FetchStatus, name="fetch_status", values_callable=lambda e: [m.value for m in e]
)
CitationStatusEnum = Enum(
    CitationStatus, name="citation_status", values_callable=lambda e: [m.value for m in e]
)
VerdictEnum = Enum(Verdict, name="verdict", values_callable=lambda e: [m.value for m in e])
CandidateStatusEnum = Enum(
    CandidateStatus, name="candidate_status", values_callable=lambda e: [m.value for m in e]
)
SkillCategoryEnum = Enum(
    SkillCategory, name="skill_category", values_callable=lambda e: [m.value for m in e]
)


# --------------------------------------------------------------------------------------
# feed
# --------------------------------------------------------------------------------------


class Posting(Base):
    __tablename__ = "posting"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    canonical_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    simplify_id: Mapped[str | None] = mapped_column(String(128), index=True)

    company: Mapped[str] = mapped_column(String(512), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    url_host: Mapped[str | None] = mapped_column(String(255), index=True)
    locations: Mapped[list | None] = mapped_column(JSONB, default=list)
    sponsorship: Mapped[str | None] = mapped_column(String(128))
    role_type: Mapped[str | None] = mapped_column(String(64), index=True)
    start_season: Mapped[str | None] = mapped_column(String(64))
    is_remote: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    posted_at: Mapped[dt.datetime | None] = mapped_column(TS)
    first_seen_at: Mapped[dt.datetime] = mapped_column(TS, default=_utcnow, nullable=False)
    last_seen_at: Mapped[dt.datetime] = mapped_column(TS, default=_utcnow, nullable=False)
    inactive_at: Mapped[dt.datetime | None] = mapped_column(TS)
    repost_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    raw: Mapped[dict | None] = mapped_column(JSONB)

    jd: Mapped[PostingJD | None] = relationship(back_populates="posting", uselist=False)
    requirements: Mapped[list[PostingRequirement]] = relationship(back_populates="posting")

    __table_args__ = (
        # the active-feed query
        Index("ix_posting_active_feed", "inactive_at", "posted_at"),
        Index("ix_posting_last_seen", "last_seen_at"),
    )


class PostingJD(Base):
    __tablename__ = "posting_jd"

    posting_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("posting.id", ondelete="CASCADE"), primary_key=True
    )
    adapter: Mapped[str | None] = mapped_column(String(64), index=True)
    raw_text: Mapped[str | None] = mapped_column(Text)
    text_sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    title: Mapped[str | None] = mapped_column(String(512))
    location: Mapped[str | None] = mapped_column(String(512))
    fetch_status: Mapped[FetchStatus] = mapped_column(
        FetchStatusEnum, default=FetchStatus.pending, nullable=False, index=True
    )
    fetch_error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    char_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    extracted_at: Mapped[dt.datetime | None] = mapped_column(TS)
    updated_at: Mapped[dt.datetime] = mapped_column(
        TS, default=_utcnow, onupdate=_utcnow, nullable=False
    )

    posting: Mapped[Posting] = relationship(back_populates="jd")


# --------------------------------------------------------------------------------------
# taxonomy
# --------------------------------------------------------------------------------------


class CanonicalSkill(Base):
    __tablename__ = "canonical_skill"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    category: Mapped[SkillCategory] = mapped_column(SkillCategoryEnum, nullable=False, index=True)
    is_actionable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)

    aliases: Mapped[list[SkillAlias]] = relationship(
        back_populates="canonical_skill", cascade="all, delete-orphan"
    )


class SkillAlias(Base):
    __tablename__ = "skill_alias"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    canonical_skill_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("canonical_skill.id", ondelete="CASCADE"), nullable=False, index=True
    )
    alias: Mapped[str] = mapped_column(String(128), nullable=False)
    # normalized form used for lookup: lowercased, punctuation stripped, whitespace collapsed
    alias_norm: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)

    canonical_skill: Mapped[CanonicalSkill] = relationship(back_populates="aliases")


class SkillAliasCandidate(Base):
    __tablename__ = "skill_alias_candidate"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    raw_text: Mapped[str] = mapped_column(String(512), nullable=False)
    raw_norm: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    seen_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    example_posting_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("posting.id", ondelete="SET NULL")
    )
    status: Mapped[CandidateStatus] = mapped_column(
        CandidateStatusEnum, default=CandidateStatus.pending, nullable=False, index=True
    )
    resolved_skill_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("canonical_skill.id", ondelete="SET NULL")
    )
    first_seen_at: Mapped[dt.datetime] = mapped_column(TS, default=_utcnow, nullable=False)
    last_seen_at: Mapped[dt.datetime] = mapped_column(TS, default=_utcnow, nullable=False)


class PostingRequirement(Base):
    __tablename__ = "posting_requirement"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    posting_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("posting.id", ondelete="CASCADE"), nullable=False, index=True
    )
    canonical_skill_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("canonical_skill.id", ondelete="SET NULL")
    )
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    importance: Mapped[Importance] = mapped_column(ImportanceEnum, nullable=False)
    confidence: Mapped[float] = mapped_column(Numeric(4, 3), nullable=False, default=0)
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False)
    model_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=_utcnow, nullable=False)

    posting: Mapped[Posting] = relationship(back_populates="requirements")

    __table_args__ = (
        # the gap rollup
        Index("ix_requirement_skill_importance", "canonical_skill_id", "importance"),
        UniqueConstraint(
            "posting_id", "raw_text", "prompt_version", name="uq_requirement_posting_raw_prompt"
        ),
    )


# --------------------------------------------------------------------------------------
# evidence
# --------------------------------------------------------------------------------------


class EvidenceChunk(Base):
    __tablename__ = "evidence_chunk"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)  # markdown | repo_readme
    source_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    heading: Mapped[str | None] = mapped_column(String(512))
    ordinal: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    text_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    token_estimate: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))
    embedding_model: Mapped[str | None] = mapped_column(String(64))
    evidence_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=_utcnow, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        TS, default=_utcnow, onupdate=_utcnow, nullable=False
    )
    deleted_at: Mapped[dt.datetime | None] = mapped_column(TS)

    __table_args__ = (
        UniqueConstraint("source_type", "source_ref", "ordinal", name="uq_chunk_source_ordinal"),
        Index("ix_chunk_live", "deleted_at"),
    )


class EvidenceSkill(Base):
    """Manual tags. A small number of these meaningfully improves retrieval precision."""

    __tablename__ = "evidence_skill"

    evidence_chunk_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("evidence_chunk.id", ondelete="CASCADE"), primary_key=True
    )
    canonical_skill_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("canonical_skill.id", ondelete="CASCADE"), primary_key=True
    )


class EvidenceVersion(Base):
    """Single-row table holding the monotonic corpus version."""

    __tablename__ = "evidence_version"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    bumped_at: Mapped[dt.datetime] = mapped_column(TS, default=_utcnow, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (CheckConstraint("id = 1", name="ck_evidence_version_singleton"),)


# --------------------------------------------------------------------------------------
# matching
# --------------------------------------------------------------------------------------


class Match(Base):
    __tablename__ = "match"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    posting_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("posting.id", ondelete="CASCADE"), nullable=False
    )
    evidence_version: Mapped[int] = mapped_column(Integer, nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False)
    model_id: Mapped[str] = mapped_column(String(64), nullable=False)
    jd_sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    score: Mapped[float | None] = mapped_column(Numeric(5, 4))
    verdict: Mapped[Verdict | None] = mapped_column(VerdictEnum)
    rationale: Mapped[dict | None] = mapped_column(JSONB)
    is_stale: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_usd: Mapped[float] = mapped_column(Numeric(10, 6), default=0, nullable=False)
    computed_at: Mapped[dt.datetime] = mapped_column(TS, default=_utcnow, nullable=False)

    citations: Mapped[list[MatchCitation]] = relationship(
        back_populates="match", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint(
            "posting_id",
            "evidence_version",
            "prompt_version",
            "model_id",
            "jd_sha256",
            name="uq_match_cache_key",
        ),
        # the recompute sweep
        Index("ix_match_stale", "is_stale", "posting_id"),
    )


class MatchCitation(Base):
    __tablename__ = "match_citation"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    match_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("match.id", ondelete="CASCADE"), nullable=False, index=True
    )
    canonical_skill_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("canonical_skill.id", ondelete="SET NULL"), index=True
    )
    evidence_chunk_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("evidence_chunk.id", ondelete="SET NULL")
    )
    status: Mapped[CitationStatus] = mapped_column(CitationStatusEnum, nullable=False)
    reasoning: Mapped[str | None] = mapped_column(Text)

    match: Mapped[Match] = relationship(back_populates="citations")

    __table_args__ = (
        CheckConstraint(
            "status <> 'evidenced' OR evidence_chunk_id IS NOT NULL",
            name="ck_evidenced_requires_chunk",
        ),
    )


class ShortlistEntry(Base):
    """Output of the filter+rank pipeline (Task 8), consumed by the LLM matcher (Task 9)."""

    __tablename__ = "shortlist_entry"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    posting_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("posting.id", ondelete="CASCADE"), nullable=False
    )
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    coarse_score: Mapped[float] = mapped_column(Numeric(6, 5), nullable=False)
    evidence_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=_utcnow, nullable=False)

    __table_args__ = (UniqueConstraint("run_id", "posting_id", name="uq_shortlist_run_posting"),)


# --------------------------------------------------------------------------------------
# ops
# --------------------------------------------------------------------------------------


class RunMetric(Base):
    __tablename__ = "run_metric"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    stage: Mapped[str] = mapped_column(String(64), nullable=False)
    metric: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    labels: Mapped[dict | None] = mapped_column(JSONB)
    recorded_at: Mapped[dt.datetime] = mapped_column(TS, default=_utcnow, nullable=False)

    __table_args__ = (Index("ix_run_metric_stage_metric", "stage", "metric", "recorded_at"),)


class LLMCache(Base):
    """Content-addressed cache for every LLM call. Key includes prompt_version and model."""

    __tablename__ = "llm_cache"

    cache_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_usd: Mapped[float] = mapped_column(Numeric(10, 6), default=0, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(TS, default=_utcnow, nullable=False)


class IngestRun(Base):
    __tablename__ = "ingest_run"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    started_at: Mapped[dt.datetime] = mapped_column(TS, default=_utcnow, nullable=False)
    finished_at: Mapped[dt.datetime | None] = mapped_column(TS)
    feed_sha256: Mapped[str | None] = mapped_column(String(64))
    total_seen: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    new_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    deactivated_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reactivated_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
