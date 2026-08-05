"""LLM matching with citations back to evidence chunks.

Design notes worth defending in an interview:

**Nothing unvalidated reaches Postgres.** The database has
`ck_evidenced_requires_chunk`, but a check constraint firing is a bad error message and a
poisoned transaction. Validation happens in `validate_response` before a single row is
constructed, so the constraint is a backstop and never the first line of defence.

**A rejected response is never cached.** `jme.llm.structured_call` writes an `llm_cache`
row for every live call. When validation rejects the payload we expunge that pending row,
otherwise the fabrication would be served from cache forever and the retry would be
pointless. The single retry also uses a different (corrective) prompt, so it cannot be
answered from the cache either.

**Two caches, deliberately.** The `match` row itself is the outer cache, keyed by
`uq_match_cache_key` = (posting_id, evidence_version, prompt_version, model_id, jd_sha256).
A fresh row short-circuits before any LLM work. `llm_cache` is the inner cache, keyed on
content + prompt_version + model + the evidence_version/jd hash we pass as
`extra_key_parts`. The inner one still earns its keep: it survives a match row being
recomputed because it went stale.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from jme.config import get_settings
from jme.llm import LLMResult, sha256, structured_call
from jme.logging import get_logger
from jme.matcher.prompts import (
    MATCH_SCHEMA,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    build_corrective_prompt,
    build_user_prompt,
)
from jme.matcher.retrieval import (
    RetrievalSet,
    current_evidence_version,
    default_top_k,
    load_requirements,
    retrieve_for_posting,
)
from jme.metrics import new_run_id, record
from jme.models import (
    CitationStatus,
    Importance,
    LLMCache,
    Match,
    MatchCitation,
    Posting,
    PostingJD,
    ShortlistEntry,
    Verdict,
)

log = get_logger(__name__)

KIND = "match"
STAGE = "match"

CostPolicy = Literal["warn", "abort"]

#: How much a requirement contributes to the score, by importance.
IMPORTANCE_WEIGHT: dict[Importance, float] = {
    Importance.required: 1.0,
    Importance.preferred: 0.5,
    Importance.mentioned: 0.25,
}

#: How much credit a status earns.
STATUS_CREDIT: dict[CitationStatus, float] = {
    CitationStatus.evidenced: 1.0,
    CitationStatus.weak: 0.5,
    CitationStatus.absent: 0.0,
}


# --------------------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------------------


class MatchError(RuntimeError):
    """Base for every typed failure the matcher raises."""


class NoRequirementsError(MatchError):
    """The posting has no extracted requirements, so there is nothing to match."""


class MatchValidationError(MatchError):
    """The model's response was rejected. Carries the human-readable problems."""

    def __init__(self, message: str, problems: Sequence[str] | None = None) -> None:
        super().__init__(message)
        self.problems: list[str] = list(problems or [message])


class MalformedResponseError(MatchValidationError):
    """Structurally wrong: missing rows, unknown requirement ids, bad enum values."""


class MissingCitationError(MatchValidationError):
    """status == 'evidenced' with a null evidence_chunk_id."""


class FabricatedCitationError(MatchValidationError):
    """A cited chunk id was never in the retrieved candidate set."""


class CostCeilingExceeded(MatchError):
    """One match cost more than settings.max_cost_per_match_usd (policy='abort')."""


class RunBudgetExceeded(MatchError):
    """A batch cost more than ceiling * shortlist_size."""


# --------------------------------------------------------------------------------------
# results
# --------------------------------------------------------------------------------------


@dataclass
class ValidatedCitation:
    requirement_id: int
    canonical_skill_id: int | None
    status: CitationStatus
    evidence_chunk_id: int | None
    reasoning: str | None


@dataclass
class ValidatedMatch:
    citations: list[ValidatedCitation]
    verdict: Verdict
    rationale: str
    unaddressed: list[int] = field(default_factory=list)
    dropped_citations: int = 0


@dataclass
class MatchOutcome:
    """What `match_posting` did, for callers that need more than the row."""

    match: Match
    retrieval: RetrievalSet | None
    from_match_cache: bool
    from_llm_cache: bool
    cost_usd: float
    new_cost_usd: float
    llm_calls: int
    retried: bool


