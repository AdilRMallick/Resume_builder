"""Deterministic raw-text -> canonical_skill_id resolution. No LLM, no fuzzy matching.

Design notes worth defending:

**Token matching, not substring matching.** The text and every alias are normalized and
split into tokens; an alias matches only as a contiguous run of whole tokens. That is
what makes ``"Go"`` unable to match inside ``"Django"``, ``"Mongo"`` or ``"ongoing"`` --
those normalize to single tokens that are simply not equal to ``"go"``. Because
:mod:`jme.taxonomy.normalize` treats ``+``, ``#`` and internal ``.`` as word characters,
``c++``, ``c#``, ``.net`` and ``node.js`` are single tokens too, so the alias ``c`` can
never be found inside ``c++``.

**Leftmost-longest scan.** :func:`resolve` accepts a whole requirement span, not just a
bare skill name. The span is scanned like a lexer: at each token position the longest
alias that starts there wins ("google cloud platform" beats "google cloud", "react
native" beats "react", "sql server" beats "sql"), and the first match found in the span
is returned. Deterministic and explainable, which matters more here than clever.

**Guard phrases.** Some genuinely useful aliases are also English words. ``"go"`` is the
worst offender, but ``"rest"``, ``"c"``, ``"r"``, ``"spring"``, ``"lambda"`` and
``"express"`` all appear in prose. GUARD_PHRASES lists spans that must never produce a
match; their tokens are masked before scanning, so "go-to-market strategy",
"R&D", "the rest of the team", "Spring 2027 start" and "lambda expressions in Java"
behave. Guards live in code rather than in the seed YAML because they describe matcher
behaviour, not taxonomy.

**This module cannot create a canonical skill.** It never imports
:class:`~jme.models.CanonicalSkill` and issues exactly two kinds of statement: a SELECT
over ``skill_alias`` and an INSERT ... ON CONFLICT DO UPDATE over
``skill_alias_candidate``. Only the CLI (``jme taxonomy approve|promote|seed``) writes to
``canonical_skill``.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from jme.logging import get_logger
from jme.models import CandidateStatus, SkillAlias, SkillAliasCandidate
from jme.taxonomy.normalize import normalize, tokenize

__all__ = [
    "GUARD_PHRASES",
    "AliasIndex",
    "SkillMatch",
    "invalidate_cache",
    "load_index",
    "resolve",
    "resolve_all",
    "scan",
]

log = get_logger(__name__)

#: Spans that must never yield a skill match. Their tokens are masked before scanning.
GUARD_PHRASES: tuple[str, ...] = (
    # "Go" the English verb / marketing noun
    "go to market",
    "go getter",
    "on the go",
    "go live",
    "go the extra mile",
    "go above and beyond",
    "go beyond",
    "ready to go",
    "we go",
    "you go",
    "they go",
    "will go",
    "can go",
    "must go",
    "to go",
    "go through",
    "go into",
    "go from",
    "go hand in hand",
    # "R" from R&D
    "r and d",
    "research and development",
    # "C" from the executive suite
    "c level",
    "c suite",
    "c corp",
    # "REST" the noun
    "the rest",
    "rest of",
    "at rest",
    "rest assured",
    "rest of the",
    # "Lambda" the language construct
    "lambda expressions",
    "lambda expression",
    "lambda calculus",
    "lambda functions in java",
    # "Spring" the season -- new-grad postings are full of start-season text
    "spring 2024",
    "spring 2025",
    "spring 2026",
    "spring 2027",
    "spring 2028",
    "spring 2029",
    "spring semester",
    "spring quarter",
    "spring internship",
    "spring co op",
    "spring start",
    "fall and spring",
    "spring and summer",
    # "Express" the verb
    "express interest",
    "express your",
    "express yourself",
    "express written",
    # "Node" the graph/compute noun
    "compute node",
    "leaf node",
    "tree node",
    "node in the graph",
    # misc
    "swift execution",
    "unity of purpose",
    "scale of the",
)


class AliasIndex:
    """In-memory alias table: tokenized alias -> canonical_skill_id."""

    __slots__ = ("by_tokens", "max_tokens", "size")

    def __init__(self, mapping: dict[tuple[str, ...], int]) -> None:
        self.by_tokens = mapping
        self.max_tokens = max((len(k) for k in mapping), default=0)
        self.size = len(mapping)

    def lookup(self, tokens: tuple[str, ...]) -> int | None:
        return self.by_tokens.get(tokens)


class SkillMatch:
    """One alias occurrence inside a span. ``start``/``end`` are token indices."""

    __slots__ = ("alias", "end", "skill_id", "start")

    def __init__(self, skill_id: int, alias: str, start: int, end: int) -> None:
        self.skill_id = skill_id
        self.alias = alias
        self.start = start
        self.end = end

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"SkillMatch(skill_id={self.skill_id}, alias={self.alias!r}, span=({self.start},{self.end}))"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SkillMatch):
            return NotImplemented
        return (self.skill_id, self.alias, self.start, self.end) == (
            other.skill_id,
            other.alias,
            other.start,
            other.end,
        )


def _guard_index() -> tuple[tuple[tuple[str, ...], ...], int]:
    phrases = tuple(sorted({tokenize(p) for p in GUARD_PHRASES if tokenize(p)}, key=len, reverse=True))
    longest = max((len(p) for p in phrases), default=0)
    return phrases, longest


_GUARDS, _GUARD_MAX = _guard_index()
_GUARD_SET = frozenset(_GUARDS)

# process-wide cache; the alias table changes only when the CLI edits the taxonomy
_INDEX: AliasIndex | None = None


def load_index(session: Session) -> AliasIndex:
    """Read the alias table from the database. Read-only, no cache involvement."""
    rows = session.execute(select(SkillAlias.alias_norm, SkillAlias.canonical_skill_id)).all()
    mapping: dict[tuple[str, ...], int] = {}
    for alias_norm, skill_id in rows:
        key = tokenize(alias_norm)
        if key:
            mapping[key] = skill_id
    return AliasIndex(mapping)


def get_index(session: Session, *, refresh: bool = False) -> AliasIndex:
    """Cached alias index. Call :func:`invalidate_cache` after any taxonomy write."""
    global _INDEX
    if _INDEX is None or refresh:
        _INDEX = load_index(session)
        log.debug("taxonomy.index_loaded", aliases=_INDEX.size, max_tokens=_INDEX.max_tokens)
    return _INDEX


def invalidate_cache() -> None:
    """Drop the cached alias index (tests, and every CLI command that mutates aliases)."""
    global _INDEX
    _INDEX = None


def _masked_tokens(tokens: Sequence[str]) -> set[int]:
    """Token indices covered by a guard phrase."""
    masked: set[int] = set()
    n = len(tokens)
    for i in range(n):
        for length in range(min(_GUARD_MAX, n - i), 0, -1):
            if tuple(tokens[i : i + length]) in _GUARD_SET:
                masked.update(range(i, i + length))
                break
    return masked


def find_matches(index: AliasIndex, tokens: Sequence[str]) -> list[SkillMatch]:
    """Non-overlapping leftmost-longest alias matches, guard phrases excluded."""
    if not tokens or index.max_tokens == 0:
        return []
    masked = _masked_tokens(tokens)
    matches: list[SkillMatch] = []
    n = len(tokens)
    i = 0
    while i < n:
        if i in masked:
            i += 1
            continue
        hit: SkillMatch | None = None
        for length in range(min(index.max_tokens, n - i), 0, -1):
            window = range(i, i + length)
            if any(j in masked for j in window):
                continue
            key = tuple(tokens[i : i + length])
            skill_id = index.lookup(key)
            if skill_id is not None:
                hit = SkillMatch(skill_id, " ".join(key), i, i + length)
                break
        if hit is None:
            i += 1
        else:
            matches.append(hit)
            i = hit.end
    return matches


def scan(session: Session, raw_text: str) -> list[SkillMatch]:
    """All alias occurrences in ``raw_text``, in order. Never writes anything."""
    return find_matches(get_index(session), tokenize(raw_text))


def resolve(
    session: Session,
    raw_text: str,
    posting_id: int | None = None,
    *,
    record_candidate: bool = True,
) -> int | None:
    """Resolve a raw requirement span to a canonical skill id, or ``None``.

    On no match the span is recorded in ``skill_alias_candidate`` for manual review.
    This function never creates a ``canonical_skill``.
    """
    norm = normalize(raw_text)
    if not norm:
        return None
    matches = find_matches(get_index(session), tuple(norm.split(" ")))
    if matches:
        return matches[0].skill_id
    if record_candidate:
        _record_candidate(session, raw_text, norm, posting_id)
    return None


def resolve_all(
    session: Session,
    texts: Iterable[str],
    posting_id: int | None = None,
    *,
    record_candidates: bool = True,
) -> list[int | None]:
    """Batch form of :func:`resolve`; loads the alias index once."""
    get_index(session)
    return [
        resolve(session, text, posting_id, record_candidate=record_candidates) for text in texts
    ]


def _record_candidate(
    session: Session, raw_text: str, raw_norm: str, posting_id: int | None
) -> None:
    """Insert or bump a review-queue row. Concurrency-safe via ON CONFLICT DO UPDATE.

    An already-triaged candidate (approved/rejected/promoted) keeps its status; only the
    counter and the last-seen timestamp move, so a rejected string does not silently
    reappear as pending.
    """
    now = dt.datetime.now(dt.UTC)
    table = SkillAliasCandidate.__table__
    stmt = pg_insert(table).values(
        raw_text=raw_text[:512],
        raw_norm=raw_norm[:512],
        seen_count=1,
        example_posting_id=posting_id,
        status=CandidateStatus.pending.value,
        first_seen_at=now,
        last_seen_at=now,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[table.c.raw_norm],
        set_={
            "seen_count": table.c.seen_count + 1,
            "last_seen_at": now,
            "example_posting_id": func.coalesce(
                table.c.example_posting_id, stmt.excluded.example_posting_id
            ),
        },
    )
    session.execute(stmt)
    log.debug("taxonomy.candidate_recorded", raw_norm=raw_norm[:80], posting_id=posting_id)
