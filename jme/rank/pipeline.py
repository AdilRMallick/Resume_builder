"""The two-stage funnel, wired together and instrumented.

`run()` is deterministic: given the same postings, the same requirement rows and the
same evidence version, it produces byte-identical rankings, including the order of
tied scores. Determinism comes from three places and nowhere else:

  * stage 1 returns survivors `ORDER BY id`;
  * scores are rounded to the precision of the column they are stored in before they
    are compared, so equal scores really are equal;
  * the sort key is `(-coarse_score, posting_id)`, so ties are broken on a stable
    primary key rather than on whatever order Postgres happened to return rows in.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from jme.config import Settings, get_settings
from jme.embeddings import EmbeddingProvider
from jme.logging import get_logger
from jme.metrics import Timer, record
from jme.models import EvidenceVersion, ShortlistEntry
from jme.rank.filters import DROP_REASONS, FLAG_NAMES, FilterOutcome, apply_filters
from jme.rank.score import PostingScore, score_postings

logger = get_logger(__name__)

STAGE = "rank"


@dataclass(frozen=True)
class RankResult:
    run_id: str
    evidence_version: int
    entries: list[ShortlistEntry]
    scores: list[PostingScore]
    outcome: FilterOutcome

    @property
    def shortlist_size(self) -> int:
        return len(self.entries)


def current_evidence_version(session: Session) -> int:
    """The corpus version the shortlist is keyed on. Absent table row means v1."""
    version = session.execute(select(EvidenceVersion.version).where(EvidenceVersion.id == 1)).scalar()
    return int(version) if version is not None else 1


def run(
    session: Session,
    run_id: str,
    limit: int | None = None,
    *,
    settings: Settings | None = None,
    provider: EmbeddingProvider | None = None,
    dry_run: bool = False,
) -> list[ShortlistEntry]:
    """Filter, rank, persist. Returns the shortlist in rank order (rank 1 first).

    `limit` overrides `settings.shortlist_size`. `dry_run=True` computes everything
    and writes nothing -- neither shortlist rows nor metrics.
    """
    return rank(
        session, run_id, limit, settings=settings, provider=provider, dry_run=dry_run
    ).entries


def rank(
    session: Session,
    run_id: str,
    limit: int | None = None,
    *,
    settings: Settings | None = None,
    provider: EmbeddingProvider | None = None,
    dry_run: bool = False,
) -> RankResult:
    """Same as `run()` but returns the full result, including the stage-1 funnel."""
    settings = settings or get_settings()
    top_n = limit if limit is not None else settings.shortlist_size
    evidence_version = current_evidence_version(session)

    with Timer() as stage1_timer:
        outcome = apply_filters(session, settings)

    with Timer() as stage2_timer:
        scores = score_postings(session, outcome.kept, settings, provider)

    shortlisted = scores[: max(top_n, 0)]

    entries: list[ShortlistEntry] = []
    if not dry_run:
        # Re-running the same run_id replaces its shortlist rather than colliding with
        # the (run_id, posting_id) unique constraint.
        session.execute(delete(ShortlistEntry).where(ShortlistEntry.run_id == run_id))

    for position, score in enumerate(shortlisted, start=1):
        entry = ShortlistEntry(
            run_id=run_id,
            posting_id=score.posting_id,
            rank=position,
            coarse_score=score.coarse_score,
            evidence_version=evidence_version,
        )
        entries.append(entry)

    if not dry_run:
        session.add_all(entries)
        _record_metrics(
            session,
            run_id,
            outcome=outcome,
            scored=len(scores),
            shortlisted=len(entries),
            evidence_version=evidence_version,
            stage1_seconds=stage1_timer.seconds,
            stage2_seconds=stage2_timer.seconds,
        )
        session.flush()

    logger.info(
        "rank.done",
        run_id=run_id,
        evidence_version=evidence_version,
        total=outcome.total_postings,
        after_filters=len(outcome.kept),
        after_ranking=len(entries),
        dry_run=dry_run,
        stage1_seconds=round(stage1_timer.seconds, 4),
        stage2_seconds=round(stage2_timer.seconds, 4),
    )
    return RankResult(
        run_id=run_id,
        evidence_version=evidence_version,
        entries=entries,
        scores=scores,
        outcome=outcome,
    )


def _record_metrics(
    session: Session,
    run_id: str,
    *,
    outcome: FilterOutcome,
    scored: int,
    shortlisted: int,
    evidence_version: int,
    stage1_seconds: float,
    stage2_seconds: float,
) -> None:
    record(session, run_id, STAGE, "total_postings", outcome.total_postings)
    record(session, run_id, STAGE, "after_filters", len(outcome.kept))
    record(session, run_id, STAGE, "after_ranking", shortlisted)
    record(session, run_id, STAGE, "scored_postings", scored)
    record(session, run_id, STAGE, "evidence_version", evidence_version)
    record(session, run_id, STAGE, "stage1_seconds", round(stage1_seconds, 6))
    record(session, run_id, STAGE, "stage2_seconds", round(stage2_seconds, 6))

    # Every reason gets a row even at zero: a missing bucket in a funnel is
    # indistinguishable from a zero, and that ambiguity is how funnels start lying.
    for reason in DROP_REASONS:
        record(
            session,
            run_id,
            STAGE,
            f"drop_{reason}",
            outcome.drop_counts.get(reason, 0),
            labels={"kind": "filter_drop", "rule": reason},
        )
    for flag in FLAG_NAMES:
        record(
            session,
            run_id,
            STAGE,
            f"flag_{flag}",
            outcome.flag_counts.get(flag, 0),
            labels={"kind": "filter_flag", "rule": flag},
        )


def funnel(session: Session, run_id: str) -> list[tuple[str, float, dict | None]]:
    """(metric, value, labels) for one run's rank stage, in funnel order."""
    order = [
        "total_postings",
        *[f"drop_{r}" for r in DROP_REASONS],
        "after_filters",
        "scored_postings",
        "after_ranking",
        *[f"flag_{f}" for f in FLAG_NAMES],
        "stage1_seconds",
        "stage2_seconds",
        "evidence_version",
    ]
    rows = session.execute(
        text(
            """
            SELECT metric, value, labels
            FROM run_metric
            WHERE run_id = :run_id AND stage = :stage
            ORDER BY id
            """
        ),
        {"run_id": run_id, "stage": STAGE},
    ).all()
    found = {metric: (float(value), labels) for metric, value, labels in rows}
    ordered = [(m, *found[m]) for m in order if m in found]
    extra = [(m, *found[m]) for m in sorted(found) if m not in order]
    return [(m, v, lbl) for m, v, lbl in ordered + extra]


def latest_run_id(session: Session) -> str | None:
    return session.execute(
        text(
            """
            SELECT run_id FROM run_metric
            WHERE stage = :stage
            GROUP BY run_id
            ORDER BY max(recorded_at) DESC
            LIMIT 1
            """
        ),
        {"stage": STAGE},
    ).scalar()