@dataclass
class ShortlistReport:
    run_id: str
    total: int = 0
    matched: int = 0
    from_cache: int = 0
    failed: int = 0
    skipped: int = 0
    cost_usd: float = 0.0
    new_cost_usd: float = 0.0
    budget_usd: float = 0.0
    outcomes: list[MatchOutcome] = field(default_factory=list)
    errors: list[tuple[int, str]] = field(default_factory=list)
    aborted: str | None = None


# --------------------------------------------------------------------------------------
# the injectable seam
# --------------------------------------------------------------------------------------


def call_llm(
    session: Session,
    *,
    system: str,
    user: str,
    run_id: str | None,
    extra_key_parts: tuple[str, ...],
    force_refresh: bool = False,
    model: str | None = None,
) -> LLMResult:
    """The one place this package talks to the model. Tests monkeypatch this symbol.

    Everything else - caching, token accounting, schema-validated JSON - is
    `jme.llm.structured_call`'s job and is not reimplemented here.
    """
    return structured_call(
        session,
        kind=KIND,
        system=system,
        user=user,
        schema=MATCH_SCHEMA,
        prompt_version=PROMPT_VERSION,
        run_id=run_id,
        extra_key_parts=extra_key_parts,
        force_refresh=force_refresh,
        model=model,
    )


def _expunge_pending_llm_cache(session: Session) -> int:
    """Drop not-yet-flushed llm_cache rows so a rejected response is never cached."""
    dropped = 0
    for obj in list(session.new):
        if isinstance(obj, LLMCache):
            session.expunge(obj)
            dropped += 1
    return dropped


# --------------------------------------------------------------------------------------
# validation - the load-bearing part
# --------------------------------------------------------------------------------------


