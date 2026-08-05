"""Ingest: source records -> chunks -> upserted, embedded `evidence_chunk` rows.

The whole design turns on one number: embedding is the only expensive step, so a
chunk is re-embedded **only when its text hash changes**. Everything else here exists
to make that hash comparison meaningful:

  * Chunk identity is `(source_type, source_ref, ordinal)`, backed by
    `uq_chunk_source_ordinal`. Ordinals come from the deterministic chunker, so an
    unedited document maps chunk-for-chunk onto the rows already in the table.
  * A chunk whose source or ordinal disappears is soft-deleted, never dropped, because
    `match_citation.evidence_chunk_id` points at it and history should stay readable.
  * A soft-deleted chunk whose text comes back is revived in place, so citations that
    referenced it become valid again instead of pointing at a tombstone.

Ingestion runs in two phases: plan (read-only diff) and apply (writes + embeddings).
`--dry-run` is just phase one, which means the dry-run report is computed by the same
code path that does the real work, not a parallel approximation of it.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from jme.embeddings import EmbeddingProvider, get_provider
from jme.evidence.chunker import Chunk, chunk_markdown
from jme.evidence.sources import SourceRecord, load_sources
from jme.evidence.version import bump_version, current_version
from jme.logging import get_logger
from jme.models import CanonicalSkill, EvidenceChunk, EvidenceSkill

log = get_logger(__name__)

EMBED_BATCH = 64


class TaxonomyEmptyError(RuntimeError):
    """Raised when tagging is attempted before the skill taxonomy exists."""


class UnknownSkillError(ValueError):
    """Raised when a tag names a skill that is not in `canonical_skill`."""


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------------------
# results
# --------------------------------------------------------------------------------------


@dataclass
class IngestStats:
    sources: int = 0
    chunks: int = 0
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    deleted: int = 0
    revived: int = 0
    embedded: int = 0
    version_before: int = 0
    version_after: int = 0
    dry_run: bool = False

    @property
    def changed(self) -> bool:
        """Did anything happen that should move the corpus version?"""
        return bool(self.added or self.updated or self.deleted or self.revived)

    def as_dict(self) -> dict[str, object]:
        return {
            "sources": self.sources,
            "chunks": self.chunks,
            "added": self.added,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "deleted": self.deleted,
            "revived": self.revived,
            "embedded": self.embedded,
            "version_before": self.version_before,
            "version_after": self.version_after,
            "changed": self.changed,
            "dry_run": self.dry_run,
        }


@dataclass
class _Action:
    kind: str  # insert | update | revive | unchanged | delete
    source_type: str
    source_ref: str
    ordinal: int
    row: EvidenceChunk | None = None
    chunk: Chunk | None = None
    text_sha: str | None = None


@dataclass
class _Plan:
    actions: list[_Action] = field(default_factory=list)
    sources: int = 0
    chunks: int = 0

    def counts(self) -> dict[str, int]:
        out = {"insert": 0, "update": 0, "revive": 0, "unchanged": 0, "delete": 0}
        for action in self.actions:
            out[action.kind] += 1
        return out


# --------------------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------------------


def ingest(
    session: Session,
    records: Iterable[SourceRecord],
    *,
    scanned_types: set[str] | None = None,
    provider: EmbeddingProvider | None = None,
    dry_run: bool = False,
) -> IngestStats:
    """Diff `records` against the corpus and apply the result.

    `scanned_types` scopes soft-deletion. Only source types that were actually looked
    at this run can lose chunks; ingesting a markdown directory must never tombstone
    repo READMEs that simply were not fetched.
    """
    records = list(records)
    if scanned_types is None:
        scanned_types = {r.source_type for r in records}

    plan = _plan(session, records, scanned_types)
    counts = plan.counts()

    stats = IngestStats(
        sources=plan.sources,
        chunks=plan.chunks,
        added=counts["insert"],
        updated=counts["update"],
        unchanged=counts["unchanged"],
        deleted=counts["delete"],
        revived=counts["revive"],
        version_before=current_version(session),
        dry_run=dry_run,
    )

    if dry_run:
        stats.version_after = stats.version_before + (1 if stats.changed else 0)
        log.info("evidence.ingest_dry_run", **stats.as_dict())
        return stats

    if not stats.changed:
        stats.version_after = stats.version_before
        log.info("evidence.ingest_noop", **stats.as_dict())
        return stats

    reason = (
        f"ingest: +{stats.added} ~{stats.updated} "
        f"-{stats.deleted} revived={stats.revived}"
    )
    version = bump_version(session, reason)
    stats.version_after = version
    stats.embedded = _apply(session, plan, version, provider or get_provider())
    session.flush()
    log.info("evidence.ingest", **stats.as_dict())
    return stats


def ingest_from_config(
    session: Session,
    *,
    directory: str | Path | None = None,
    repos: Sequence[str] | None = None,
    provider: EmbeddingProvider | None = None,
    dry_run: bool = False,
) -> IngestStats:
    records, scanned = load_sources(directory=directory, repos=repos)
    return ingest(
        session, records, scanned_types=scanned, provider=provider, dry_run=dry_run
    )


def live_chunks(
    session: Session,
    *,
    source_type: str | None = None,
    source_ref: str | None = None,
    limit: int | None = None,
) -> list[EvidenceChunk]:
    """Every chunk that is currently part of the corpus.

    Soft-deleted rows stay in the table for citation integrity but must never reach
    retrieval, so this is the only query the rest of the system should use.
    """
    stmt = select(EvidenceChunk).where(EvidenceChunk.deleted_at.is_(None))
    if source_type:
        stmt = stmt.where(EvidenceChunk.source_type == source_type)
    if source_ref:
        stmt = stmt.where(EvidenceChunk.source_ref == source_ref)
    stmt = stmt.order_by(
        EvidenceChunk.source_type, EvidenceChunk.source_ref, EvidenceChunk.ordinal
    )
    if limit:
        stmt = stmt.limit(limit)
    return list(session.execute(stmt).scalars())


def reembed(
    session: Session,
    *,
    force: bool = False,
    provider: EmbeddingProvider | None = None,
) -> int:
    """Fill in missing embeddings, or recompute all of them with `force`.

    Backfilling a missing embedding does not change what the corpus *says*, so it does
    not bump the version. `--force` does: it usually means the provider or model
    changed, and every cached match was computed against the old vector space.
    """
    provider = provider or get_provider()
    stmt = select(EvidenceChunk).where(EvidenceChunk.deleted_at.is_(None))
    if not force:
        stmt = stmt.where(
            (EvidenceChunk.embedding.is_(None))
            | (EvidenceChunk.embedding_model != provider.name)
        )
    rows = list(session.execute(stmt.order_by(EvidenceChunk.id)).scalars())
    if not rows:
        return 0

    for start in range(0, len(rows), EMBED_BATCH):
        batch = rows[start : start + EMBED_BATCH]
        vectors = provider.embed([row.text for row in batch])
        for row, vector in zip(batch, vectors, strict=True):
            row.embedding = vector
            row.embedding_model = provider.name
    session.flush()

    if force:
        bump_version(session, f"reembed --force with {provider.name} ({len(rows)} chunks)")
    log.info("evidence.reembed", count=len(rows), model=provider.name, force=force)
    return len(rows)


# --------------------------------------------------------------------------------------
# manual tags
# --------------------------------------------------------------------------------------


def tag_chunk(session: Session, chunk_id: int, skill_names: Sequence[str]) -> list[str]:
    """Attach canonical skills to a chunk. Returns the names actually added.

    A tag change is a corpus change: it alters what retrieval will surface for a
    skill, so it bumps the version exactly like an edit does. Tagging with skills that
    are already attached adds nothing and bumps nothing.
    """
    chunk = _require_chunk(session, chunk_id)
    skills = _resolve_skills(session, skill_names)

    existing = set(
        session.execute(
            select(EvidenceSkill.canonical_skill_id).where(
                EvidenceSkill.evidence_chunk_id == chunk.id
            )
        ).scalars()
    )
    added = [s for s in skills if s.id not in existing]
    if not added:
        return []

    session.execute(
        pg_insert(EvidenceSkill)
        .values([{"evidence_chunk_id": chunk.id, "canonical_skill_id": s.id} for s in added])
        .on_conflict_do_nothing()
    )
    session.flush()
    names = [s.name for s in added]
    bump_version(session, f"tag chunk {chunk.id}: +{', '.join(names)}")
    return names


def untag_chunk(session: Session, chunk_id: int, skill_names: Sequence[str]) -> list[str]:
    """Remove canonical skills from a chunk. Returns the names actually removed."""
    chunk = _require_chunk(session, chunk_id)
    skills = _resolve_skills(session, skill_names)

    removed: list[str] = []
    for skill in skills:
        row = session.get(EvidenceSkill, (chunk.id, skill.id))
        if row is not None:
            session.delete(row)
            removed.append(skill.name)
    if not removed:
        return []
    session.flush()
    bump_version(session, f"untag chunk {chunk.id}: -{', '.join(removed)}")
    return removed


def chunk_skills(session: Session, chunk_id: int) -> list[str]:
    return list(
        session.execute(
            select(CanonicalSkill.name)
            .join(EvidenceSkill, EvidenceSkill.canonical_skill_id == CanonicalSkill.id)
            .where(EvidenceSkill.evidence_chunk_id == chunk_id)
            .order_by(CanonicalSkill.name)
        ).scalars()
    )


def skills_by_chunk(session: Session, chunk_ids: Sequence[int]) -> dict[int, list[str]]:
    """Tags for many chunks in one query, for the `list` view."""
    if not chunk_ids:
        return {}
    rows = session.execute(
        select(EvidenceSkill.evidence_chunk_id, CanonicalSkill.name)
        .join(CanonicalSkill, CanonicalSkill.id == EvidenceSkill.canonical_skill_id)
        .where(EvidenceSkill.evidence_chunk_id.in_(list(chunk_ids)))
        .order_by(CanonicalSkill.name)
    ).all()
    out: dict[int, list[str]] = {}
    for chunk_id, name in rows:
        out.setdefault(chunk_id, []).append(name)
    return out


def _require_chunk(session: Session, chunk_id: int) -> EvidenceChunk:
    chunk = session.get(EvidenceChunk, chunk_id)
    if chunk is None:
        raise ValueError(f"no evidence chunk with id {chunk_id}")
    return chunk


def _resolve_skills(session: Session, names: Sequence[str]) -> list[CanonicalSkill]:
    if not names:
        return []
    total = session.execute(select(func.count()).select_from(CanonicalSkill)).scalar_one()
    if total == 0:
        raise TaxonomyEmptyError(
            "canonical_skill is empty, so there is nothing to tag with. "
            "Run `jme taxonomy seed` first."
        )

    wanted = [n.strip() for n in names if n.strip()]
    found = list(
        session.execute(
            select(CanonicalSkill).where(
                func.lower(CanonicalSkill.name).in_([n.lower() for n in wanted])
            )
        ).scalars()
    )
    by_lower = {s.name.lower(): s for s in found}
    missing = [n for n in wanted if n.lower() not in by_lower]
    if missing:
        raise UnknownSkillError(
            f"unknown canonical skill(s): {', '.join(missing)}. "
            "Tags must resolve to an existing canonical_skill; "
            "use `jme taxonomy` to add one."
        )
    # de-duplicate while preserving the order the user typed
    seen: set[int] = set()
    ordered: list[CanonicalSkill] = []
    for name in wanted:
        skill = by_lower[name.lower()]
        if skill.id not in seen:
            seen.add(skill.id)
            ordered.append(skill)
    return ordered


# --------------------------------------------------------------------------------------
# plan / apply
# --------------------------------------------------------------------------------------


def _plan(session: Session, records: list[SourceRecord], scanned_types: set[str]) -> _Plan:
    plan = _Plan(sources=len(records))

    existing = _load_existing(session, scanned_types)
    seen_keys: set[tuple[str, str]] = set()

    for record in records:
        key = (record.source_type, record.source_ref)
        seen_keys.add(key)
        by_ordinal = dict(existing.get(key, {}))
        chunks = chunk_markdown(record.text)
        plan.chunks += len(chunks)

        for chunk in chunks:
            row = by_ordinal.pop(chunk.ordinal, None)
            text_sha = sha256(chunk.text)
            if row is None:
                kind = "insert"
            elif row.deleted_at is not None:
                kind = "revive"
            elif row.text_sha256 == text_sha:
                kind = "unchanged"
            else:
                kind = "update"
            plan.actions.append(
                _Action(
                    kind=kind,
                    source_type=record.source_type,
                    source_ref=record.source_ref,
                    ordinal=chunk.ordinal,
                    row=row,
                    chunk=chunk,
                    text_sha=text_sha,
                )
            )

        # ordinals that used to exist but no longer do: the document got shorter
        for ordinal, row in sorted(by_ordinal.items()):
            if row.deleted_at is None:
                plan.actions.append(
                    _Action("delete", record.source_type, record.source_ref, ordinal, row=row)
                )

    # whole documents that vanished. Only for types that produced at least one record
    # this run: an empty result is far more likely to be a bad --dir than a corpus
    # someone actually emptied, and tombstoning everything on a typo is unrecoverable
    # without a re-embed.
    types_with_records = {r.source_type for r in records}
    for key, rows in existing.items():
        source_type, source_ref = key
        if key in seen_keys or source_type not in types_with_records:
            continue
        for ordinal, row in sorted(rows.items()):
            if row.deleted_at is None:
                plan.actions.append(_Action("delete", source_type, source_ref, ordinal, row=row))

    return plan


def _load_existing(
    session: Session, scanned_types: set[str]
) -> dict[tuple[str, str], dict[int, EvidenceChunk]]:
    if not scanned_types:
        return {}
    rows = session.execute(
        select(EvidenceChunk).where(EvidenceChunk.source_type.in_(sorted(scanned_types)))
    ).scalars()
    out: dict[tuple[str, str], dict[int, EvidenceChunk]] = {}
    for row in rows:
        out.setdefault((row.source_type, row.source_ref), {})[row.ordinal] = row
    return out


def _apply(session: Session, plan: _Plan, version: int, provider: EmbeddingProvider) -> int:
    now = dt.datetime.now(dt.UTC)

    # everything that needs a vector, embedded in as few provider calls as possible
    needs_embedding = [
        a
        for a in plan.actions
        if a.kind in ("insert", "update")
        or (a.kind == "revive" and a.row is not None and a.row.text_sha256 != a.text_sha)
        or (a.kind == "revive" and a.row is not None and a.row.embedding is None)
    ]
    vectors: dict[int, list[float]] = {}
    for start in range(0, len(needs_embedding), EMBED_BATCH):
        batch = needs_embedding[start : start + EMBED_BATCH]
        embedded = provider.embed([a.chunk.text for a in batch if a.chunk])
        for action, vector in zip(batch, embedded, strict=True):
            vectors[id(action)] = vector

    for action in plan.actions:
        if action.kind == "delete":
            assert action.row is not None
            action.row.deleted_at = now
            continue

        if action.kind == "unchanged":
            continue

        assert action.chunk is not None
        chunk = action.chunk
        if action.kind == "insert":
            row = EvidenceChunk(
                source_type=action.source_type,
                source_ref=action.source_ref,
                ordinal=chunk.ordinal,
                heading=chunk.heading,
                text=chunk.text,
                text_sha256=action.text_sha or sha256(chunk.text),
                token_estimate=chunk.token_estimate,
                embedding=vectors.get(id(action)),
                embedding_model=provider.name,
                evidence_version=version,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            continue

        row = action.row
        assert row is not None
        row.heading = chunk.heading
        row.text = chunk.text
        row.text_sha256 = action.text_sha or sha256(chunk.text)
        row.token_estimate = chunk.token_estimate
        row.evidence_version = version
        row.updated_at = now
        row.deleted_at = None
        vector = vectors.get(id(action))
        if vector is not None:
            row.embedding = vector
            row.embedding_model = provider.name

    session.flush()
    return len(vectors)
