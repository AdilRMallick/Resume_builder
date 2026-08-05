"""Turn job description text into `posting_requirement` rows.

Three things here are load-bearing and worth reading closely:

1. **Verbatim verification.** The model is told to quote spans. It is not trusted to have
   done so. Every span is checked against the job description with whitespace and
   typographic punctuation normalized away - a model that reflows a bullet across lines or
   turns a curly apostrophe straight has still quoted it, and rejecting that would be
   pedantry. A span that survives the check is stored as the *source text's* exact
   characters (recovered through an offset map), not as the model typed it, so `raw_text`
   is always something you can find in the JD with ctrl-F. Anything that fails gets one
   corrective retry naming the offending spans, and is then dropped. A paraphrase is never
   stored: `raw_text` is the audit trail for the whole gap report, and a summarized
   requirement quietly poisons it.

2. **Idempotency.** Persistence is INSERT ... ON CONFLICT on
   `uq_requirement_posting_raw_prompt` (posting_id, raw_text, prompt_version). The enrich
   queue is at-least-once, so re-extraction has to be a no-op-shaped update rather than a
   second set of rows.

3. **The taxonomy boundary.** Extraction never creates canonical skills. It hands raw text
   to the resolver and takes whatever comes back, including None. A null
   `canonical_skill_id` is a legitimate outcome: the resolver files the text in the
   `skill_alias_candidate` review queue and a human decides later.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from jme.enricher.prompts import (
    PROMPT_VERSION,
    REQUIREMENTS_SCHEMA,
    SYSTEM_PROMPT,
    build_retry_prompt,
    build_user_prompt,
)
from jme.llm import LLMResult, structured_call
from jme.logging import get_logger
from jme.metrics import new_run_id, record
from jme.models import Importance, PostingRequirement

log = get_logger(__name__)

STAGE = "extraction"
KIND = "extraction"

# Importance strength, used when the model returns the same span twice.
_IMPORTANCE_RANK = {Importance.mentioned: 0, Importance.preferred: 1, Importance.required: 2}


class LLMCall(Protocol):
    """The seam tests replace. Structurally identical to `jme.llm.structured_call`."""

    def __call__(self, session: Session, **kwargs: Any) -> LLMResult: ...


#: Module-level seam. Tests monkeypatch `jme.enricher.extraction.call_llm`; callers that
#: prefer injection pass ``llm_call=`` to :func:`extract_requirements`. Both routes exist
#: so no test ever needs an API key.
call_llm: LLMCall = structured_call

Resolver = Callable[..., int | None]


# --------------------------------------------------------------------------------------
# text normalization and verbatim verification
# --------------------------------------------------------------------------------------

# Typographic variants a model routinely "corrects" while copying. Folding these is not
# tolerance of paraphrase; the words are identical.
_CHAR_FOLD = {
    "‘": "'", "’": "'", "‛": "'", "′": "'",
    "“": '"', "”": '"', "‟": '"', "″": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-",
    "…": "...",  # length-changing, handled below
}
_DROP = {"​", "‌", "‍", "﻿", "­"}


@dataclass(frozen=True)
class NormalizedText:
    """Normalized form of a string plus a map back to offsets in the original."""

    original: str
    normalized: str
    offsets: tuple[int, ...]  # offsets[i] = index in `original` of normalized[i]


def normalize(text: str) -> NormalizedText:
    """Collapse whitespace and fold typographic punctuation, keeping an offset map.

    The offset map is the reason we can store the JD's own characters rather than the
    model's transcription of them.
    """
    chars: list[str] = []
    offsets: list[int] = []
    pending_space = False
    for index, char in enumerate(text):
        if char in _DROP:
            continue
        if char.isspace():
            pending_space = bool(chars)
            continue
        folded = _CHAR_FOLD.get(char, char)
        if pending_space:
            chars.append(" ")
            offsets.append(index)
            pending_space = False
        for piece in folded:  # "…" folds to three chars, all mapped to the same origin
            chars.append(piece)
            offsets.append(index)
    return NormalizedText(text, "".join(chars), tuple(offsets))


def find_verbatim(haystack: NormalizedText, candidate: str) -> str | None:
    """Return the source text's own span for `candidate`, or None if it is not there.

    Matching is on the normalized forms. A case-insensitive second pass catches models
    that re-capitalize a span; the returned text still comes from the source, so the
    stored `raw_text` carries the JD's capitalization either way.
    """
    needle = normalize(candidate)
    if not needle.normalized:
        return None

    position = haystack.normalized.find(needle.normalized)
    if position < 0:
        hay_lower = haystack.normalized.lower()
        needle_lower = needle.normalized.lower()
        # .lower() is length-preserving for everything we care about; bail if it is not,
        # because the offset map would no longer line up.
        if len(hay_lower) == len(haystack.normalized) and len(needle_lower) == len(
            needle.normalized
        ):
            position = hay_lower.find(needle_lower)
    if position < 0:
        return None

    start = haystack.offsets[position]
    end = haystack.offsets[position + len(needle.normalized) - 1] + 1
    return haystack.original[start:end]


# --------------------------------------------------------------------------------------
# model output parsing
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    raw_text: str
    importance: Importance
    confidence: float


def parse_candidates(payload: dict[str, Any]) -> list[Candidate]:
    """Schema-shaped payload to candidates, skipping anything malformed rather than raising.

    `structured_call` already validates against the JSON schema, so this is belt-and-braces
    for cached payloads written by an older schema version.
    """
    out: list[Candidate] = []
    for item in payload.get("requirements") or []:
        if not isinstance(item, dict):
            continue
        raw_text = item.get("raw_text")
        if not isinstance(raw_text, str) or not raw_text.strip():
            continue
        try:
            importance = Importance(item.get("importance"))
        except ValueError:
            log.warning("bad_importance", value=item.get("importance"), raw_text=raw_text[:80])
            continue
        try:
            confidence = float(item.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        out.append(
            Candidate(
                raw_text=raw_text,
                importance=importance,
                confidence=round(min(max(confidence, 0.0), 1.0), 3),
            )
        )
    return out


def verify(candidates: list[Candidate], jd: NormalizedText) -> tuple[list[Candidate], list[str]]:
    """Split candidates into (verbatim, rejected raw_text spans).

    Verbatim candidates come back with `raw_text` replaced by the source text's own span.
    """
    kept: list[Candidate] = []
    rejected: list[str] = []
    for candidate in candidates:
        span = find_verbatim(jd, candidate.raw_text)
        if span is None:
            rejected.append(candidate.raw_text)
            continue
        kept.append(
            Candidate(
                raw_text=span.strip(),
                importance=candidate.importance,
                confidence=candidate.confidence,
            )
        )
    return kept, rejected


def dedupe(candidates: list[Candidate]) -> list[Candidate]:
    """Collapse repeated spans, keeping the strongest importance and highest confidence.

    Required because ON CONFLICT DO UPDATE cannot touch the same row twice in one
    statement, and because two model spans can recover to the same source span.
    """
    best: dict[str, Candidate] = {}
    for candidate in candidates:
        existing = best.get(candidate.raw_text)
        if existing is None:
            best[candidate.raw_text] = candidate
            continue
        best[candidate.raw_text] = Candidate(
            raw_text=candidate.raw_text,
            importance=max(
                existing.importance, candidate.importance, key=lambda i: _IMPORTANCE_RANK[i]
            ),
            confidence=max(existing.confidence, candidate.confidence),
        )
    return list(best.values())


# --------------------------------------------------------------------------------------
# taxonomy
# --------------------------------------------------------------------------------------


def _default_resolver(session: Session, raw_text: str, posting_id: int | None = None) -> int | None:
    """Lazy import so this module never hard-depends on the taxonomy being finished.

    An unresolved requirement is a supported outcome - the row keeps a null
    canonical_skill_id and the raw text lands in the alias review queue - so a missing
    resolver degrades extraction instead of stopping it.
    """
    try:
        from jme.taxonomy.resolver import resolve
    except ImportError as exc:
        log.warning("taxonomy_resolver_unavailable", raw_text=raw_text[:80], error=str(exc))
        return None
    return resolve(session, raw_text, posting_id=posting_id)


def _resolve_all(
    session: Session, candidates: list[Candidate], posting_id: int, resolver: Resolver | None
) -> list[int | None]:
    resolve_fn = resolver or _default_resolver
    resolved: list[int | None] = []
    for candidate in candidates:
        try:
            skill_id = resolve_fn(session, candidate.raw_text, posting_id=posting_id)
        except Exception as exc:  # noqa: BLE001 - one bad span must not lose the posting
            log.warning(
                "resolver_failed", raw_text=candidate.raw_text[:80], error=str(exc),
                posting_id=posting_id,
            )
            skill_id = None
        resolved.append(skill_id)
    return resolved


# --------------------------------------------------------------------------------------
# persistence
# --------------------------------------------------------------------------------------


def _upsert(
    session: Session,
    posting_id: int,
    candidates: list[Candidate],
    skill_ids: list[int | None],
    model_id: str | None,
) -> list[PostingRequirement]:
    if not candidates:
        return []

    rows = [
        {
            "posting_id": posting_id,
            "canonical_skill_id": skill_id,
            "raw_text": candidate.raw_text,
            "importance": candidate.importance,
            "confidence": candidate.confidence,
            "prompt_version": PROMPT_VERSION,
            "model_id": model_id,
        }
        for candidate, skill_id in zip(candidates, skill_ids, strict=True)
    ]

    stmt = insert(PostingRequirement).values(rows)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_requirement_posting_raw_prompt",
        set_={
            "canonical_skill_id": stmt.excluded.canonical_skill_id,
            "importance": stmt.excluded.importance,
            "confidence": stmt.excluded.confidence,
            "model_id": stmt.excluded.model_id,
        },
    ).returning(PostingRequirement)

    result = session.scalars(stmt, execution_options={"populate_existing": True})
    return list(result)


# --------------------------------------------------------------------------------------
# the entry point
# --------------------------------------------------------------------------------------


def extract_requirements(
    session: Session,
    posting_id: int,
    jd_text: str,
    run_id: str | None = None,
    resolver: Resolver | None = None,
    *,
    company: str | None = None,
    title: str | None = None,
    llm_call: LLMCall | None = None,
    model: str | None = None,
    force_refresh: bool = False,
) -> list[PostingRequirement]:
    """Extract, verify, resolve, and persist the requirements for one posting.

    Flushes but does not commit: the caller owns the transaction boundary, because the
    queue worker only acks after its commit lands.
    """
    if not jd_text or not jd_text.strip():
        raise ValueError(f"posting {posting_id} has no job description text to extract from")

    run = run_id or new_run_id("extract")
    invoke = llm_call or call_llm
    labels = {"posting_id": posting_id}

    jd = normalize(jd_text)
    user_prompt = build_user_prompt(jd_text, company=company, title=title)

    result = invoke(
        session,
        kind=KIND,
        system=SYSTEM_PROMPT,
        user=user_prompt,
        schema=REQUIREMENTS_SCHEMA,
        prompt_version=PROMPT_VERSION,
        run_id=run,
        model=model,
        force_refresh=force_refresh,
    )
    kept, rejected = verify(parse_candidates(result.payload), jd)

    if rejected:
        log.warning(
            "paraphrase_detected",
            posting_id=posting_id,
            count=len(rejected),
            spans=[span[:80] for span in rejected[:5]],
        )
        retry = invoke(
            session,
            kind=KIND,
            system=SYSTEM_PROMPT,
            user=build_retry_prompt(user_prompt, rejected),
            schema=REQUIREMENTS_SCHEMA,
            prompt_version=PROMPT_VERSION,
            run_id=run,
            model=model,
            force_refresh=force_refresh,
        )
        result = retry
        kept, rejected = verify(parse_candidates(retry.payload), jd)
        if rejected:
            # One retry is the budget. Drop rather than store a paraphrase.
            log.warning(
                "paraphrase_rejected",
                posting_id=posting_id,
                count=len(rejected),
                spans=[span[:80] for span in rejected[:5]],
            )

    candidates = dedupe(kept)
    skill_ids = _resolve_all(session, candidates, posting_id, resolver)
    rows = _upsert(session, posting_id, candidates, skill_ids, result.model)
    session.flush()

    resolved_count = sum(1 for skill_id in skill_ids if skill_id is not None)
    record(session, run, STAGE, "requirements_found", len(candidates), labels)
    record(session, run, STAGE, "requirements_resolved", resolved_count, labels)
    record(
        session, run, STAGE, "requirements_unresolved", len(candidates) - resolved_count, labels
    )
    record(session, run, STAGE, "paraphrase_rejected", len(rejected), labels)

    log.info(
        "extraction_complete",
        posting_id=posting_id,
        run_id=run,
        found=len(candidates),
        resolved=resolved_count,
        rejected=len(rejected),
        prompt_version=PROMPT_VERSION,
        model=result.model,
        cached=result.cached,
    )
    return rows