def validate_response(payload: dict[str, Any], retrieval: RetrievalSet) -> ValidatedMatch:
    """Turn a raw model payload into rows, or raise.

    Rejection rules, in the order the task brief states them:
      1. status 'evidenced' with a null evidence_chunk_id  -> MissingCitationError
      2. any evidence_chunk_id outside the retrieved set   -> FabricatedCitationError
      3. structurally unusable payload                     -> MalformedResponseError

    Softer handling, deliberately not a rejection:
      * a non-'evidenced' row citing an unknown chunk has the citation nulled (the
        citation carries no meaning there, so failing the whole match would be theatre)
      * a requirement the model ignored is recorded as 'absent' and counted
      * a canonical_skill_id disagreeing with the database is overridden by the database;
        the taxonomy is curated and the model does not get a vote
    """
    problems: list[str] = []
    valid_ids = retrieval.valid_chunk_ids

    rows = payload.get("requirements")
    if not isinstance(rows, list):
        raise MalformedResponseError(
            "response has no 'requirements' array", ["'requirements' must be an array"]
        )

    citations: list[ValidatedCitation] = []
    seen: set[int] = set()
    dropped = 0

    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            problems.append(f"requirements[{index}] is not an object")
            continue

        raw_req_id = row.get("requirement_id")
        try:
            req_id = int(raw_req_id)
        except (TypeError, ValueError):
            problems.append(f"requirements[{index}] has a non-integer requirement_id")
            continue

        req = retrieval.requirement(req_id)
        if req is None:
            problems.append(
                f"requirement_id {req_id} was not in the prompt; valid ids are "
                f"{sorted(retrieval.requirement_ids)}"
            )
            continue
        if req_id in seen:
            problems.append(f"requirement_id {req_id} appeared more than once")
            continue
        seen.add(req_id)

        try:
            status = CitationStatus(str(row.get("status")))
        except ValueError:
            problems.append(
                f"requirement {req_id}: status {row.get('status')!r} is not "
                "evidenced|weak|absent"
            )
            continue

        chunk_id = row.get("evidence_chunk_id")
        if chunk_id is not None:
            try:
                chunk_id = int(chunk_id)
            except (TypeError, ValueError):
                problems.append(
                    f"requirement {req_id}: evidence_chunk_id {chunk_id!r} is not an integer"
                )
                continue

        if status is CitationStatus.evidenced:
            # rule 1: evidenced without a citation. Caught here so it never reaches the
            # ck_evidenced_requires_chunk check constraint.
            if chunk_id is None:
                problems.append(
                    f"requirement {req_id}: status 'evidenced' requires an evidence_chunk_id, "
                    "got null"
                )
                continue
            # rule 2: fabricated id. Hard rejection.
            if chunk_id not in valid_ids:
                problems.append(
                    f"requirement {req_id}: evidence_chunk_id {chunk_id} was never supplied"
                )
                continue
        elif chunk_id is not None and chunk_id not in valid_ids:
            log.warning(
                "dropped_unknown_citation",
                requirement_id=req_id,
                status=status.value,
                evidence_chunk_id=chunk_id,
            )
            chunk_id = None
            dropped += 1

        model_skill = row.get("canonical_skill_id")
        if (
            model_skill is not None
            and req.canonical_skill_id is not None
            and int(model_skill) != req.canonical_skill_id
        ):
            log.warning(
                "canonical_skill_id_overridden",
                requirement_id=req_id,
                model_said=model_skill,
                using=req.canonical_skill_id,
            )

        reasoning = row.get("reasoning")
        citations.append(
            ValidatedCitation(
                requirement_id=req_id,
                canonical_skill_id=req.canonical_skill_id,
                status=status,
                evidence_chunk_id=chunk_id,
                reasoning=str(reasoning).strip() if reasoning else None,
            )
        )

    if problems:
        _raise_for(problems)

    unaddressed = sorted(rid for rid in retrieval.requirement_ids if rid not in seen)
    for req_id in unaddressed:
        missing = retrieval.requirement(req_id)
        citations.append(
            ValidatedCitation(
                requirement_id=req_id,
                canonical_skill_id=missing.canonical_skill_id if missing else None,
                status=CitationStatus.absent,
                evidence_chunk_id=None,
                reasoning="not addressed in the model response; recorded as absent",
            )
        )

    try:
        verdict = Verdict(str(payload.get("verdict")))
    except ValueError:
        raise MalformedResponseError(
            f"verdict {payload.get('verdict')!r} is not strong|plausible|stretch|no",
            [f"verdict must be one of {[v.value for v in Verdict]}"],
        ) from None

    rationale = str(payload.get("rationale") or "").strip()

    order = {rid: i for i, rid in enumerate(r.requirement_id for r in retrieval.requirements)}
    citations.sort(key=lambda c: order.get(c.requirement_id, 10_000))

    return ValidatedMatch(
        citations=citations,
        verdict=verdict,
        rationale=rationale,
        unaddressed=unaddressed,
        dropped_citations=dropped,
    )


def _raise_for(problems: list[str]) -> None:
    """Pick the most specific error type for a set of problems. Fabrication wins:
    it is the one failure mode that would put a lie in the database."""
    joined = "; ".join(problems)
    if any("was never supplied" in p for p in problems):
        raise FabricatedCitationError(f"fabricated citation: {joined}", problems)
    if any("requires an evidence_chunk_id" in p for p in problems):
        raise MissingCitationError(f"missing citation: {joined}", problems)
    raise MalformedResponseError(f"malformed match response: {joined}", problems)


def compute_score(validated: ValidatedMatch, retrieval: RetrievalSet) -> float:
    """Importance-weighted evidence coverage in [0, 1].

    Computed here rather than asked of the model: a number the model invents is not
    comparable across postings, and this one is reproducible from the stored citations.
    """
    total = 0.0
    earned = 0.0
    for citation in validated.citations:
        req = retrieval.requirement(citation.requirement_id)
        weight = IMPORTANCE_WEIGHT.get(req.importance if req else Importance.mentioned, 0.25)
        total += weight
        earned += weight * STATUS_CREDIT[citation.status]
    if total == 0:
        return 0.0
    return round(earned / total, 4)


# --------------------------------------------------------------------------------------
# posting inputs
# --------------------------------------------------------------------------------------


