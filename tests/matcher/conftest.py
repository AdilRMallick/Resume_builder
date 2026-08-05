"""Fixtures for the matcher suite.

No test here touches the network or an API key: embeddings come from the deterministic
`hash` provider, and every LLM call is served by `StubLLM`, which is installed over
`jme.matcher.match.call_llm` - the single seam the matcher uses to reach the model.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

import pytest

from jme.embeddings import HashEmbeddingProvider
from jme.llm import LLMResult
from jme.matcher import match as match_module
from jme.models import (
    CanonicalSkill,
    EvidenceChunk,
    EvidenceSkill,
    EvidenceVersion,
    FetchStatus,
    Importance,
    Posting,
    PostingJD,
    PostingRequirement,
    ShortlistEntry,
    SkillCategory,
)

MODEL_ID = "claude-opus-5"

JD_TEXT = """\
Software Engineer, New Grad at Acme.
You will build distributed backend services.
Requirements: strong Go, production PostgreSQL experience, Kubernetes a plus.
"""

CHUNK_TEXTS = [
    (
        "go-pipeline",
        "Redis pipeline",
        "Built a Redis Streams consumer group harness in Go with XAUTOCLAIM-based reclaim "
        "of messages abandoned by dead workers, plus a dead letter stream after N attempts.",
    ),
    (
        "postgres-schema",
        "Schema design",
        "Designed the PostgreSQL schema with pgvector embeddings and an HNSW index, and "
        "tuned the active-feed query with EXPLAIN ANALYZE on PostgreSQL 16.",
    ),
    (
        "k8s-eks",
        "Infrastructure",
        "Ran Kubernetes workloads on AWS EKS, wrote Helm charts, and debugged a "
        "noisy-neighbour CPU throttling issue with cgroup metrics.",
    ),
    (
        "react-dashboard",
        "Frontend",
        "Built a React dashboard over a FastAPI backend, with server-side pagination and a "
        "typed OpenAPI client.",
    ),
]


# --------------------------------------------------------------------------------------
# the stubbed model
# --------------------------------------------------------------------------------------


@dataclass
class StubCall:
    system: str
    user: str
    run_id: str | None
    extra_key_parts: tuple[str, ...]
    force_refresh: bool


class StubLLM:
    """Stands in for `jme.matcher.match.call_llm`. Records every call it receives.

    Payloads are consumed in order; the last one repeats if the matcher calls more times
    than there are payloads (which is what makes "returns the same fabrication twice"
    easy to express).
    """

    def __init__(
        self,
        payloads: list[dict[str, Any]],
        *,
        cost_usd: float = 0.004,
        input_tokens: int = 3200,
        output_tokens: int = 700,
        cached: bool = False,
    ) -> None:
        self.payloads = payloads
        self.cost_usd = cost_usd
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cached = cached
        self.calls: list[StubCall] = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def __call__(
        self,
        session,
        *,
        system: str,
        user: str,
        run_id: str | None,
        extra_key_parts: tuple[str, ...],
        force_refresh: bool = False,
        model: str | None = None,
    ) -> LLMResult:
        self.calls.append(
            StubCall(
                system=system,
                user=user,
                run_id=run_id,
                extra_key_parts=tuple(extra_key_parts),
                force_refresh=force_refresh,
            )
        )
        index = min(len(self.calls) - 1, len(self.payloads) - 1)
        return LLMResult(
            payload=self.payloads[index],
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cost_usd=self.cost_usd,
            cached=self.cached,
            model=model or MODEL_ID,
        )


@pytest.fixture
def install_stub(monkeypatch):
    def _install(payloads: list[dict[str, Any]], **kwargs: Any) -> StubLLM:
        stub = StubLLM(payloads, **kwargs)
        monkeypatch.setattr(match_module, "call_llm", stub)
        return stub

    return _install


# --------------------------------------------------------------------------------------
# database seed
# --------------------------------------------------------------------------------------


@dataclass
class Seed:
    posting: Posting
    requirements: list[PostingRequirement] = field(default_factory=list)
    chunks: list[EvidenceChunk] = field(default_factory=list)
    skills: dict[str, CanonicalSkill] = field(default_factory=dict)
    jd_text: str = JD_TEXT

    @property
    def posting_id(self) -> int:
        return self.posting.id

    @property
    def chunk_ids(self) -> list[int]:
        return [chunk.id for chunk in self.chunks]

    @property
    def requirement_ids(self) -> list[int]:
        return [req.id for req in self.requirements]


def make_posting(session, *, key: str, company: str = "Acme", inactive: bool = False) -> Posting:
    now = dt.datetime.now(dt.UTC)
    posting = Posting(
        canonical_key=key,
        company=company,
        title="Software Engineer, New Grad",
        url=f"https://boards.greenhouse.io/{key}",
        url_host="boards.greenhouse.io",
        locations=["Detroit, MI"],
        role_type="swe",
        first_seen_at=now,
        last_seen_at=now,
        inactive_at=now if inactive else None,
    )
    session.add(posting)
    session.flush()
    session.add(
        PostingJD(
            posting_id=posting.id,
            adapter="greenhouse",
            raw_text=JD_TEXT,
            fetch_status=FetchStatus.ok,
            char_count=len(JD_TEXT),
            extracted_at=now,
        )
    )
    session.flush()
    return posting


def make_requirements(session, posting: Posting, skills: dict[str, CanonicalSkill]):
    spec = [
        ("Strong experience with Go", skills["Go"], Importance.required),
        ("Production PostgreSQL experience", skills["PostgreSQL"], Importance.required),
        ("Kubernetes a plus", skills["Kubernetes"], Importance.preferred),
    ]
    out = []
    for raw_text, skill, importance in spec:
        req = PostingRequirement(
            posting_id=posting.id,
            canonical_skill_id=skill.id,
            raw_text=raw_text,
            importance=importance,
            confidence=0.9,
            prompt_version="v1",
            model_id=MODEL_ID,
        )
        session.add(req)
        out.append(req)
    session.flush()
    return out


def set_evidence_version(session, version: int) -> None:
    row = session.get(EvidenceVersion, 1)
    if row is None:
        session.add(EvidenceVersion(id=1, version=version, reason="test seed"))
    else:
        row.version = version
    session.flush()


@pytest.fixture
def seed(db_session) -> Seed:
    provider = HashEmbeddingProvider()

    skills = {}
    for name, category in [
        ("Go", SkillCategory.language),
        ("PostgreSQL", SkillCategory.database),
        ("Kubernetes", SkillCategory.infra),
        ("React", SkillCategory.framework),
    ]:
        skill = CanonicalSkill(name=name, category=category, is_actionable=True)
        db_session.add(skill)
        skills[name] = skill
    db_session.flush()

    chunks = []
    for ordinal, (ref, heading, text) in enumerate(CHUNK_TEXTS):
        chunk = EvidenceChunk(
            source_type="markdown",
            source_ref=f"evidence/{ref}.md",
            heading=heading,
            ordinal=ordinal,
            text=text,
            text_sha256=f"{ordinal:064d}",
            token_estimate=len(text) // 4,
            embedding=provider.embed_one(text),
            embedding_model=provider.name,
            evidence_version=1,
        )
        db_session.add(chunk)
        chunks.append(chunk)
    db_session.flush()

    set_evidence_version(db_session, 1)

    posting = make_posting(db_session, key="acme-swe-1")
    requirements = make_requirements(db_session, posting, skills)
    db_session.flush()

    return Seed(posting=posting, requirements=requirements, chunks=chunks, skills=skills)


def add_shortlist(session, run_id: str, posting_ids: list[int], evidence_version: int = 1) -> None:
    for rank, posting_id in enumerate(posting_ids, start=1):
        session.add(
            ShortlistEntry(
                run_id=run_id,
                posting_id=posting_id,
                rank=rank,
                coarse_score=round(1.0 / rank, 5),
                evidence_version=evidence_version,
            )
        )
    session.flush()


def tag_chunk(session, chunk: EvidenceChunk, skill: CanonicalSkill) -> None:
    session.add(EvidenceSkill(evidence_chunk_id=chunk.id, canonical_skill_id=skill.id))
    session.flush()


# --------------------------------------------------------------------------------------
# payload builders
# --------------------------------------------------------------------------------------


def payload(
    seed: Seed,
    *,
    statuses: list[str] | None = None,
    chunk_ids: list[int | None] | None = None,
    verdict: str = "plausible",
    rationale: str = "Strong on Go and Postgres, thin on Kubernetes.",
) -> dict[str, Any]:
    """A well-formed response citing real chunk ids unless told otherwise."""
    statuses = statuses or ["evidenced", "evidenced", "weak"]
    if chunk_ids is None:
        chunk_ids = [seed.chunks[0].id, seed.chunks[1].id, seed.chunks[2].id]

    rows = []
    for req, status, chunk_id in zip(seed.requirements, statuses, chunk_ids, strict=True):
        rows.append(
            {
                "requirement_id": req.id,
                "canonical_skill_id": req.canonical_skill_id,
                "status": status,
                "evidence_chunk_id": chunk_id,
                "reasoning": f"{status} for {req.raw_text}",
            }
        )
    return {"requirements": rows, "verdict": verdict, "rationale": rationale}
