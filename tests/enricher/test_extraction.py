"""Extraction against a real Postgres, with a stubbed LLM layer."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from jme.enricher import extraction
from jme.enricher.extraction import extract_requirements
from jme.enricher.prompts import PROMPT_VERSION
from jme.models import CanonicalSkill, Importance, PostingRequirement, RunMetric, SkillCategory

pytestmark = pytest.mark.integration

JD = """\
Software Engineer, New Grad - Test Co

Requirements
- Proficiency in Python and experience with PostgreSQL.
- Familiarity with Kubernetes and container
  orchestration.
- Strong written communication skills.

Preferred
- Experience with Apache Kafka.
"""


def payload(*items: tuple[str, str, float]) -> dict:
    return {
        "requirements": [
            {"raw_text": raw, "importance": importance, "confidence": confidence}
            for raw, importance, confidence in items
        ]
    }


GOOD = payload(
    ("Python", "required", 0.95),
    ("PostgreSQL", "required", 0.9),
    ("Kubernetes", "required", 0.85),
    ("Strong written communication skills", "required", 0.8),
    ("Apache Kafka", "preferred", 0.75),
)


def _rows(session, posting_id: int) -> list[PostingRequirement]:
    return list(
        session.scalars(
            select(PostingRequirement).where(PostingRequirement.posting_id == posting_id)
        )
    )


def _metric(session, run_id: str, name: str) -> float:
    return float(
        session.scalar(
            select(func.coalesce(func.sum(RunMetric.value), 0)).where(
                RunMetric.run_id == run_id,
                RunMetric.stage == "extraction",
                RunMetric.metric == name,
            )
        )
    )


# --------------------------------------------------------------------------------------


def test_persists_verbatim_rows_with_prompt_version_and_model(
    db_session, make_posting, stub_llm, no_resolver
):
    posting_id = make_posting(db_session, JD)
    llm = stub_llm([GOOD])

    rows = extract_requirements(
        db_session, posting_id, JD, run_id="run-basic", resolver=no_resolver, llm_call=llm
    )

    assert len(rows) == 5
    assert llm.call_count == 1
    for row in rows:
        assert row.prompt_version == PROMPT_VERSION
        assert row.model_id == "stub-model-v1"
        assert row.raw_text in JD, "every stored span must be findable in the JD"
    assert {row.importance for row in rows} == {Importance.required, Importance.preferred}


def test_call_uses_the_extraction_kind_and_prompt_version(
    db_session, make_posting, stub_llm, no_resolver
):
    posting_id = make_posting(db_session, JD)
    llm = stub_llm([GOOD])
    extract_requirements(db_session, posting_id, JD, resolver=no_resolver, llm_call=llm)

    call = llm.calls[0]
    assert call["kind"] == "extraction"
    assert call["prompt_version"] == PROMPT_VERSION
    assert JD in call["user"]  # the JD is quotable source text inside the prompt


def test_module_seam_can_be_monkeypatched(
    db_session, make_posting, stub_llm, no_resolver, monkeypatch
):
    """The alternative to injection: patch one clearly-named seam."""
    posting_id = make_posting(db_session, JD)
    llm = stub_llm([GOOD])
    monkeypatch.setattr(extraction, "call_llm", llm)

    rows = extract_requirements(db_session, posting_id, JD, resolver=no_resolver)
    assert len(rows) == 5
    assert llm.call_count == 1


# -- verbatim enforcement -------------------------------------------------------------


def test_paraphrase_triggers_exactly_one_retry_then_is_dropped(
    db_session, make_posting, stub_llm, no_resolver
):
    posting_id = make_posting(db_session, JD)
    paraphrased = payload(
        ("Python", "required", 0.95),
        ("5+ years of experience with distributed systems", "required", 0.9),  # invented
        ("experience with container orchestration tools", "preferred", 0.7),  # reworded
    )
    # The model does not fix itself on the retry either.
    llm = stub_llm([paraphrased, paraphrased])

    rows = extract_requirements(
        db_session, posting_id, JD, run_id="run-paraphrase", resolver=no_resolver, llm_call=llm
    )

    assert llm.call_count == 2, "exactly one corrective retry, not zero and not a loop"
    assert [row.raw_text for row in rows] == ["Python"]
    assert _metric(db_session, "run-paraphrase", "paraphrase_rejected") == 2
    assert _metric(db_session, "run-paraphrase", "requirements_found") == 1

    stored = {row.raw_text for row in _rows(db_session, posting_id)}
    assert "5+ years of experience with distributed systems" not in stored
    assert "experience with container orchestration tools" not in stored


def test_retry_prompt_names_the_offending_spans(
    db_session, make_posting, stub_llm, no_resolver
):
    posting_id = make_posting(db_session, JD)
    bad = payload(("familiarity with orchestration systems", "preferred", 0.7))
    llm = stub_llm([bad, GOOD])

    extract_requirements(db_session, posting_id, JD, resolver=no_resolver, llm_call=llm)

    retry_prompt = llm.calls[1]["user"]
    assert "familiarity with orchestration systems" in retry_prompt
    assert "CORRECTION" in retry_prompt


def test_retry_that_returns_verbatim_spans_is_stored(
    db_session, make_posting, stub_llm, no_resolver
):
    posting_id = make_posting(db_session, JD)
    bad = payload(("must know Kubernetes well", "required", 0.9))
    llm = stub_llm([bad, GOOD])

    rows = extract_requirements(
        db_session, posting_id, JD, run_id="run-recover", resolver=no_resolver, llm_call=llm
    )

    assert llm.call_count == 2
    assert len(rows) == 5
    assert _metric(db_session, "run-recover", "paraphrase_rejected") == 0


def test_whitespace_reflow_is_not_treated_as_paraphrase(
    db_session, make_posting, stub_llm, no_resolver
):
    posting_id = make_posting(db_session, JD)
    # The model joined a wrapped bullet onto one line. That is reflow, not paraphrase.
    llm = stub_llm([payload(("Familiarity with Kubernetes and container orchestration",
                            "required", 0.9))])

    rows = extract_requirements(db_session, posting_id, JD, resolver=no_resolver, llm_call=llm)

    assert llm.call_count == 1, "a reflowed span must not cost a retry"
    # stored as the JD wrote it, line break and all, so raw_text stays ctrl-F findable
    assert rows[0].raw_text == "Familiarity with Kubernetes and container\n  orchestration"
    assert rows[0].raw_text in JD


def test_no_persisted_row_is_missing_prompt_version(
    db_session, make_posting, stub_llm, no_resolver, goldens
):
    """Assert over every row this test's extractions produced, not just the returned ones."""
    for case in goldens:
        posting_id = make_posting(db_session, case.jd_text, company=case.company)
        extract_requirements(
            db_session,
            posting_id,
            case.jd_text,
            run_id="run-version",
            resolver=no_resolver,
            llm_call=stub_llm([case.llm_payload]),
        )

    versions = set(db_session.scalars(select(PostingRequirement.prompt_version)))
    assert versions == {PROMPT_VERSION}
    missing = db_session.scalar(
        select(func.count())
        .select_from(PostingRequirement)
        .where(
            (PostingRequirement.prompt_version.is_(None))
            | (PostingRequirement.prompt_version == "")
        )
    )
    assert missing == 0