def load_jd_text(session: Session, posting: Posting) -> tuple[str, bool]:
    """JD text for the prompt and the cache key.

    A posting with no resolved JD still matches, on title and company alone, per
    ARCHITECTURE section 8 ("failures degrade rather than drop"). The fallback text is
    still hashed, so the degraded result gets its own cache entry and is replaced
    naturally once the fetcher resolves the real JD.
    """
    jd = session.get(PostingJD, posting.id)
    if jd is not None and jd.raw_text:
        return jd.raw_text, True
    return f"{posting.company}\n{posting.title}", False


# --------------------------------------------------------------------------------------
# match one posting
# --------------------------------------------------------------------------------------


def match_posting(
    session: Session,
    posting_id: int,
    run_id: str | None = None,
    force: bool = False,
    *,
    top_k: int | None = None,
    cost_policy: CostPolicy = "warn",
    model: str | None = None,
) -> Match:
    """Match one posting and return its persisted `match` row.

    `force` recomputes even when a fresh match row exists, and bypasses the LLM cache.
    `cost_policy` decides what an over-ceiling match does: "warn" logs, records a metric
    and continues (the default - one expensive posting should not kill a nightly run),
    "abort" raises `CostCeilingExceeded` after the row is persisted (we already paid for
    the answer; throwing it away would be the second mistake).
    """
    return match_posting_detailed(
        session,
        posting_id,
        run_id=run_id,
        force=force,
        top_k=top_k,
        cost_policy=cost_policy,
        model=model,
    ).match


