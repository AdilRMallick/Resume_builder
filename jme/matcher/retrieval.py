"""Per-requirement evidence retrieval.

For each requirement of a posting we pull the top K live evidence chunks by pgvector
cosine distance (`<=>`), with a fixed boost for chunks a human has manually tagged with
that requirement's canonical skill. Manual tags are sparse but high precision, so a
small boost is worth more than any amount of embedding tuning at this corpus size.

The union of those per-requirement hits is the *candidate set*. It is the only thing the
model is allowed to cite, and `RetrievalSet.valid_chunk_ids` is what citation validation
checks against. Real chunk ids are shown to the model rather than small ordinals: the id
is what lands in `match_citation.evidence_chunk_id`, so there is no translation layer to
get wrong, and a fabricated id is still trivially detectable against the map.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from sqlalchemy import Float, case, cast, literal, select
from sqlalchemy.orm import Session

from jme.embeddings import get_provider
from jme.logging import get_logger
from jme.models import (
    CanonicalSkill,
    EvidenceChunk,
    EvidenceSkill,
    EvidenceVersion,
    Importance,
    PostingRequirement,
)

log = get_logger(__name__)

#: How many chunks to retrieve per requirement. Configurable per call, via JME_MATCH_TOP_K,
#: or via the CLI's --k flag.
DEFAULT_TOP_K = 3

#: Subtracted from cosine distance for a chunk manually tagged with the requirement's
#: canonical skill. Cosine distance is in [0, 2]; 0.15 reorders near-ties without letting
#: a tag drag in something semantically unrelated.
DEFAULT_TAG_BOOST = 0.15


def default_top_k() -> int:
    raw = os.environ.get("JME_MATCH_TOP_K")
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            log.warning("bad_top_k_env", value=raw, fallback=DEFAULT_TOP_K)
    return DEFAULT_TOP_K


def default_tag_boost() -> float:
    raw = os.environ.get("JME_MATCH_TAG_BOOST")
    if raw:
        try:
            return float(raw)
        except ValueError:
            log.warning("bad_tag_boost_env", value=raw, fallback=DEFAULT_TAG_BOOST)
    return DEFAULT_TAG_BOOST


@dataclass(frozen=True)
class RequirementView:
    """A posting requirement flattened for prompting. `raw_text` is the literal JD span."""

    requirement_id: int
    canonical_skill_id: int | None
    skill_name: str | None
    raw_text: str
    importance: Importance

    @property
    def label(self) -> str:
        return self.skill_name or self.raw_text


@dataclass(frozen=True)
class RetrievedChunk:
    chunk_id: int
    source_type: str
    source_ref: str
    heading: str | None
    text: str
    distance: float
    tagged: bool

    @property
    def effective_score(self) -> float:
        """Lower is better. Cosine distance after the manual-tag boost."""
        return self.distance - (DEFAULT_TAG_BOOST if self.tagged else 0.0)

    @property
    def citation(self) -> str:
        head = f" - {self.heading}" if self.heading else ""
        return f"{self.source_type}:{self.source_ref}{head}"


@dataclass
class RetrievalSet:
    """Everything the matcher shows the model, plus the map used to police its answer."""

    posting_id: int
    evidence_version: int
    top_k: int
    requirements: list[RequirementView] = field(default_factory=list)
    chunks: list[RetrievedChunk] = field(default_factory=list)
    #: requirement_id -> chunk ids retrieved for it, best first
    per_requirement: dict[int, list[int]] = field(default_factory=dict)

    @property
    def valid_chunk_ids(self) -> frozenset[int]:
        return frozenset(chunk.chunk_id for chunk in self.chunks)

    @property
    def requirement_ids(self) -> frozenset[int]:
        return frozenset(req.requirement_id for req in self.requirements)

    def chunk(self, chunk_id: int) -> RetrievedChunk | None:
        for chunk in self.chunks:
            if chunk.chunk_id == chunk_id:
                return chunk
        return None

    def requirement(self, requirement_id: int) -> RequirementView | None:
        for req in self.requirements:
            if req.requirement_id == requirement_id:
                return req
        return None


def current_evidence_version(session: Session) -> int:
    row = session.get(EvidenceVersion, 1)
    return int(row.version) if row is not None else 1


def load_requirements(session: Session, posting_id: int) -> list[RequirementView]:
    """Requirements for a posting, most important first, deterministically ordered."""
    rows = session.execute(
        select(PostingRequirement, CanonicalSkill.name)
        .outerjoin(CanonicalSkill, CanonicalSkill.id == PostingRequirement.canonical_skill_id)
        .where(PostingRequirement.posting_id == posting_id)
        .order_by(PostingRequirement.id)
    ).all()

    order = {Importance.required: 0, Importance.preferred: 1, Importance.mentioned: 2}
    views = [
        RequirementView(
            requirement_id=req.id,
            canonical_skill_id=req.canonical_skill_id,
            skill_name=name,
            raw_text=req.raw_text,
            importance=req.importance,
        )
        for req, name in rows
    ]
    views.sort(key=lambda v: (order.get(v.importance, 3), v.requirement_id))
    return views


def _query_text(req: RequirementView) -> str:
    """What gets embedded. The canonical skill name is appended when known: it is the
    normalized handle for the concept and measurably helps the offline hash provider."""
    if req.skill_name and req.skill_name.lower() not in req.raw_text.lower():
        return f"{req.raw_text} {req.skill_name}"
    return req.raw_text


def retrieve_for_requirement(
    session: Session,
    req: RequirementView,
    *,
    top_k: int,
    tag_boost: float,
) -> list[RetrievedChunk]:
    provider = get_provider()
    vector = provider.embed_one(_query_text(req))

    distance = EvidenceChunk.embedding.cosine_distance(vector)

    if req.canonical_skill_id is not None:
        tagged = (
            select(literal(1))
            .select_from(EvidenceSkill)
            .where(
                EvidenceSkill.evidence_chunk_id == EvidenceChunk.id,
                EvidenceSkill.canonical_skill_id == req.canonical_skill_id,
            )
            .exists()
        )
    else:
        tagged = literal(False)

    boost = case((tagged, cast(literal(tag_boost), Float)), else_=cast(literal(0.0), Float))

    # NOTE: the hnsw index only serves a bare `ORDER BY embedding <=> $1`; subtracting the
    # tag boost forces a scan. At a few hundred chunks that is microseconds, and correctness
    # of the boost matters more. If the corpus grows past ~50k chunks, split this into an
    # index-served top-N plus a tagged-chunks query and merge in Python.
    stmt = (
        select(EvidenceChunk, distance.label("distance"), tagged.label("tagged"))
        .where(EvidenceChunk.deleted_at.is_(None), EvidenceChunk.embedding.is_not(None))
        .order_by((distance - boost).asc(), EvidenceChunk.id.asc())
        .limit(top_k)
    )

    out: list[RetrievedChunk] = []
    for chunk, dist, is_tagged in session.execute(stmt).all():
        out.append(
            RetrievedChunk(
                chunk_id=chunk.id,
                source_type=chunk.source_type,
                source_ref=chunk.source_ref,
                heading=chunk.heading,
                text=chunk.text,
                distance=float(dist),
                tagged=bool(is_tagged),
            )
        )
    return out


def retrieve_for_posting(
    session: Session,
    posting_id: int,
    *,
    top_k: int | None = None,
    tag_boost: float | None = None,
    requirements: list[RequirementView] | None = None,
) -> RetrievalSet:
    """Top-K chunks per requirement, deduplicated into one candidate set."""
    k = top_k if top_k is not None else default_top_k()
    boost = tag_boost if tag_boost is not None else default_tag_boost()
    reqs = requirements if requirements is not None else load_requirements(session, posting_id)

    result = RetrievalSet(
        posting_id=posting_id,
        evidence_version=current_evidence_version(session),
        top_k=k,
        requirements=reqs,
    )

    seen: dict[int, RetrievedChunk] = {}
    for req in reqs:
        hits = retrieve_for_requirement(session, req, top_k=k, tag_boost=boost)
        result.per_requirement[req.requirement_id] = [hit.chunk_id for hit in hits]
        for hit in hits:
            existing = seen.get(hit.chunk_id)
            # keep the best (smallest) boosted score across requirements
            if existing is None or hit.effective_score < existing.effective_score:
                seen[hit.chunk_id] = hit

    result.chunks = sorted(seen.values(), key=lambda c: (c.effective_score, c.chunk_id))

    log.debug(
        "retrieved_evidence",
        posting_id=posting_id,
        requirements=len(reqs),
        candidates=len(result.chunks),
        top_k=k,
    )
    return result
