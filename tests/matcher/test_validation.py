"""Citation validation, in isolation. No database, no model, no network.

These are the rules that keep a fabricated citation out of Postgres, so they are tested
against a hand-built RetrievalSet rather than through the full pipeline.
"""

from __future__ import annotations

import pytest

from jme.matcher.match import (
    FabricatedCitationError,
    MalformedResponseError,
    MatchValidationError,
    MissingCitationError,
    compute_score,
    validate_response,
)
from jme.matcher.retrieval import RequirementView, RetrievalSet, RetrievedChunk
from jme.models import CitationStatus, Importance, Verdict


def make_retrieval() -> RetrievalSet:
    requirements = [
        RequirementView(1, 10, "Go", "Strong Go experience", Importance.required),
        RequirementView(2, 11, "PostgreSQL", "Production Postgres", Importance.required),
        RequirementView(3, 12, "Kubernetes", "Kubernetes a plus", Importance.preferred),
    ]
    chunks = [
        RetrievedChunk(101, "markdown", "a.md", "Go", "wrote a Go service", 0.2, False),
        RetrievedChunk(102, "markdown", "b.md", "DB", "designed a Postgres schema", 0.3, True),
    ]
    return RetrievalSet(
        posting_id=1,
        evidence_version=1,
        top_k=3,
        requirements=requirements,
        chunks=chunks,
        per_requirement={1: [101], 2: [102], 3: [101, 102]},
    )


def row(req_id, status, chunk_id, skill=None):
    return {
        "requirement_id": req_id,
        "canonical_skill_id": skill,
        "status": status,
        "evidence_chunk_id": chunk_id,
        "reasoning": "because",
    }


def response(rows, verdict="plausible"):
    return {"requirements": rows, "verdict": verdict, "rationale": "short rationale"}


def test_happy_path_maps_every_requirement():
    retrieval = make_retrieval()
    validated = validate_response(
        response(
            [
                row(1, "evidenced", 101),
                row(2, "evidenced", 102),
                row(3, "absent", None),
            ]
        ),
        retrieval,
    )

    assert [c.requirement_id for c in validated.citations] == [1, 2, 3]
    assert [c.status for c in validated.citations] == [
        CitationStatus.evidenced,
        CitationStatus.evidenced,
        CitationStatus.absent,
    ]
    # the canonical skill comes from the database, never from the model
    assert [c.canonical_skill_id for c in validated.citations] == [10, 11, 12]
    assert validated.verdict is Verdict.plausible
    assert validated.unaddressed == []


def test_evidenced_without_a_chunk_id_is_rejected():
    with pytest.raises(MissingCitationError) as exc:
        validate_response(response([row(1, "evidenced", None)]), make_retrieval())
    assert "requires an evidence_chunk_id" in str(exc.value)
    assert isinstance(exc.value, MatchValidationError)


def test_evidenced_with_an_unknown_chunk_id_is_rejected():
    with pytest.raises(FabricatedCitationError) as exc:
        validate_response(response([row(1, "evidenced", 999_999)]), make_retrieval())
    assert "999999" in str(exc.value)


def test_fabrication_outranks_a_missing_citation_in_the_error_type():
    """Both problems in one response: the fabrication is the one worth naming."""
    with pytest.raises(FabricatedCitationError):
        validate_response(
            response([row(1, "evidenced", None), row(2, "evidenced", 424_242)]),
            make_retrieval(),
        )


def test_weak_row_citing_an_unknown_chunk_has_the_citation_dropped():
    validated = validate_response(
        response([row(1, "weak", 999), row(2, "absent", None), row(3, "weak", 102)]),
        make_retrieval(),
    )
    by_req = {c.requirement_id: c for c in validated.citations}
    assert by_req[1].evidence_chunk_id is None
    assert by_req[3].evidence_chunk_id == 102
    assert validated.dropped_citations == 1


def test_unknown_requirement_id_is_malformed():
    with pytest.raises(MalformedResponseError):
        validate_response(response([row(4242, "absent", None)]), make_retrieval())


def test_duplicate_requirement_ids_are_rejected():
    with pytest.raises(MalformedResponseError):
        validate_response(
            response([row(1, "absent", None), row(1, "weak", 101)]), make_retrieval()
        )


def test_bad_status_and_bad_verdict_are_malformed():
    with pytest.raises(MalformedResponseError):
        validate_response(response([row(1, "maybe", None)]), make_retrieval())
    with pytest.raises(MalformedResponseError):
        validate_response(
            response([row(1, "absent", None)], verdict="excellent"), make_retrieval()
        )


def test_missing_requirements_array_is_malformed():
    with pytest.raises(MalformedResponseError):
        validate_response({"verdict": "no", "rationale": "x"}, make_retrieval())


def test_unaddressed_requirements_become_absent():
    validated = validate_response(response([row(1, "evidenced", 101)]), make_retrieval())
    assert validated.unaddressed == [2, 3]
    assert len(validated.citations) == 3
    assert all(
        c.status is CitationStatus.absent for c in validated.citations if c.requirement_id != 1
    )


def test_score_is_importance_weighted():
    retrieval = make_retrieval()
    # required Go evidenced (1.0*1.0), required Postgres absent (1.0*0), preferred k8s weak (.5*.5)
    validated = validate_response(
        response([row(1, "evidenced", 101), row(2, "absent", None), row(3, "weak", None)]),
        retrieval,
    )
    assert compute_score(validated, retrieval) == pytest.approx((1.0 + 0.0 + 0.25) / 2.5)

    everything = validate_response(
        response([row(1, "evidenced", 101), row(2, "evidenced", 102), row(3, "evidenced", 101)]),
        retrieval,
    )
    assert compute_score(everything, retrieval) == 1.0