def match_posting_detailed(
    session: Session,
    posting_id: int,
    *,
    run_id: str | None = None,
    force: bool = False,
    top_k: int | None = None,
    cost_policy: CostPolicy = "warn",
    model: str | None = None,
) -> MatchOutcome:
    settings = get_settings()
    run_id = run_id or new_run_id(KIND)
    model_id = model or settings.anthropic_model
    k = top_k if top_k is not None else default_top_k()

    posting = session.get(Posting, posting_id)
    if posting is None:
        raise MatchError(f"posting {posting_id} does not exist")

    jd_text, jd_present = load_jd_text(session, posting)
    jd_sha = sha256(jd_text)
    evidence_version = current_evidence_version(session)

    existing = session.scalar(
        select(Match).where(
            Match.posting_id == posting_id,
            Match.evidence_version == evidence_version,
            Match.prompt_version == PROMPT_VERSION,
            Match.model_id == model_id,
            Match.jd_sha256 == jd_sha,
        )
    )

    # outer cache: a fresh row for this exact key means there is nothing to do
    if existing is not None and not force and not existing.is_stale:
        record(session, run_id, STAGE, "match_row_cache_hit", 1, {"posting_id": posting_id})
        session.commit()
        log.info(
            "match_cached", posting_id=posting_id, match_id=existing.id, evidence_version=evidence_version
        )
        return MatchOutcome(
            match=existing,
            retrieval=None,
            from_match_cache=True,
            from_llm_cache=True,
            cost_usd=float(existing.cost_usd or 0),
            new_cost_usd=0.0,
            llm_calls=0,
            retried=False,
        )

    requirements = load_requirements(session, posting_id)
    if not requirements:
        record(session, run_id, STAGE, "no_requirements", 1, {"posting_id": posting_id})
        session.commit()
        raise NoRequirementsError(
            f"posting {posting_id} has no extracted requirements; run extraction first"
        )

    retrieval = retrieve_for_posting(session, posting_id, top_k=k, requirements=requirements)

    system = SYSTEM_PROMPT
    user = build_user_prompt(
        company=posting.company,
        title=posting.title,
        jd_text=jd_text if jd_present else None,
        requirements=requirements,
        chunks=retrieval.chunks,
    )
    # ARCHITECTURE section 5: sha256(jd_text) + evidence_version + prompt_version + model_id.
    # prompt_version and model_id are folded in by structured_call itself; the other two are
    # explicit here so the dependency is impossible to lose in a refactor.
    extra_key_parts = (f"jd_sha256={jd_sha}", f"evidence_version={evidence_version}")

    results: list[LLMResult] = []
    validated: ValidatedMatch | None = None
    retried = False
    last_error: MatchValidationError | None = None

    for attempt in (1, 2):
        result = call_llm(
            session,
            system=system,
            user=user,
            run_id=run_id,
            extra_key_parts=extra_key_parts,
            force_refresh=force and attempt == 1,
            model=model,
        )
        results.append(result)
        try:
            validated = validate_response(result.payload, retrieval)
            break
        except MatchValidationError as exc:
            last_error = exc
            # never let a rejected payload reach llm_cache, or it would be replayed forever
            _expunge_pending_llm_cache(session)
            log.warning(
                "match_response_rejected",
                posting_id=posting_id,
                attempt=attempt,
                error=type(exc).__name__,
                problems=exc.problems,
            )
            if attempt == 2:
                break
            retried = True
            record(session, run_id, STAGE, "citation_retry", 1, {"posting_id": posting_id})
            user = build_corrective_prompt(
                user,
                problems=exc.problems,
                valid_chunk_ids=sorted(retrieval.valid_chunk_ids),
            )

    if validated is None:
        assert last_error is not None
        metric = {
            FabricatedCitationError: "fabricated_citation",
            MissingCitationError: "missing_citation",
        }.get(type(last_error), "malformed_response")
        record(session, run_id, STAGE, metric, 1, {"posting_id": posting_id})
        record(session, run_id, STAGE, "match_failed", 1, {"posting_id": posting_id})
        # commit the metrics only: no match row was ever constructed, so there is nothing
        # partial to unwind, and the poisoned cache rows were expunged above
        session.commit()
        log.error(
            "match_rejected",
            posting_id=posting_id,
            error=type(last_error).__name__,
            attempts=len(results),
            problems=last_error.problems,
        )
        raise last_error

    cost = sum(r.cost_usd for r in results)
    new_cost = sum(r.cost_usd for r in results if not r.cached)
    input_tokens = sum(r.input_tokens for r in results)
    output_tokens = sum(r.output_tokens for r in results)
    score = compute_score(validated, retrieval)

    over_ceiling = cost > settings.max_cost_per_match_usd

    rationale = {
        "text": validated.rationale,
        "verdict": validated.verdict.value,
        "prompt_version": PROMPT_VERSION,
        "top_k": k,
        "retrieved_chunk_ids": sorted(retrieval.valid_chunk_ids),
        "requirement_count": len(requirements),
        "jd_present": jd_present,
        "llm_calls": len(results),
        "retried": retried,
        "unaddressed_requirement_ids": validated.unaddressed,
        "dropped_citations": validated.dropped_citations,
    }

    # ---- one transaction for the match row and every citation ----
    with session.begin_nested():
        if existing is None:
            match = Match(
                posting_id=posting_id,
                evidence_version=evidence_version,
                prompt_version=PROMPT_VERSION,
                model_id=model_id,
                jd_sha256=jd_sha,
            )
            session.add(match)
        else:
            match = existing
            session.execute(delete(MatchCitation).where(MatchCitation.match_id == match.id))

        match.score = score
        match.verdict = validated.verdict
        match.rationale = rationale
        match.is_stale = False
        match.input_tokens = input_tokens
        match.output_tokens = output_tokens
        match.cost_usd = Decimal(str(round(cost, 6)))
        match.computed_at = dt.datetime.now(dt.UTC)
        session.flush()

        for citation in validated.citations:
            session.add(
                MatchCitation(
                    match_id=match.id,
                    canonical_skill_id=citation.canonical_skill_id,
                    evidence_chunk_id=citation.evidence_chunk_id,
                    status=citation.status,
                    reasoning=citation.reasoning,
                )
            )
        session.flush()

    counts = {status: 0 for status in CitationStatus}
    for citation in validated.citations:
        counts[citation.status] += 1

    labels = {"posting_id": posting_id}
    record(session, run_id, STAGE, "matched", 1, labels)
    record(session, run_id, STAGE, "match_cost_usd", cost, labels)
    record(session, run_id, STAGE, "match_row_cache_hit", 0, labels)
    record(session, run_id, STAGE, "score", score, labels)
    for status, count in counts.items():
        record(session, run_id, STAGE, f"citation_{status.value}", count, labels)
    if validated.unaddressed:
        record(session, run_id, STAGE, "unaddressed_requirements", len(validated.unaddressed), labels)
    if over_ceiling:
        record(
            session,
            run_id,
            STAGE,
            "cost_ceiling_exceeded",
            1,
            {"posting_id": posting_id, "cost_usd": round(cost, 6), "policy": cost_policy},
        )
        log.error(
            "cost_ceiling_exceeded",
            posting_id=posting_id,
            cost_usd=round(cost, 6),
            ceiling_usd=settings.max_cost_per_match_usd,
            policy=cost_policy,
        )

    session.commit()
    session.expire(match, ["citations"])

    log.info(
        "matched",
        posting_id=posting_id,
        match_id=match.id,
        verdict=validated.verdict.value,
        score=score,
        cost_usd=round(cost, 6),
        evidence_version=evidence_version,
        llm_calls=len(results),
        cached=all(r.cached for r in results),
    )

    outcome = MatchOutcome(
        match=match,
        retrieval=retrieval,
        from_match_cache=False,
        from_llm_cache=all(r.cached for r in results),
        cost_usd=cost,
        new_cost_usd=new_cost,
        llm_calls=len(results),
        retried=retried,
    )

    if over_ceiling and cost_policy == "abort":
        raise CostCeilingExceeded(
            f"match for posting {posting_id} cost ${cost:.6f}, ceiling is "
            f"${settings.max_cost_per_match_usd:.6f} (match {match.id} was persisted)"
        )
    return outcome


