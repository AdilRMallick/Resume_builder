"""Stage 2 scoring.

The pure-formula tests need no database. The retrieval tests do, because the whole
point is that pgvector picks the nearest live chunk with the real `<=>` operator.
"""

from __future__ import annotations

import pytest

from jme.models import Importance
from jme.rank.filters import apply_filters
from jme.rank.score import (
    LOCATION_BOOST_WEIGHT,
    MENTIONED_WEIGHT,
    PREFERRED_WEIGHT,
    REQUIRED_WEIGHT,
    RequirementMatch,
    clamp_similarity,
    combine,
    has_boost_location,
    nearest_chunks,
    score_postings,
    weight_for,
)

PYTHON_EVIDENCE = (
    "Built a Python service on Postgres with SQLAlchemy, including query optimization "
    "work that cut the active-feed scan from a sequential scan to an index scan."
)
KUBERNETES_EVIDENCE = (
    "Ran Kubernetes workloads on AWS EKS, wrote Helm charts, and debugged a CPU "
    "throttling issue using cgroup metrics."
)
PYTHON_REQUIREMENT = "Experience with Python and Postgres query optimization"


# --------------------------------------------------------------------------------------
# the formula, no database
# --------------------------------------------------------------------------------------


def _match(importance: Importance, similarity: float, requirement_id: int = 1) -> RequirementMatch:
    return RequirementMatch(
        requirement_id=requirement_id,
        raw_text="x",
        importance=importance,
        weight=weight_for(importance),
        similarity=similarity,
        evidence_chunk_id=1,
    )


def test_weights_are_strictly_ordered():
    assert REQUIRED_WEIGHT > PREFERRED_WEIGHT > MENTIONED_WEIGHT > 0


def test_required_beats_preferred_beats_mentioned_for_identical_similarity():
    """The acceptance criterion, at the formula level."""
    required = combine([_match(Importance.required, 0.8)], location_boosted=False)
    preferred = combine([_match(Importance.preferred, 0.8)], location_boosted=False)
    mentioned = combine([_match(Importance.mentioned, 0.8)], location_boosted=False)

    assert required > preferred > mentioned


def test_score_is_not_a_weighted_mean():
    """A weighted mean would normalise importance back out. Regression guard."""
    single_required = combine([_match(Importance.required, 1.0)], location_boosted=False)
    single_preferred = combine([_match(Importance.preferred, 1.0)], location_boosted=False)

    assert single_required == pytest.approx(1.0 - LOCATION_BOOST_WEIGHT)
    assert single_preferred == pytest.approx((1.0 - LOCATION_BOOST_WEIGHT) * PREFERRED_WEIGHT)


def test_score_stays_within_unit_interval():
    perfect_boosted = combine(
        [_match(Importance.required, 1.0), _match(Importance.required, 1.0, 2)],
        location_boosted=True,
    )
    empty = combine([], location_boosted=False)

    assert perfect_boosted == 1.0
    assert empty == 0.0


def test_location_boost_is_bounded_by_its_weight():
    plain = combine([_match(Importance.required, 0.5)], location_boosted=False)
    boosted = combine([_match(Importance.required, 0.5)], location_boosted=True)

    assert boosted - plain == pytest.approx(LOCATION_BOOST_WEIGHT)


def test_negative_cosine_contributes_nothing_rather_than_subtracting():
    assert clamp_similarity(-0.3) == 0.0
    assert clamp_similarity(1.4) == 1.0

    mixed = combine(
        [_match(Importance.required, 0.6), _match(Importance.required, 0.0, 2)],
        location_boosted=False,
    )
    alone = combine([_match(Importance.required, 0.6)], location_boosted=False)
    assert mixed < alone  # an unmatched requirement dilutes, it does not go negative


@pytest.mark.parametrize(
    ("locations", "expected"),
    [
        (["Detroit, MI"], True),
        (["Ann Arbor, MI"], True),
        (["New York, NY"], False),
        ([], False),
        (["Grand Rapids, Michigan"], True),
    ],
)
def test_has_boost_location(locations, expected):
    assert has_boost_location(locations, boost_terms=["Detroit", "Ann Arbor", "Michigan"]) is expected


