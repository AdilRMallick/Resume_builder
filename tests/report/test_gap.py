"""The gap report against a seeded database with known counts."""

from __future__ import annotations

import json

import pytest

from jme.report.gap import (
    build_gap_report,
    coverage_report,
    explain_gap_query,
    gap_query_sql,
)
from jme.report.render import SCHEMA_VERSION, gap_report_to_dict, write_gap_json

from .conftest import (
    ELIGIBLE_POSTINGS,
    FALLBACK_FAILED,
    GREENHOUSE_RESOLVED,
    LEVER_RESOLVED,
    UNMAPPED_REQUIREMENTS,
)

pytestmark = pytest.mark.integration


def _by_name(items) -> dict:
    return {item.skill: item for item in items}


def test_ranked_gaps_match_the_hand_computed_order(db_session, seeded) -> None:
    report = build_gap_report(db_session)
    assert [g.skill for g in report.gaps] == seeded.expected_gap_order


def test_required_but_unevidenced_skill_ranks_first(db_session, seeded) -> None:
    """The whole point of the project: most-required skill with no evidence, on top."""
    report = build_gap_report(db_session)
    top = report.gaps[0]
    assert top.skill == "Kubernetes"
    assert top.required_count == 12
    assert top.status == "absent"
    # ... and its only citation lives on a stale match, which must not count at all
    assert top.matches_considered == 0
    assert top.evidenced_citations == 0


def test_non_actionable_skill_never_appears(db_session, seeded) -> None:
    """`is_actionable = false` is what keeps 'excellent communication' out of a report
    that is supposed to tell me what to go learn."""
    report = build_gap_report(db_session)
    names = {g.skill for g in report.gaps} | {c.skill for c in report.covered}
    assert "Excellent Communication" not in names
    # it is the single most-required skill in the fixture, so its absence is meaningful
    assert all(g.required_count <= 20 for g in report.gaps)


def test_mentioned_only_skill_does_not_outrank_a_required_skill(db_session, seeded) -> None:
    """Docker is `mentioned` on exactly the same 12 postings Kubernetes is `required` on.

    Identical posting_count, so any ranking that used posting_count - or total mentions -
    would tie them or put Docker first. required_count is the tiebreaker that matters.
    """
    report = build_gap_report(db_session)
    docker = _by_name(report.gaps)["Docker"]
    kubernetes = _by_name(report.gaps)["Kubernetes"]

    assert docker.posting_count == kubernetes.posting_count == 12
    assert docker.mentioned_count == 12
    assert docker.required_count == 0
    order = [g.skill for g in report.gaps]
    assert order.index("Kubernetes") < order.index("Docker")
    # and below every other required skill too, not just Kubernetes
    assert order.index("Docker") == len(order) - 1


def test_importance_histogram_and_posting_counts(db_session, seeded) -> None:
    gaps = _by_name(build_gap_report(db_session).gaps)
    kafka = gaps["Kafka"]
    assert (kafka.required_count, kafka.preferred_count, kafka.mentioned_count) == (4, 2, 0)
    assert kafka.posting_count == 6
    graphql = gaps["GraphQL"]
    assert (graphql.required_count, graphql.preferred_count) == (2, 5)
    assert graphql.posting_count == 7


def test_best_status_ever_wins_over_a_later_absent(db_session, seeded) -> None:
    """SQL is cited `absent` in one live match and `evidenced` in another.

    The rule is best-ever, because an `absent` citation is usually a retrieval miss while
    an `evidenced` one is constrained by the schema to carry a real chunk id.
    """
    report = build_gap_report(db_session)
    covered = _by_name(report.covered)
    assert set(covered) == seeded.expected_covered
    sql = covered["SQL"]
    assert sql.status == "evidenced"
    assert sql.evidenced_citations == 1
    assert sql.absent_citations == 1
    assert "SQL" not in {g.skill for g in report.gaps}


def test_weak_evidence_still_counts_as_a_gap(db_session, seeded) -> None:
    go = _by_name(build_gap_report(db_session).gaps)["Go"]
    assert go.status == "weak"
    assert go.weak_citations == 1
    assert go.absent_citations == 1
    assert go.matches_considered == 2


def test_absent_with_zero_matches_is_distinguishable_from_evaluated_absent(
    db_session, seeded
) -> None:
    gaps = _by_name(build_gap_report(db_session).gaps)
    assert gaps["Terraform"].status == "absent"
    assert gaps["Terraform"].matches_considered == 1  # looked at, found nothing
    assert gaps["Kafka"].matches_considered == 0  # never evaluated at all