# --------------------------------------------------------------------------------------
# batches
# --------------------------------------------------------------------------------------


ProgressHook = Callable[[int, int, int, MatchOutcome | None, Exception | None], None]


def latest_shortlist_run(session: Session) -> str | None:
    return session.scalar(
        select(ShortlistEntry.run_id).order_by(ShortlistEntry.created_at.desc()).limit(1)
    )


def shortlist_postings(session: Session, run_id: str) -> list[int]:
    return list(
        session.scalars(
            select(ShortlistEntry.posting_id)
            .where(ShortlistEntry.run_id == run_id)
            .order_by(ShortlistEntry.rank.asc())
        ).all()
    )


def match_shortlist(
    session: Session,
    run_id: str | None = None,
    *,
    force: bool = False,
    top_k: int | None = None,
    cost_policy: CostPolicy = "warn",
    run_budget_usd: float | None = None,
    on_progress: ProgressHook | None = None,
    model: str | None = None,
) -> ShortlistReport:
    """Match every posting in a shortlist run.

    The run budget is `max_cost_per_match_usd * shortlist_size` by default, where
    shortlist_size is the actual size of this run. Per-match overspend is survivable
    (`cost_policy='warn'`); blowing the whole run's budget is not, and aborts the batch.
    """
    settings = get_settings()
    resolved = run_id or latest_shortlist_run(session)
    if resolved is None:
        raise MatchError("no shortlist runs exist; run the rank pipeline first")

    posting_ids = shortlist_postings(session, resolved)
    size = len(posting_ids) or settings.shortlist_size
    budget = (
        run_budget_usd
        if run_budget_usd is not None
        else settings.max_cost_per_match_usd * size
    )

    report = ShortlistReport(run_id=resolved, total=len(posting_ids), budget_usd=budget)

    for index, posting_id in enumerate(posting_ids, start=1):
        try:
            outcome = match_posting_detailed(
                session,
                posting_id,
                run_id=resolved,
                force=force,
                top_k=top_k,
                cost_policy=cost_policy,
                model=model,
            )
        except NoRequirementsError as exc:
            report.skipped += 1
            report.errors.append((posting_id, str(exc)))
            if on_progress:
                on_progress(index, report.total, posting_id, None, exc)
            continue
        except CostCeilingExceeded:
            raise
        except MatchError as exc:
            report.failed += 1
            report.errors.append((posting_id, f"{type(exc).__name__}: {exc}"))
            if on_progress:
                on_progress(index, report.total, posting_id, None, exc)
            continue

        report.matched += 1
        report.from_cache += 1 if outcome.from_match_cache else 0
        report.cost_usd += outcome.cost_usd
        report.new_cost_usd += outcome.new_cost_usd
        report.outcomes.append(outcome)
        if on_progress:
            on_progress(index, report.total, posting_id, outcome, None)

        if report.new_cost_usd > budget:
            report.aborted = (
                f"run spend ${report.new_cost_usd:.4f} exceeded budget ${budget:.4f} "
                f"after {index}/{report.total} postings"
            )
            record(session, resolved, STAGE, "run_budget_exceeded", 1, {"spend": report.new_cost_usd})
            session.commit()
            log.error("run_budget_exceeded", run_id=resolved, spend=report.new_cost_usd, budget=budget)
            raise RunBudgetExceeded(report.aborted)

    record(session, resolved, STAGE, "postings_matched", report.matched)
    record(session, resolved, STAGE, "run_cost_usd", report.new_cost_usd)
    session.commit()
    log.info(
        "shortlist_matched",
        run_id=resolved,
        total=report.total,
        matched=report.matched,
        from_cache=report.from_cache,
        failed=report.failed,
        skipped=report.skipped,
        cost_usd=round(report.cost_usd, 6),
    )
    return report


