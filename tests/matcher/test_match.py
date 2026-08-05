"""End-to-end matching against a real Postgres, with the model stubbed out.

The acceptance criteria for Task 9 live here: a fabricated citation is rejected after
exactly one retry and leaves nothing behind, an `evidenced` row with no chunk id never
reaches the check constraint, cost is recorded and the ceiling is detected, re-matching
is served from cache, and bumping evidence_version writes a new row rather than
overwriting the old one.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from jme.config import get_settings
from jme.llm import LLMResult, sha256
from jme.matcher import match as match_module
from jme.matcher.match import (
    CostCeilingExceeded,
    FabricatedCitationError,
    MissingCitationError,
    NoRequirementsError,
    RunBudgetExceeded,
    match_posting,
    match_posting_detailed,
    match_shortlist,
    recompute_stale,
)
from jme.matcher.prompts import PROMPT_VERSION
from jme.matcher.retrieval import retrieve_for_posting
from jme.models import (
    CitationStatus,
    Match,
    MatchCitation,
    PostingRequirement,
    RunMetric,
    Verdict,
)
from tests.matcher.conftest import (
    JD_TEXT,
    MODEL_ID,
    add_shortlist,
    make_posting,
    make_requirements,
    payload,
    set_evidence_version,
)

pytestmark = pytest.mark.integration


def count(session, model) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


def metric(session, run_id: str, name: str) -> float:
    return float(
        session.scalar(
            select(func.coalesce(func.sum(RunMetric.value), 0)).where(
                RunMetric.run_id == run_id,
                RunMetric.stage == "match",
                RunMetric.metric == name,
            )
        )
        or 0
    )


# --------------------------------------------------------------------------------------
# happy path
# --------------------------------------------------------------------------------------


def test_match_writes_row_and_citations(db_session, seed, install_stub):
    stub = install_stub([payload(seed)])

    match = match_posting(db_session, seed.posting_id, run_id="run-happy")

    assert stub.call_count == 1
    assert match.posting_id == seed.posting_id
    assert match.evidence_version == 1
    assert match.prompt_version == PROMPT_VERSION
    assert match.model_id == MODEL_ID
    assert match.jd_sha256 == sha256(JD_TEXT)
    assert match.is_stale is False
    assert match.verdict is Verdict.plausible
    # required evidenced + required evidenced + preferred weak -> (1 + 1 + 0.25) / 2.5
    assert float(match.score) == pytest.approx(0.9)
    assert match.rationale["prompt_version"] == PROMPT_VERSION
    assert match.rationale["retried"] is False
    assert match.rationale["jd_present"] is True

    citations = db_session.scalars(
        select(MatchCitation).where(MatchCitation.match_id == match.id)
    ).all()
    assert len(citations) == len(seed.requirements)
    assert {c.status for c in citations} == {CitationStatus.evidenced, CitationStatus.weak}
    evidenced = [c for c in citations if c.status is CitationStatus.evidenced]
    assert all(c.evidence_chunk_id in set(seed.chunk_ids) for c in evidenced)
    assert {c.canonical_skill_id for c in citations} == {
        r.canonical_skill_id for r in seed.requirements
    }


def test_cost_and_tokens_are_recorded(db_session, seed, install_stub):
    install_stub([payload(seed)], cost_usd=0.0123, input_tokens=4000, output_tokens=900)

    match = match_posting(db_session, seed.posting_id, run_id="run-cost")

    assert float(match.cost_usd) == pytest.approx(0.0123)
    assert match.input_tokens == 4000
    assert match.output_tokens == 900
    assert metric(db_session, "run-cost", "match_cost_usd") == pytest.approx(0.0123)
    assert metric(db_session, "run-cost", "matched") == 1
    assert metric(db_session, "run-cost", "citation_evidenced") == 2


def test_cache_key_parts_carry_jd_hash_and_evidence_version(db_session, seed, install_stub):
    stub = install_stub([payload(seed)])
    match_posting(db_session, seed.posting_id, run_id="run-key")

    parts = stub.calls[0].extra_key_parts
    assert f"jd_sha256={sha256(JD_TEXT)}" in parts
    assert "evidence_version=1" in parts
    assert stub.calls[0].force_refresh is False


def test_call_llm_delegates_to_structured_call_with_prompt_version(db_session, seed, monkeypatch):
    """The seam itself: prompt_version, schema and kind must reach jme.llm.structured_call,
    which is what folds prompt_version and model_id into the cache key."""
    seen = {}

    def fake_structured_call(session, **kwargs):
        seen.update(kwargs)
        return LLMResult(payload(seed), 10, 5, 0.001, cached=False, model=MODEL_ID)

    monkeypatch.setattr(match_module, "structured_call", fake_structured_call)
    match_posting(db_session, seed.posting_id, run_id="run-seam")

    assert seen["kind"] == "match"
    assert seen["prompt_version"] == PROMPT_VERSION
    assert seen["schema"]["properties"]["requirements"]["items"]["required"] == [
        "requirement_id",
        "canonical_skill_id",
        "status",
        "evidence_chunk_id",
        "reasoning",
    ]
    assert any(part.startswith("evidence_version=") for part in seen["extra_key_parts"])
    assert "CHUNK" in seen["user"] and "REQ" in seen["user"]


# --------------------------------------------------------------------------------------
# citation validation, the non-negotiable part
# --------------------------------------------------------------------------------------


def test_fabricated_citation_is_rejected_after_exactly_one_retry(db_session, seed, install_stub):
    fabricated = payload(
        seed,
        statuses=["evidenced", "evidenced", "absent"],
        chunk_ids=[seed.chunks[0].id, 987_654_321, None],
    )
    stub = install_stub([fabricated])  # the same lie twice

    with pytest.raises(FabricatedCitationError) as exc:
        match_posting(db_session, seed.posting_id, run_id="run-fab")

    assert "987654321" in str(exc.value)
    assert stub.call_count == 2, "expected exactly one retry"

    # the retry must have been corrective, and must have listed the ids that are allowed
    retry_prompt = stub.calls[1].user
    assert "CORRECTION" in retry_prompt
    assert "987654321" in retry_prompt
    valid_ids = retrieve_for_posting(db_session, seed.posting_id).valid_chunk_ids
    assert valid_ids
    for chunk_id in sorted(valid_ids):
        assert str(chunk_id) in retry_prompt

    # nothing persisted
    assert count(db_session, Match) == 0
    assert count(db_session, MatchCitation) == 0
    assert metric(db_session, "run-fab", "fabricated_citation") == 1
    assert metric(db_session, "run-fab", "citation_retry") == 1
    assert metric(db_session, "run-fab", "matched") == 0


def test_evidenced_with_null_chunk_id_never_reaches_postgres(db_session, seed, install_stub):
    stub = install_stub(
        [payload(seed, statuses=["evidenced", "absent", "absent"], chunk_ids=[None, None, None])]
    )

    with pytest.raises(MissingCitationError):
        match_posting(db_session, seed.posting_id, run_id="run-null")

    assert stub.call_count == 2
    assert count(db_session, Match) == 0
    assert count(db_session, MatchCitation) == 0
    assert metric(db_session, "run-null", "missing_citation") == 1


def test_the_database_would_have_rejected_it_too(db_session, seed):
    """Proof that ck_evidenced_requires_chunk is a real backstop - and that application
    validation is what actually fires, since the test above never got here."""
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            match = Match(
                posting_id=seed.posting_id,
                evidence_version=1,
                prompt_version=PROMPT_VERSION,
                model_id=MODEL_ID,
                jd_sha256=sha256(JD_TEXT),
            )
            db_session.add(match)
            db_session.flush()
            db_session.add(
                MatchCitation(
                    match_id=match.id,
                    canonical_skill_id=None,
                    evidence_chunk_id=None,
                    status=CitationStatus.evidenced,
                    reasoning="claims evidence, cites nothing",
                )
            )
            db_session.flush()


def test_retry_recovers_when_the_second_answer_is_clean(db_session, seed, install_stub):
    bad = payload(seed, chunk_ids=[424_242, seed.chunks[1].id, seed.chunks[2].id])
    good = payload(seed)
    stub = install_stub([bad, good])

    outcome = match_posting_detailed(db_session, seed.posting_id, run_id="run-retry")

    assert stub.call_count == 2
    assert outcome.retried is True
    assert outcome.match.rationale["retried"] is True
    assert outcome.cost_usd == pytest.approx(stub.cost_usd * 2), "both calls are paid for"
    assert count(db_session, Match) == 1
    assert count(db_session, MatchCitation) == len(seed.requirements)
    assert metric(db_session, "run-retry", "fabricated_citation") == 0


def test_a_rejected_response_is_never_written_to_the_llm_cache(db_session, seed, monkeypatch):
    """The real structured_call caches every live answer. A rejected answer must not
    survive, otherwise the fabrication would be replayed forever."""
    from jme.models import LLMCache

    calls = {"n": 0}

    def fake_structured_call(session, **kwargs):
        calls["n"] += 1
        session.merge(
            LLMCache(
                cache_key=f"poison-{calls['n']}",
                kind="match",
                payload={"x": 1},
                input_tokens=1,
                output_tokens=1,
                cost_usd=0.001,
            )
        )
        return LLMResult(
            payload(seed, chunk_ids=[seed.chunks[0].id, 777_777, None]),
            10,
            5,
            0.001,
            cached=False,
            model=MODEL_ID,
        )

    monkeypatch.setattr(match_module, "structured_call", fake_structured_call)

    with pytest.raises(FabricatedCitationError):
        match_posting(db_session, seed.posting_id, run_id="run-poison")

    assert calls["n"] == 2
    assert count(db_session, LLMCache) == 0


# --------------------------------------------------------------------------------------
# cost control
# --------------------------------------------------------------------------------------


def test_cost_ceiling_warns_by_default(db_session, seed, install_stub, monkeypatch):
    monkeypatch.setattr(get_settings(), "max_cost_per_match_usd", 0.001)
    install_stub([payload(seed)], cost_usd=0.25)

    match = match_posting(db_session, seed.posting_id, run_id="run-ceiling")

    assert float(match.cost_usd) == pytest.approx(0.25)
    assert metric(db_session, "run-ceiling", "cost_ceiling_exceeded") == 1
    assert count(db_session, Match) == 1


def test_cost_ceiling_can_abort(db_session, seed, install_stub, monkeypatch):
    monkeypatch.setattr(get_settings(), "max_cost_per_match_usd", 0.001)
    install_stub([payload(seed)], cost_usd=0.25)

    with pytest.raises(CostCeilingExceeded):
        match_posting(db_session, seed.posting_id, run_id="run-abort", cost_policy="abort")

    # we already paid for the answer, so it is persisted; the run is what stops
    assert count(db_session, Match) == 1
    assert metric(db_session, "run-abort", "cost_ceiling_exceeded") == 1


def test_under_ceiling_records_no_breach(db_session, seed, install_stub, monkeypatch):
    monkeypatch.setattr(get_settings(), "max_cost_per_match_usd", 0.05)
    install_stub([payload(seed)], cost_usd=0.004)
    match_posting(db_session, seed.posting_id, run_id="run-ok")
    assert metric(db_session, "run-ok", "cost_ceiling_exceeded") == 0


# --------------------------------------------------------------------------------------
# caching, upsert, history
# --------------------------------------------------------------------------------------


def test_rematching_is_served_from_cache_and_does_not_duplicate(db_session, seed, install_stub):
    stub = install_stub([payload(seed)])

    first = match_posting_detailed(db_session, seed.posting_id, run_id="run-cache")
    second = match_posting_detailed(db_session, seed.posting_id, run_id="run-cache")

    assert stub.call_count == 1, "the second match must not call the model"
    assert second.from_match_cache is True
    assert second.match.id == first.match.id
    assert count(db_session, Match) == 1
    assert count(db_session, MatchCitation) == len(seed.requirements)
    assert metric(db_session, "run-cache", "match_row_cache_hit") == 1


def test_force_recomputes_in_place(db_session, seed, install_stub):
    stub = install_stub([payload(seed), payload(seed, verdict="strong")])

    first = match_posting(db_session, seed.posting_id, run_id="run-force")
    second = match_posting(db_session, seed.posting_id, run_id="run-force", force=True)

    assert stub.call_count == 2
    assert stub.calls[1].force_refresh is True, "force must bypass the llm cache too"
    assert second.id == first.id
    assert second.verdict is Verdict.strong
    assert count(db_session, Match) == 1
    assert count(db_session, MatchCitation) == len(seed.requirements)


def test_bumping_evidence_version_creates_a_new_row_and_keeps_history(
    db_session, seed, install_stub
):
    stub = install_stub([payload(seed), payload(seed, verdict="strong")])

    old = match_posting(db_session, seed.posting_id, run_id="run-v1")
    old_id = old.id

    set_evidence_version(db_session, 2)
    db_session.commit()

    new = match_posting(db_session, seed.posting_id, run_id="run-v2")

    assert stub.call_count == 2
    assert new.id != old_id
    assert count(db_session, Match) == 2
    assert count(db_session, MatchCitation) == 2 * len(seed.requirements)

    rows = db_session.scalars(
        select(Match).where(Match.posting_id == seed.posting_id).order_by(Match.id)
    ).all()
    assert [r.evidence_version for r in rows] == [1, 2]
    assert rows[0].verdict is Verdict.plausible, "history is untouched"
    assert rows[1].verdict is Verdict.strong


def test_a_stale_row_is_recomputed_in_place(db_session, seed, install_stub):
    stub = install_stub([payload(seed), payload(seed, verdict="strong")])

    first = match_posting(db_session, seed.posting_id, run_id="run-stale")
    first.is_stale = True
    db_session.commit()

    second = match_posting(db_session, seed.posting_id, run_id="run-stale")

    assert stub.call_count == 2
    assert second.id == first.id
    assert second.is_stale is False
    assert count(db_session, Match) == 1


def test_posting_without_requirements_is_a_typed_error(db_session, seed, install_stub):
    stub = install_stub([payload(seed)])
    db_session.execute(
        PostingRequirement.__table__.delete().where(
            PostingRequirement.posting_id == seed.posting_id
        )
    )
    db_session.flush()

    with pytest.raises(NoRequirementsError):
        match_posting(db_session, seed.posting_id, run_id="run-empty")
    assert stub.call_count == 0
    assert count(db_session, Match) == 0


# --------------------------------------------------------------------------------------
# batches
# --------------------------------------------------------------------------------------


def _second_posting(db_session, seed):
    other = make_posting(db_session, key="acme-swe-2", company="Globex")
    make_requirements(db_session, other, seed.skills)
    db_session.flush()
    return other


def test_match_shortlist_matches_every_posting(db_session, seed, install_stub):
    other = _second_posting(db_session, seed)
    add_shortlist(db_session, "sl-1", [seed.posting_id, other.id])

    # requirement ids differ per posting, so queue one payload per posting
    stub = install_stub([payload(seed)], cost_usd=0.002)
    stub.payloads = [payload(seed), _payload_for(db_session, other)]

    seen: list[int] = []
    report = match_shortlist(
        db_session,
        "sl-1",
        on_progress=lambda i, total, posting_id, outcome, err: seen.append(posting_id),
    )

    assert report.total == 2
    assert report.matched == 2
    assert report.failed == 0
    assert seen == [seed.posting_id, other.id]
    assert report.cost_usd == pytest.approx(0.004)
    assert count(db_session, Match) == 2
    assert metric(db_session, "sl-1", "postings_matched") == 2


def test_match_shortlist_resolves_the_latest_run(db_session, seed, install_stub):
    add_shortlist(db_session, "sl-latest", [seed.posting_id])
    install_stub([payload(seed)])
    report = match_shortlist(db_session)
    assert report.run_id == "sl-latest"
    assert report.matched == 1


def test_run_budget_aborts_the_batch(db_session, seed, install_stub):
    other = _second_posting(db_session, seed)
    add_shortlist(db_session, "sl-budget", [seed.posting_id, other.id])
    stub = install_stub([payload(seed)], cost_usd=0.03)
    stub.payloads = [payload(seed), _payload_for(db_session, other)]

    with pytest.raises(RunBudgetExceeded):
        match_shortlist(db_session, "sl-budget", run_budget_usd=0.01)

    assert stub.call_count == 1, "the batch stops at the first posting over budget"
    assert count(db_session, Match) == 1
    assert metric(db_session, "sl-budget", "run_budget_exceeded") == 1


def test_shortlist_skips_postings_without_requirements(db_session, seed, install_stub):
    other = make_posting(db_session, key="acme-swe-3", company="Initech")
    db_session.flush()
    add_shortlist(db_session, "sl-skip", [seed.posting_id, other.id])
    install_stub([payload(seed)])

    report = match_shortlist(db_session, "sl-skip")

    assert report.matched == 1
    assert report.skipped == 1
    assert report.errors and report.errors[0][0] == other.id


def test_recompute_stale_is_bounded_and_ignores_inactive_postings(db_session, seed, install_stub):
    other = _second_posting(db_session, seed)
    stub = install_stub([payload(seed)])
    stub.payloads = [payload(seed), _payload_for(db_session, other)]

    first = match_posting(db_session, seed.posting_id, run_id="seed-1")
    second = match_posting(db_session, other.id, run_id="seed-2")
    for row in (first, second):
        row.is_stale = True
    other.inactive_at = dt.datetime.now(dt.UTC)
    db_session.commit()

    stub.payloads = [payload(seed, verdict="strong")]
    report = recompute_stale(db_session, run_id="run-sweep", limit=5)

    assert report.total == 1, "the inactive posting is not swept"
    assert report.matched == 1
    db_session.refresh(first)
    assert first.is_stale is False
    assert first.verdict is Verdict.strong
    assert count(db_session, Match) == 2


def test_recompute_stale_supersedes_old_versions(db_session, seed, install_stub):
    stub = install_stub([payload(seed)])
    old = match_posting(db_session, seed.posting_id, run_id="seed-3")
    old.is_stale = True
    set_evidence_version(db_session, 2)
    db_session.commit()

    stub.payloads = [payload(seed, verdict="strong")]
    report = recompute_stale(db_session, run_id="run-sweep-2", limit=10)

    assert report.matched == 1
    db_session.refresh(old)
    assert old.evidence_version == 1
    assert old.is_stale is False, "superseded rows stop being rediscovered"
    assert count(db_session, Match) == 2
    # a second sweep finds nothing left to do
    assert recompute_stale(db_session, run_id="run-sweep-3", limit=10).total == 0


def test_recompute_stale_respects_the_limit(db_session, seed, install_stub):
    other = _second_posting(db_session, seed)
    stub = install_stub([payload(seed)])
    stub.payloads = [payload(seed), _payload_for(db_session, other)]
    first = match_posting(db_session, seed.posting_id, run_id="seed-4")
    second = match_posting(db_session, other.id, run_id="seed-5")
    for row in (first, second):
        row.is_stale = True
    db_session.commit()

    stub.payloads = [payload(seed)]
    report = recompute_stale(db_session, run_id="run-sweep-4", limit=1)
    assert report.total == 1


def _payload_for(session, posting):
    """A valid payload for a posting other than the seeded one."""
    from jme.matcher.retrieval import load_requirements, retrieve_for_posting

    retrieval = retrieve_for_posting(session, posting.id)
    chunk_ids = sorted(retrieval.valid_chunk_ids)
    rows = []
    for index, req in enumerate(load_requirements(session, posting.id)):
        rows.append(
            {
                "requirement_id": req.requirement_id,
                "canonical_skill_id": req.canonical_skill_id,
                "status": "evidenced" if index == 0 else "absent",
                "evidence_chunk_id": chunk_ids[0] if index == 0 else None,
                "reasoning": "stubbed",
            }
        )
    return {"requirements": rows, "verdict": "plausible", "rationale": "stub"}