def test_eligibility_filters_exclude_inactive_sponsorship_and_role_type(
    db_session, seeded
) -> None:
    report = build_gap_report(db_session)
    assert report.eligible_posting_count == ELIGIBLE_POSTINGS
    # each excluded posting carries one more `required` Kubernetes row; if any leaked in,
    # the count would be 13, 14 or 15
    assert _by_name(report.gaps)["Kubernetes"].required_count == 12

    unfiltered = build_gap_report(db_session, apply_eligibility=False)
    assert unfiltered.eligible_posting_count == seeded.active_postings
    assert _by_name(unfiltered.gaps)["Kubernetes"].required_count == 14  # inactive still out


def test_taxonomy_coverage_is_reported_honestly(db_session, seeded) -> None:
    report = build_gap_report(db_session)
    assert report.unmapped_requirement_count == UNMAPPED_REQUIREMENTS
    assert (
        report.mapped_requirement_count + report.unmapped_requirement_count
        == report.total_requirement_count
    )
    assert report.taxonomy_coverage == pytest.approx(
        report.mapped_requirement_count / report.total_requirement_count
    )
    assert 0.9 < report.taxonomy_coverage < 1.0


def test_evidence_version_comes_from_the_corpus(db_session, seeded) -> None:
    assert build_gap_report(db_session).evidence_version == 3


def test_empty_database_returns_an_empty_report_not_an_error(db_session) -> None:
    report = build_gap_report(db_session)
    assert report.gaps == []
    assert report.covered == []
    assert report.eligible_posting_count == 0
    assert report.taxonomy_coverage == 0.0


def test_json_export_shape_is_stable(db_session, seeded, tmp_path) -> None:
    report = build_gap_report(db_session)
    path = write_gap_json(report, tmp_path / "gap.json", top=3, include_covered=True)
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["kind"] == "gap_report"
    assert payload["generated_at"].endswith("+00:00")
    assert payload["evidence_version"] == 3
    assert payload["counts"]["eligible_postings"] == ELIGIBLE_POSTINGS
    assert payload["counts"]["requirements_unmapped"] == UNMAPPED_REQUIREMENTS
    assert payload["counts"]["gaps_returned"] == 3
    assert [g["skill"] for g in payload["gaps"]] == ["Kubernetes", "Go", "Terraform"]
    assert {c["skill"] for c in payload["covered"]} == {"Python", "SQL"}
    assert payload["gaps"][0]["is_gap"] is True
    assert set(payload["gaps"][0]) == {
        "canonical_skill_id", "skill", "category", "required_count", "preferred_count",
        "mentioned_count", "posting_count", "status", "evidenced_citations",
        "weak_citations", "absent_citations", "matches_considered", "is_gap",
    }


def test_json_export_omits_covered_unless_asked(db_session, seeded) -> None:
    report = build_gap_report(db_session)
    assert "covered" not in gap_report_to_dict(report, top=5)


def test_coverage_report_counts_adapters_and_taxonomy(db_session, seeded) -> None:
    cov = coverage_report(db_session)
    by_adapter = {a.adapter: a for a in cov.by_adapter}

    assert by_adapter["greenhouse"].resolved == GREENHOUSE_RESOLVED
    assert by_adapter["lever"].resolved == LEVER_RESOLVED
    assert by_adapter["fallback"].postings == FALLBACK_FAILED
    assert by_adapter["fallback"].resolved == 0
    assert by_adapter["fallback"].coverage == 0.0
    # postings with no jd row at all are still in the denominator - anything else
    # flatters the number
    assert cov.active_posting_count == seeded.active_postings
    assert cov.resolved_count == GREENHOUSE_RESOLVED + LEVER_RESOLVED
    assert cov.adapter_coverage == pytest.approx(
        (GREENHOUSE_RESOLVED + LEVER_RESOLVED) / seeded.active_postings
    )
    assert 0.0 < cov.taxonomy_coverage < 1.0


def test_explain_analyze_runs_and_mentions_the_rollup(db_session, seeded) -> None:
    plan = explain_gap_query(db_session)
    assert "Aggregate" in plan or "GroupAggregate" in plan or "HashAggregate" in plan
    assert "posting_requirement" in plan
    assert "actual time" in plan  # ANALYZE really ran


def test_gap_query_sql_is_a_single_statement(db_session) -> None:
    sql = gap_query_sql()
    assert sql.strip().startswith("WITH")
    # one statement: no internal semicolons at all
    assert ";" not in sql