def stale_posting_ids(session: Session, limit: int) -> list[int]:
    """Stale matches for still-active postings, oldest first. Bounded by design."""
    rows = session.execute(
        select(Match.posting_id, Match.computed_at)
        .join(Posting, Posting.id == Match.posting_id)
        .where(Match.is_stale.is_(True), Posting.inactive_at.is_(None))
        .order_by(Match.computed_at.asc())
    ).all()

    ordered: list[int] = []
    for posting_id, _ in rows:
        if posting_id not in ordered:
            ordered.append(posting_id)
        if len(ordered) >= limit:
            break
    return ordered


def recompute_stale(
    session: Session,
    *,
    run_id: str | None = None,
    limit: int = 20,
    top_k: int | None = None,
    cost_policy: CostPolicy = "warn",
    on_progress: ProgressHook | None = None,
    model: str | None = None,
) -> ShortlistReport:
    """The lazy bounded recompute from ARCHITECTURE section 4.

    An evidence bump marks matches stale; this sweeps at most `limit` of them, only for
    active postings. Recomputing writes a NEW row at the current evidence_version - the
    old row is history and is kept - and the superseded rows are unflagged so the sweep
    terminates instead of rediscovering them forever.
    """
    resolved = run_id or new_run_id("stale")
    posting_ids = stale_posting_ids(session, limit)
    report = ShortlistReport(run_id=resolved, total=len(posting_ids))
    evidence_version = current_evidence_version(session)

    for index, posting_id in enumerate(posting_ids, start=1):
        try:
            outcome = match_posting_detailed(
                session,
                posting_id,
                run_id=resolved,
                top_k=top_k,
                cost_policy=cost_policy,
                model=model,
            )
        except MatchError as exc:
            report.failed += 1
            report.errors.append((posting_id, f"{type(exc).__name__}: {exc}"))
            if on_progress:
                on_progress(index, report.total, posting_id, None, exc)
            continue

        superseded = list(
            session.scalars(
                select(Match).where(
                    Match.posting_id == posting_id,
                    Match.is_stale.is_(True),
                    Match.id != outcome.match.id,
                    Match.evidence_version < evidence_version,
                )
            ).all()
        )
        for row in superseded:
            row.is_stale = False
        if superseded:
            record(
                session,
                resolved,
                STAGE,
                "superseded_matches",
                len(superseded),
                {"posting_id": posting_id},
            )

        report.matched += 1
        report.from_cache += 1 if outcome.from_match_cache else 0
        report.cost_usd += outcome.cost_usd
        report.new_cost_usd += outcome.new_cost_usd
        report.outcomes.append(outcome)
        if on_progress:
            on_progress(index, report.total, posting_id, outcome, None)

    record(session, resolved, STAGE, "stale_recomputed", report.matched)
    session.commit()
    log.info(
        "stale_recompute",
        run_id=resolved,
        candidates=report.total,
        recomputed=report.matched,
        failed=report.failed,
        cost_usd=round(report.cost_usd, 6),
    )
    return report
