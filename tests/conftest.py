"""Shared test fixtures.

Two database strategies:

  * `db_session` - a real Postgres via JME_TEST_DATABASE_URL (defaults to the
    docker-compose instance, database `jme_test`). Schema is created from the ORM
    metadata, and each test runs inside a transaction that is rolled back. Tests that
    need pgvector or real SQL semantics use this and are marked `integration`.
  * plain unit tests should not touch the database at all.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from jme.models import Base

TEST_DB_URL = os.environ.get(
    "JME_TEST_DATABASE_URL", "postgresql+psycopg://jme:jme@localhost:5433/jme_test"
)


def _postgres_available(url: str) -> bool:
    try:
        engine = create_engine(url, pool_pre_ping=True)
        with engine.connect():
            return True
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture(scope="session")
def db_engine():
    admin_url = TEST_DB_URL.rsplit("/", 1)[0] + "/postgres"
    if _postgres_available(admin_url):
        admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
        dbname = TEST_DB_URL.rsplit("/", 1)[1]
        with admin.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": dbname}
            ).scalar()
            if not exists:
                conn.exec_driver_sql(f'CREATE DATABASE "{dbname}"')
        admin.dispose()

    if not _postgres_available(TEST_DB_URL):
        pytest.skip("no Postgres at JME_TEST_DATABASE_URL; run `docker compose up -d`")

    engine = create_engine(TEST_DB_URL)

    # Two pytest processes sharing one test database will race here: one drops the
    # schema while the other is creating it, and you get a duplicate-key error on
    # pg_type or a half-built schema. A session-scoped advisory lock serialises the
    # DDL and is released automatically when the connection closes.
    #
    # Serialising is the safe default, not the fast one. For genuinely parallel
    # suites, point each process at its own database via JME_TEST_DATABASE_URL.
    setup_conn = engine.connect()
    setup_conn.exec_driver_sql("SELECT pg_advisory_lock(hashtext('jme_test_schema'))")
    try:
        setup_conn.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS vector")
        setup_conn.commit()
        Base.metadata.drop_all(setup_conn)
        Base.metadata.create_all(setup_conn)
        setup_conn.commit()
    except Exception:
        setup_conn.close()
        engine.dispose()
        raise

    try:
        yield engine
    finally:
        # Holding the lock for the whole session is the point: it keeps a second
        # process from dropping the schema out from under a running test.
        setup_conn.close()
        engine.dispose()


@pytest.fixture
def db_session(db_engine) -> Session:
    """A session bound to a transaction that is rolled back after the test.

    `join_transaction_mode="create_savepoint"` is load-bearing, not decoration. Plenty
    of the code under test owns its own transaction boundary and calls
    `session.commit()` (the matcher does it per posting, so a crash mid-run keeps the
    matches it already wrote). Without the savepoint the first such commit would end
    this fixture's outer transaction, the rollback below would have nothing left to
    undo, and the rows would leak into the next test - which is exactly how the report
    suite used to fail with a duplicate `evidence_version` key when it ran after the
    matcher suite. With it, an inner commit releases a savepoint and the outer rollback
    still wipes everything.
    """
    connection = db_engine.connect()
    transaction = connection.begin()
    session = sessionmaker(
        bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )()
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def fake_redis():
    import fakeredis

    return fakeredis.FakeRedis(decode_responses=True)