def test_boost_terms_are_config_driven():
    assert has_boost_location(["Austin, TX"], boost_terms=["Austin"]) is True
    assert has_boost_location(["Austin, TX"], boost_terms=[]) is False


# --------------------------------------------------------------------------------------
# retrieval, against real pgvector
# --------------------------------------------------------------------------------------


@pytest.mark.integration
def test_nearest_chunk_picks_the_semantically_closest_live_chunk(
    db_session, add_chunk, provider
):
    python_chunk = add_chunk(PYTHON_EVIDENCE)
    add_chunk(KUBERNETES_EVIDENCE)

    hits = nearest_chunks(db_session, [(1, PYTHON_REQUIREMENT)], provider)

    assert hits[1].evidence_chunk_id == python_chunk.id
    assert hits[1].similarity > 0


@pytest.mark.integration
def test_soft_deleted_chunks_are_invisible(db_session, add_chunk, provider):
    add_chunk(PYTHON_EVIDENCE, deleted=True)
    live = add_chunk(KUBERNETES_EVIDENCE)

    hits = nearest_chunks(db_session, [(1, PYTHON_REQUIREMENT)], provider)

    assert hits[1].evidence_chunk_id == live.id


@pytest.mark.integration
def test_empty_corpus_scores_zero_rather_than_failing(db_session, provider):
    assert nearest_chunks(db_session, [(1, PYTHON_REQUIREMENT)], provider) == {}


@pytest.mark.integration
def test_required_match_outranks_the_same_skill_as_preferred(
    db_session, make_posting, add_requirement, add_chunk, settings, provider
):
    """Acceptance criterion, end to end: same skill, same evidence, same location."""
    add_chunk(PYTHON_EVIDENCE)
    hard = make_posting(company="HardCo")
    soft = make_posting(company="SoftCo")
    add_requirement(hard, PYTHON_REQUIREMENT, Importance.required)
    add_requirement(soft, PYTHON_REQUIREMENT, Importance.preferred)

    outcome = apply_filters(db_session, settings)
    scores = {s.posting_id: s for s in score_postings(db_session, outcome.kept, settings, provider)}

    assert scores[hard.id].coarse_score > scores[soft.id].coarse_score
    assert scores[hard.id].matches[0].evidence_chunk_id is not None
    # ...and the ranking agrees, not just the raw numbers
    ordered = [s.posting_id for s in score_postings(db_session, outcome.kept, settings, provider)]
    assert ordered.index(hard.id) < ordered.index(soft.id)


@pytest.mark.integration
def test_posting_with_no_requirements_falls_back_to_title_and_company(
    db_session, make_posting, add_chunk, settings, provider
):
    add_chunk(KUBERNETES_EVIDENCE)
    make_posting(title="Kubernetes Platform Engineer", company="EKS Corp")

    outcome = apply_filters(db_session, settings)
    (scored,) = score_postings(db_session, outcome.kept, settings, provider)

    assert scored.basis == "title_company"
    assert scored.low_confidence is True
    assert scored.requirement_count == 0
    assert scored.coarse_score > 0.0  # not silently pinned at the bottom


@pytest.mark.integration
def test_unmatched_requirements_still_produce_a_score(
    db_session, make_posting, add_requirement, add_chunk, settings, provider
):
    add_chunk(KUBERNETES_EVIDENCE)
    posting = make_posting()
    add_requirement(posting, "Fluency in Sumerian cuneiform", Importance.required)

    outcome = apply_filters(db_session, settings)
    (scored,) = score_postings(db_session, outcome.kept, settings, provider)

    assert scored.basis == "requirements"
    assert 0.0 <= scored.coarse_score <= 1.0


@pytest.mark.integration
def test_scores_are_sorted_with_ties_broken_on_posting_id(
    db_session, make_posting, add_requirement, add_chunk, settings, provider
):
    add_chunk(PYTHON_EVIDENCE)
    twins = [make_posting(company="Twin") for _ in range(4)]
    for twin in twins:
        add_requirement(twin, PYTHON_REQUIREMENT, Importance.required)

    outcome = apply_filters(db_session, settings)
    scored = score_postings(db_session, outcome.kept, settings, provider)

    assert len({s.coarse_score for s in scored}) == 1  # genuinely tied
    assert [s.posting_id for s in scored] == sorted(t.id for t in twins)