# -- idempotency ----------------------------------------------------------------------


def test_rerunning_the_same_posting_does_not_duplicate_rows(
    db_session, make_posting, stub_llm, no_resolver
):
    posting_id = make_posting(db_session, JD)

    first = extract_requirements(
        db_session, posting_id, JD, resolver=no_resolver, llm_call=stub_llm([GOOD])
    )
    second = extract_requirements(
        db_session, posting_id, JD, resolver=no_resolver, llm_call=stub_llm([GOOD])
    )

    assert len(_rows(db_session, posting_id)) == len(first) == len(second) == 5
    assert {row.id for row in first} == {row.id for row in second}, "upsert, not delete+insert"


def test_rerun_updates_importance_and_confidence_in_place(
    db_session, make_posting, stub_llm, no_resolver
):
    posting_id = make_posting(db_session, JD)
    extract_requirements(
        db_session,
        posting_id,
        JD,
        resolver=no_resolver,
        llm_call=stub_llm([payload(("Apache Kafka", "mentioned", 0.4))]),
    )
    extract_requirements(
        db_session,
        posting_id,
        JD,
        resolver=no_resolver,
        llm_call=stub_llm([payload(("Apache Kafka", "preferred", 0.9))]),
    )

    rows = _rows(db_session, posting_id)
    assert len(rows) == 1
    assert rows[0].importance is Importance.preferred
    assert float(rows[0].confidence) == pytest.approx(0.9)


def test_duplicate_spans_in_one_response_collapse_to_one_row(
    db_session, make_posting, stub_llm, no_resolver
):
    posting_id = make_posting(db_session, JD)
    rows = extract_requirements(
        db_session,
        posting_id,
        JD,
        resolver=no_resolver,
        llm_call=stub_llm(
            [payload(("Python", "mentioned", 0.5), ("Python", "required", 0.95))]
        ),
    )
    assert len(rows) == 1
    assert rows[0].importance is Importance.required


# -- taxonomy boundary ----------------------------------------------------------------


