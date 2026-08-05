"""Stage 2: coarse relevance of a posting to the evidence corpus.

This is the cheap ranker that decides which postings are worth spending an LLM call
on. It never calls an LLM itself: it embeds each extracted requirement with
`jme.embeddings.get_provider()` and asks pgvector for the single closest live
evidence chunk, using cosine distance (`<=>`) against the HNSW index
`ix_evidence_chunk_embedding_hnsw`.

## The formula

For a posting with requirements r_1..r_n:

    base   = sum_i( weight(importance_i) * similarity_i ) / (REQUIRED_WEIGHT * n)
    coarse = (1 - LOCATION_BOOST_WEIGHT) * base + LOCATION_BOOST_WEIGHT * boost

where `similarity_i = clamp(1 - cosine_distance, 0, 1)` against the best-matching
live chunk, and `boost` is 1.0 when the posting sits in a `config.location_boost`
metro and 0.0 otherwise.

Two deliberate choices:

* **The denominator is `REQUIRED_WEIGHT * n`, not `sum_i weight_i`.** A weighted mean
  would normalise the weights straight back out again: a posting with one `required`
  match and a posting with one `preferred` match would both score exactly the
  similarity, and importance would only matter for postings with a mixed requirement
  list. Normalising against "what this posting would score if every requirement were
  `required` and perfectly evidenced" keeps the score in [0, 1] *and* makes matching a
  hard requirement strictly more valuable than matching a soft one. The score reads
  as: evidence-backed coverage of what this posting actually demands.
* **The location boost is a convex combination, not an additive bonus.** Adding a
  constant would push scores out of [0, 1] and force a clamp, which silently destroys
  ordering information at the top of the range. At 0.10 a boosted metro is worth at
  most a tenth of the score -- a tie-breaker between comparable postings, not
  something that can drag an irrelevant Detroit posting over a strong remote one.

## Postings with no extracted requirements

Fetch or extraction failures must degrade, not drop (ARCHITECTURE.md section 8). A
posting with zero requirements is scored on the similarity of "title company" to the
corpus, discounted by FALLBACK_CONFIDENCE_FACTOR, and flagged `low_confidence`. It
lands mid-table rather than pinned at 0.0, which is the honest position: we do not
know that it is a bad match, we know that we could not read it.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from jme.config import Settings, get_settings
from jme.embeddings import EmbeddingProvider, get_provider
from jme.logging import get_logger
from jme.models import EMBEDDING_DIM, Importance
from jme.rank.filters import FilteredPosting

logger = get_logger(__name__)


# --------------------------------------------------------------------------------------
# weights
# --------------------------------------------------------------------------------------

#: A `required` skill is the one that gates the application: failing it is
#: disqualifying, so it anchors the scale at 1.0.
REQUIRED_WEIGHT = 1.0
#: `preferred` is worth half a `required`. Two evidenced "nice to haves" are roughly
#: as persuasive as one evidenced hard requirement, which matches how these read in a
#: real screen.
PREFERRED_WEIGHT = 0.5
#: `mentioned` is context ("you will work alongside our Kafka team"), not a demand. It
#: is kept non-zero so it can still break ties, but it cannot carry a posting.
MENTIONED_WEIGHT = 0.2

WEIGHTS: dict[Importance, float] = {
    Importance.required: REQUIRED_WEIGHT,
    Importance.preferred: PREFERRED_WEIGHT,
    Importance.mentioned: MENTIONED_WEIGHT,
}

#: Share of the final score reserved for the geography preference. See module docstring.
LOCATION_BOOST_WEIGHT = 0.10

#: Title+company is a weaker signal than an extracted requirement list, so the
#: fallback path is discounted rather than trusted at face value -- but not zeroed,
#: because "we could not read the JD" is not "you are a bad fit".
FALLBACK_CONFIDENCE_FACTOR = 0.85

#: Coarse scores are persisted into `shortlist_entry.coarse_score NUMERIC(6,5)`.
#: Rounding to the column's precision *before* sorting is what makes ties genuine
#: ties, so the stable tie-break on posting_id actually decides them instead of
#: float noise deciding them differently on every run.
SCORE_PRECISION = 5

#: Requirement vectors are shipped to Postgres as bind parameters. 1536 floats of
#: text is ~15-20 KB per vector, so batch rather than building one enormous statement.
NEAREST_BATCH_SIZE = 200

#: Candidates pulled per query vector before the deterministic tie-break in Python.
#: This is not an accuracy knob, it is a determinism knob: `ORDER BY distance` alone
#: leaves exact ties to be resolved by physical row order, but adding `, c.id` as a
#: second sort key makes the ordering unsatisfiable by the HNSW index and collapses the
#: plan to a seq scan plus top-N heapsort (measured at 7,320 chunks: 0.3 ms index scan
#: vs 54 ms seq scan). So: order by distance alone, take a few candidates, and break
#: ties on chunk id in Python where it costs nothing.
NEAREST_PROBE_K = 5


# --------------------------------------------------------------------------------------
# results
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RequirementMatch:
    requirement_id: int
    raw_text: str
    importance: Importance
    weight: float
    similarity: float
    evidence_chunk_id: int | None

    @property
    def contribution(self) -> float:
        return self.weight * self.similarity


@dataclass(frozen=True)
class PostingScore:
    posting_id: int
    coarse_score: float
    basis: str  # "requirements" | "title_company"
    requirement_count: int
    location_boosted: bool
    low_confidence: bool
    matches: tuple[RequirementMatch, ...] = field(default_factory=tuple)

    @property
    def sort_key(self) -> tuple[float, int]:
        """Descending score, ascending posting_id. Never depends on SQL row order."""
        return (-self.coarse_score, self.posting_id)


# --------------------------------------------------------------------------------------
# pure scoring
# --------------------------------------------------------------------------------------


def weight_for(importance: Importance | str) -> float:
    if isinstance(importance, str):
        importance = Importance(importance)
    return WEIGHTS[importance]


def clamp_similarity(value: float) -> float:
    """Cosine similarity into [0, 1].

    The hash embedding provider uses signed hashing, so unrelated texts can land at a
    small negative cosine. Negative contributions are meaningless here (an irrelevant
    requirement should contribute nothing, not subtract from a matched one), so the
    floor is 0.
    """
    return max(0.0, min(1.0, value))


def combine(
    matches: Sequence[RequirementMatch],
    *,
    location_boosted: bool,
    basis_factor: float = 1.0,
) -> float:
    """The formula from the module docstring. Pure, no database, no config."""
    if matches:
        denominator = REQUIRED_WEIGHT * len(matches)
        base = sum(m.contribution for m in matches) / denominator
    else:
        base = 0.0
    base = max(0.0, min(1.0, base * basis_factor))
    boost = 1.0 if location_boosted else 0.0
    score = (1.0 - LOCATION_BOOST_WEIGHT) * base + LOCATION_BOOST_WEIGHT * boost
    return round(score, SCORE_PRECISION)


def has_boost_location(
    locations: Iterable[str], title: str = "", boost_terms: Sequence[str] = ()
) -> bool:
    """Case-insensitive substring match of the boost allowlist against the locations.

    `is_remote` deliberately does not earn the boost: the boost expresses "I would
    rather be in these metros", and a remote role is neither in them nor out of them.
    """
    haystacks = [str(loc).lower() for loc in locations]
    if title:
        haystacks.append(title.lower())
    return any(term.strip().lower() in hay for term in boost_terms if term.strip() for hay in haystacks)


# --------------------------------------------------------------------------------------
# pgvector retrieval
# --------------------------------------------------------------------------------------

# One parameterised index probe per query vector. The LATERAL is what lets the HNSW
# index answer the ORDER BY per outer row: the query vector is an outer reference, so
# each iteration is its own index scan. `deleted_at IS NULL` is applied as a filter on
# top of the index scan, which is why we pull NEAREST_PROBE_K candidates rather than 1.
_NEAREST_SQL = f"""
SELECT q.key AS key, m.chunk_id AS chunk_id, m.similarity AS similarity
FROM unnest(:keys ::bigint[], :embeddings ::text[]::vector({EMBEDDING_DIM})[]) AS q(key, emb)
CROSS JOIN LATERAL (
    SELECT c.id AS chunk_id, 1 - (c.embedding <=> q.emb) AS similarity
    FROM evidence_chunk c
    WHERE c.deleted_at IS NULL AND c.embedding IS NOT NULL
    ORDER BY c.embedding <=> q.emb
    LIMIT :probe_k
) m
"""


@dataclass(frozen=True)
class ChunkHit:
    evidence_chunk_id: int
    similarity: float


def _vector_literal(vec: Sequence[float]) -> str:
    return "[" + ",".join(f"{v:.6g}" for v in vec) + "]"


def _better(candidate: ChunkHit, incumbent: ChunkHit | None) -> bool:
    """Higher similarity wins; exact ties go to the lower chunk id. Deterministic."""
    if incumbent is None:
        return True
    lhs = (-round(candidate.similarity, 9), candidate.evidence_chunk_id)
    rhs = (-round(incumbent.similarity, 9), incumbent.evidence_chunk_id)
    return lhs < rhs


def nearest_chunks(
    session: Session,
    keyed_texts: Sequence[tuple[int, str]],
    provider: EmbeddingProvider | None = None,
) -> dict[int, ChunkHit]:
    """key -> best live evidence chunk for each text.

    Keys with no live chunk in the corpus are simply absent from the result.
    """
    if not keyed_texts:
        return {}
    provider = provider or get_provider()

    # Identical requirement text across postings is common ("Bachelor's degree in
    # Computer Science"); embed each distinct string once.
    distinct = sorted({txt for _, txt in keyed_texts})
    vectors = dict(zip(distinct, provider.embed(distinct), strict=True))

    stmt = text(_NEAREST_SQL).bindparams(bindparam("keys"), bindparam("embeddings"))
    out: dict[int, ChunkHit] = {}
    for start in range(0, len(keyed_texts), NEAREST_BATCH_SIZE):
        batch = keyed_texts[start : start + NEAREST_BATCH_SIZE]
        rows = session.execute(
            stmt,
            {
                "keys": [key for key, _ in batch],
                "embeddings": [_vector_literal(vectors[txt]) for _, txt in batch],
                "probe_k": NEAREST_PROBE_K,
            },
        ).all()
        for key, chunk_id, similarity in rows:
            hit = ChunkHit(int(chunk_id), float(similarity))
            if _better(hit, out.get(int(key))):
                out[int(key)] = hit
    return out


# --------------------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------------------

def _similarity(hit: ChunkHit | None) -> float:
    """A requirement with no chunk in the corpus contributes nothing, not a penalty."""
    return hit.similarity if hit is not None else 0.0


def _chunk_id(hit: ChunkHit | None) -> int | None:
    return hit.evidence_chunk_id if hit is not None else None


_REQUIREMENTS_SQL = """
SELECT r.id, r.posting_id, r.raw_text, r.importance
FROM posting_requirement r
WHERE r.posting_id = ANY(:posting_ids)
ORDER BY r.posting_id, r.id
"""


def score_postings(
    session: Session,
    postings: Sequence[FilteredPosting],
    settings: Settings | None = None,
    provider: EmbeddingProvider | None = None,
) -> list[PostingScore]:
    """Score every surviving posting. Output is sorted: score desc, posting_id asc."""
    if not postings:
        return []
    settings = settings or get_settings()
    provider = provider or get_provider()

    posting_ids = [p.posting_id for p in postings]
    req_rows = session.execute(
        text(_REQUIREMENTS_SQL).bindparams(bindparam("posting_ids")),
        {"posting_ids": posting_ids},
    ).all()

    by_posting: dict[int, list[tuple[int, str, Importance]]] = {pid: [] for pid in posting_ids}
    keyed: list[tuple[int, str]] = []
    for req_id, posting_id, raw_text, importance in req_rows:
        imp = importance if isinstance(importance, Importance) else Importance(importance)
        by_posting[int(posting_id)].append((int(req_id), raw_text, imp))
        keyed.append((int(req_id), raw_text))

    req_nearest = nearest_chunks(session, keyed, provider)

    # Fallback path: postings with nothing extracted are matched on title + company.
    fallback_keyed = [
        (p.posting_id, f"{p.title} {p.company}") for p in postings if not by_posting[p.posting_id]
    ]
    fallback_nearest = nearest_chunks(session, fallback_keyed, provider)

    scores: list[PostingScore] = []
    for posting in postings:
        boosted = has_boost_location(
            posting.locations, boost_terms=settings.location_boost
        )
        reqs = by_posting[posting.posting_id]
        if reqs:
            matches = tuple(
                RequirementMatch(
                    requirement_id=req_id,
                    raw_text=raw_text,
                    importance=imp,
                    weight=weight_for(imp),
                    similarity=clamp_similarity(_similarity(req_nearest.get(req_id))),
                    evidence_chunk_id=_chunk_id(req_nearest.get(req_id)),
                )
                for req_id, raw_text, imp in reqs
            )
            scores.append(
                PostingScore(
                    posting_id=posting.posting_id,
                    coarse_score=combine(matches, location_boosted=boosted),
                    basis="requirements",
                    requirement_count=len(matches),
                    location_boosted=boosted,
                    low_confidence=False,
                    matches=matches,
                )
            )
            continue

        hit = fallback_nearest.get(posting.posting_id)
        pseudo = RequirementMatch(
            requirement_id=-posting.posting_id,
            raw_text=f"{posting.title} {posting.company}",
            importance=Importance.required,
            weight=REQUIRED_WEIGHT,
            similarity=clamp_similarity(_similarity(hit)),
            evidence_chunk_id=_chunk_id(hit),
        )
        scores.append(
            PostingScore(
                posting_id=posting.posting_id,
                coarse_score=combine(
                    (pseudo,),
                    location_boosted=boosted,
                    basis_factor=FALLBACK_CONFIDENCE_FACTOR,
                ),
                basis="title_company",
                requirement_count=0,
                location_boosted=boosted,
                low_confidence=True,
                matches=(pseudo,),
            )
        )

    scores.sort(key=lambda s: s.sort_key)
    logger.info(
        "stage2.scored",
        postings=len(scores),
        low_confidence=sum(1 for s in scores if s.low_confidence),
        boosted=sum(1 for s in scores if s.location_boosted),
    )
    return scores
