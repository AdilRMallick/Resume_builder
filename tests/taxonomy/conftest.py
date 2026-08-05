"""Fixtures for the taxonomy tests.

The full taxonomy (229 skills / ~1300 aliases) is seeded **once per test module** into an
outer transaction that is rolled back at the end; each test then runs inside a SAVEPOINT
so writes (candidate rows) do not leak between tests. Seeding per-test would be correct
but slow, and seeding from a second connection while the module transaction is open would
block on the `canonical_skill.name` unique index -- so tests that need an empty taxonomy
live in their own module.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from jme.models import CanonicalSkill
from jme.taxonomy import resolver
from jme.taxonomy.seed import load_specs, seed


@pytest.fixture(scope="session")
def specs():
    return load_specs()


@pytest.fixture(scope="module")
def _seeded_taxonomy(db_engine, specs):
    connection = db_engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, expire_on_commit=False)
    seed(session, specs)
    session.flush()
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()
        resolver.invalidate_cache()


@pytest.fixture
def tx(_seeded_taxonomy) -> Session:
    """Seeded session inside a per-test SAVEPOINT."""
    session = _seeded_taxonomy
    resolver.invalidate_cache()
    nested = session.begin_nested()
    try:
        yield session
    finally:
        if nested.is_active:
            nested.rollback()
        resolver.invalidate_cache()


@pytest.fixture
def skill_ids(tx) -> dict[str, int]:
    return {name: sid for name, sid in tx.execute(select(CanonicalSkill.name, CanonicalSkill.id))}


@pytest.fixture
def skill_names(tx) -> dict[int, str]:
    return {sid: name for name, sid in tx.execute(select(CanonicalSkill.name, CanonicalSkill.id))}