def test_resolver_attaches_canonical_skill_id_and_none_is_legitimate(
    db_session, make_posting, stub_llm
):
    skill = CanonicalSkill(name="Python", category=SkillCategory.language)
    db_session.add(skill)
    db_session.flush()

    def resolver(session, raw_text, posting_id=None):
        return skill.id if raw_text == "Python" else None

    posting_id = make_posting(db_session, JD)
    rows = extract_requirements(
        db_session, posting_id, JD, run_id="run-resolve", resolver=resolver,
        llm_call=stub_llm([GOOD]),
    )

    by_text = {row.raw_text: row for row in rows}
    assert by_text["Python"].canonical_skill_id == skill.id
    assert by_text["Apache Kafka"].canonical_skill_id is None
    assert _metric(db_session, "run-resolve", "requirements_resolved") == 1
    assert _metric(db_session, "run-resolve", "requirements_unresolved") == 4


def test_resolver_failure_on_one_span_does_not_lose_the_posting(
    db_session, make_posting, stub_llm
):
    def exploding(session, raw_text, posting_id=None):
        if raw_text == "PostgreSQL":
            raise RuntimeError("taxonomy is down")
        return None

    posting_id = make_posting(db_session, JD)
    rows = extract_requirements(
        db_session, posting_id, JD, resolver=exploding, llm_call=stub_llm([GOOD])
    )
    assert len(rows) == 5
    assert all(row.canonical_skill_id is None for row in rows)


def test_default_resolver_uses_the_taxonomy_module_when_present(
    db_session, make_posting, stub_llm, monkeypatch
):
    """No resolver injected: extraction should reach for jme.taxonomy.resolver.resolve."""
    import sys
    import types

    seen: list[str] = []

    module = types.ModuleType("jme.taxonomy.resolver")
    module.resolve = lambda session, raw_text, posting_id=None: (  # type: ignore[attr-defined]
        seen.append(raw_text) or (7 if raw_text == "Python" else None)
    )
    package = types.ModuleType("jme.taxonomy")
    monkeypatch.setitem(sys.modules, "jme.taxonomy", package)
    monkeypatch.setitem(sys.modules, "jme.taxonomy.resolver", module)

    skill = CanonicalSkill(id=7, name="Python-for-test", category=SkillCategory.language)
    db_session.add(skill)
    db_session.flush()

    posting_id = make_posting(db_session, JD)
    rows = extract_requirements(db_session, posting_id, JD, llm_call=stub_llm([GOOD]))

    assert "Python" in seen and len(seen) == 5
    assert {row.raw_text: row.canonical_skill_id for row in rows}["Python"] == 7


def test_missing_taxonomy_module_is_survivable(db_session, make_posting, stub_llm, monkeypatch):
    """The taxonomy lands in parallel. Extraction must run before it exists."""
    import sys

    # None in sys.modules is the documented way to make an import fail on demand.
    monkeypatch.setitem(sys.modules, "jme.taxonomy", None)
    monkeypatch.setitem(sys.modules, "jme.taxonomy.resolver", None)

    posting_id = make_posting(db_session, JD)
    rows = extract_requirements(
        db_session, posting_id, JD, llm_call=stub_llm([GOOD])
    )  # no resolver injected: falls through to the lazy import
    assert len(rows) == 5
    assert all(row.canonical_skill_id is None for row in rows)


# -- metrics and guards ---------------------------------------------------------------


def test_metrics_land_for_the_posting(db_session, make_posting, stub_llm, no_resolver):
    posting_id = make_posting(db_session, JD)
    extract_requirements(
        db_session, posting_id, JD, run_id="run-metrics", resolver=no_resolver,
        llm_call=stub_llm([GOOD]),
    )

    assert _metric(db_session, "run-metrics", "requirements_found") == 5
    assert _metric(db_session, "run-metrics", "paraphrase_rejected") == 0
    # cost accounting from the (stubbed) llm layer, same stage
    assert _metric(db_session, "run-metrics", "input_tokens") == 1200
    assert _metric(db_session, "run-metrics", "cost_usd") == pytest.approx(0.0123)

    labels = db_session.scalars(
        select(RunMetric.labels).where(
            RunMetric.run_id == "run-metrics", RunMetric.metric == "requirements_found"
        )
    ).all()
    assert labels == [{"posting_id": posting_id}]


def test_empty_jd_text_is_rejected(db_session, make_posting, stub_llm, no_resolver):
    posting_id = make_posting(db_session, "")
    with pytest.raises(ValueError, match="no job description text"):
        extract_requirements(
            db_session, posting_id, "   \n ", resolver=no_resolver, llm_call=stub_llm([GOOD])
        )


def test_run_id_is_generated_when_not_supplied(db_session, make_posting, stub_llm, no_resolver):
    posting_id = make_posting(db_session, JD)
    extract_requirements(db_session, posting_id, JD, resolver=no_resolver, llm_call=stub_llm([GOOD]))
    generated = db_session.scalars(
        select(RunMetric.run_id).where(RunMetric.metric == "requirements_found")
    ).all()
    assert any(run_id.startswith("extract-") for run_id in generated)
