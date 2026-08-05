"""Seeder behaviour: idempotency, reconciliation, and the hard failures.

These tests seed from an empty database, so this module deliberately does not use the
module-scoped seeded fixture from conftest (two open transactions inserting the same
`canonical_skill.name` would block on the unique index).
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from jme.models import CanonicalSkill, SkillAlias, SkillCategory
from jme.taxonomy import resolver
from jme.taxonomy.normalize import normalize
from jme.taxonomy.seed import SeedError, SkillSpec, seed

pytestmark = pytest.mark.integration


def counts(session) -> tuple[int, int]:
    return (
        session.scalar(select(func.count(CanonicalSkill.id))),
        session.scalar(select(func.count(SkillAlias.id))),
    )


SMALL = [
    SkillSpec(name="Go", category=SkillCategory.language, aliases=("Golang", "go lang")),
    SkillSpec(name="Kubernetes", category=SkillCategory.infra, aliases=("K8s",)),
    SkillSpec(
        name="Communication", category=SkillCategory.soft, is_actionable=False, aliases=("comms",)
    ),
]


# --------------------------------------------------------------------------------------
# idempotency
# --------------------------------------------------------------------------------------


def test_seed_is_idempotent_with_the_real_taxonomy(db_session, specs):
    first = seed(db_session, specs)
    db_session.flush()
    after_first = counts(db_session)

    assert first.skills_created == len(specs)
    assert after_first[0] == len(specs)
    assert after_first[1] == sum(len(s.desired_aliases()) for s in specs)

    second = seed(db_session, specs)
    db_session.flush()

    assert second.skills_created == 0
    assert second.skills_updated == 0
    assert second.aliases_added == 0
    assert second.aliases_removed == 0
    assert second.aliases_retitled == 0
    assert second.changed is False
    assert second.skills_unchanged == len(specs)
    assert counts(db_session) == after_first


def test_second_run_creates_no_duplicate_aliases(db_session, specs):
    seed(db_session, specs)
    seed(db_session, specs)
    db_session.flush()
    dupes = db_session.execute(
        select(SkillAlias.alias_norm, func.count(SkillAlias.id))
        .group_by(SkillAlias.alias_norm)
        .having(func.count(SkillAlias.id) > 1)
    ).all()
    assert dupes == []


def test_identity_alias_is_created_for_every_skill(db_session, specs):
    seed(db_session, specs)
    db_session.flush()
    rows = dict(db_session.execute(select(CanonicalSkill.name, CanonicalSkill.id)).all())
    aliases = {
        (a.canonical_skill_id, a.alias_norm) for a in db_session.scalars(select(SkillAlias)).all()
    }
    for name, skill_id in rows.items():
        assert (skill_id, normalize(name)) in aliases, f"no identity alias for {name}"


# --------------------------------------------------------------------------------------
# reconciliation
# --------------------------------------------------------------------------------------


def test_dry_run_writes_nothing(db_session):
    report = seed(db_session, SMALL, dry_run=True)
    db_session.flush()
    assert report.skills_created == 3
    assert report.aliases_added == 7  # 3 identities + golang + go lang + k8s + comms
    assert counts(db_session) == (0, 0)
    assert any(line.startswith("+ skill Go") for line in report.changes)


def test_added_alias_is_inserted_on_reseed(db_session):
    seed(db_session, SMALL)
    db_session.flush()
    before = counts(db_session)

    updated = list(SMALL)
    updated[0] = SkillSpec(
        name="Go", category=SkillCategory.language, aliases=("Golang", "go lang", "go programming")
    )
    report = seed(db_session, updated)
    db_session.flush()

    assert report.aliases_added == 1
    assert report.aliases_removed == 0
    assert counts(db_session) == (before[0], before[1] + 1)


def test_removed_alias_is_deleted_on_reseed(db_session):
    seed(db_session, SMALL)
    db_session.flush()

    updated = list(SMALL)
    updated[0] = SkillSpec(name="Go", category=SkillCategory.language, aliases=("Golang",))
    report = seed(db_session, updated)
    db_session.flush()

    assert report.aliases_removed == 1
    assert (
        db_session.scalar(select(func.count(SkillAlias.id)).where(SkillAlias.alias_norm == "go lang"))
        == 0
    )


def test_alias_can_move_between_skills(db_session):
    seed(db_session, SMALL)
    db_session.flush()
    moved = [
        SkillSpec(name="Go", category=SkillCategory.language, aliases=("go lang",)),
        SkillSpec(name="Kubernetes", category=SkillCategory.infra, aliases=("K8s", "Golang")),
        SMALL[2],
    ]
    seed(db_session, moved)
    db_session.flush()

    alias = db_session.scalar(select(SkillAlias).where(SkillAlias.alias_norm == "golang"))
    k8s = db_session.scalar(select(CanonicalSkill).where(CanonicalSkill.name == "Kubernetes"))
    assert alias.canonical_skill_id == k8s.id


def test_skill_attributes_are_updated(db_session):
    seed(db_session, SMALL)
    db_session.flush()
    changed = list(SMALL)
    changed[1] = SkillSpec(name="Kubernetes", category=SkillCategory.cloud, aliases=("K8s",))
    report = seed(db_session, changed)
    db_session.flush()

    assert report.skills_updated == 1
    row = db_session.scalar(select(CanonicalSkill).where(CanonicalSkill.name == "Kubernetes"))
    assert row.category is SkillCategory.cloud


def test_orphan_skills_are_reported_but_never_deleted(db_session):
    seed(db_session, SMALL)
    db_session.flush()
    report = seed(db_session, SMALL[:2])
    db_session.flush()

    assert report.orphan_skills == ("Communication",)
    assert (
        db_session.scalar(
            select(func.count(CanonicalSkill.id)).where(CanonicalSkill.name == "Communication")
        )
        == 1
    )
    # and its aliases survive, because posting_requirement rows may point at them
    assert db_session.scalar(select(func.count(SkillAlias.id)).where(SkillAlias.alias_norm == "comms")) == 1


def test_seed_refuses_ambiguous_aliases(db_session):
    bad = [
        SkillSpec(name="Go", category=SkillCategory.language, aliases=("golang",)),
        SkillSpec(name="Gopher", category=SkillCategory.soft, aliases=("Golang",)),
    ]
    with pytest.raises(SeedError, match="ambiguous aliases"):
        seed(db_session, bad)


def test_seed_refuses_duplicate_names(db_session):
    bad = [
        SkillSpec(name="Go", category=SkillCategory.language),
        SkillSpec(name="GO", category=SkillCategory.language),
    ]
    with pytest.raises(SeedError, match="duplicate skill names"):
        seed(db_session, bad)


# --------------------------------------------------------------------------------------
# seeding feeds the resolver
# --------------------------------------------------------------------------------------


def test_resolution_works_immediately_after_seeding(db_session):
    seed(db_session, SMALL)
    db_session.flush()
    go = db_session.scalar(select(CanonicalSkill).where(CanonicalSkill.name == "Go"))
    assert resolver.resolve(db_session, "Golang", record_candidate=False) == go.id
    assert resolver.resolve(db_session, "Django", record_candidate=False) is None


def test_seed_invalidates_the_resolver_cache(db_session):
    seed(db_session, SMALL)
    db_session.flush()
    assert resolver.resolve(db_session, "gopher", record_candidate=False) is None

    updated = list(SMALL)
    updated[0] = SkillSpec(
        name="Go", category=SkillCategory.language, aliases=("Golang", "go lang", "gopher")
    )
    seed(db_session, updated)
    db_session.flush()
    go = db_session.scalar(select(CanonicalSkill).where(CanonicalSkill.name == "Go"))
    assert resolver.resolve(db_session, "gopher", record_candidate=False) == go.id
