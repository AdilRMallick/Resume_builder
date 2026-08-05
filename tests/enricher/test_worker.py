"""The `jme:enrich` consumer: idempotency under at-least-once delivery, and dead letters."""

from __future__ import annotations

import json
from contextlib import contextmanager

import pytest
from sqlalchemy import select

from jme.config import GROUP_ENRICH, STREAM_ENRICH, STREAM_ENRICH_DEAD
from jme.enricher.worker import build_consumer, make_handler, process_message
from jme.models import PostingRequirement
from jme.queue import Message, PermanentFailure, Producer

pytestmark = pytest.mark.integration

JD = """\
Software Engineer, New Grad - Queue Co

Requirements
- Proficiency in Python and experience with PostgreSQL.
- Familiarity with Kubernetes.

Preferred
- Experience with Apache Kafka.
"""

PAYLOAD = {
    "requirements": [
        {"raw_text": "Python", "importance": "required", "confidence": 0.95},
        {"raw_text": "PostgreSQL", "importance": "required", "confidence": 0.9},
        {"raw_text": "Kubernetes", "importance": "required", "confidence": 0.85},
        {"raw_text": "Apache Kafka", "importance": "preferred", "confidence": 0.7},
    ]
}


@pytest.fixture
def session_factory(db_session):
    """Hands the handler the test's transactional session.

    The handler commits; the fixture's outer transaction is still rolled back afterwards,
    so a real commit is exercised without leaving rows behind.
    """

    @contextmanager
    def factory():
        yield db_session
        db_session.commit()

    return factory


def _count(session, posting_id: int) -> int:
    return len(
        list(
            session.scalars(
                select(PostingRequirement).where(PostingRequirement.posting_id == posting_id)
            )
        )
    )


def test_redelivery_of_the_same_message_does_not_duplicate_rows(
    db_session, make_posting, stub_llm, no_resolver, session_factory, fake_redis
):
    posting_id = make_posting(db_session, JD)
    db_session.commit()

    llm = stub_llm([PAYLOAD])
    handler = make_handler(
        session_factory=session_factory, run_id="run-queue", resolver=no_resolver, llm_call=llm
    )

    producer = Producer(fake_redis, STREAM_ENRICH)
    message_id = producer.publish(
        {"posting_id": posting_id, "jd_sha256": "deadbeef", "adapter": "greenhouse",
         "char_count": len(JD)}
    )

    # claim_interval_sec is pushed out of reach because fakeredis's XAUTOCLAIM returns a
    # cursor with different semantics to Redis's and the sweep would not terminate. The
    # redelivery below is driven with XREADGROUP id "0", which is the same PEL replay a
    # restarted worker sees.
    group = build_consumer(fake_redis, "consumer-a", claim_interval_sec=1e9)
    group.ensure_group()

    first = group.read()
    assert [m.id for m in first] == [message_id]
    handler(first[0])
    after_first = _count(db_session, posting_id)
    assert after_first == 4

    # The worker commits but dies before XACK, so the message is still pending.
    assert fake_redis.xpending(STREAM_ENRICH, GROUP_ENRICH)["pending"] == 1
    replay = fake_redis.xreadgroup(
        groupname=GROUP_ENRICH, consumername="consumer-a", streams={STREAM_ENRICH: "0"}
    )
    redelivered = [Message(mid, json.loads(values["payload"]), 2) for mid, values in replay[0][1]]
    assert [m.id for m in redelivered] == [message_id]
    handler(redelivered[0])

    assert _count(db_session, posting_id) == after_first, "redelivery must not duplicate rows"
    group.ack(message_id)
    assert fake_redis.xpending(STREAM_ENRICH, GROUP_ENRICH)["pending"] == 0


def test_duplicate_publish_of_the_same_posting_is_idempotent(
    db_session, make_posting, stub_llm, no_resolver, session_factory, fake_redis
):
    """The fetcher retrying its XADD produces two distinct message ids, same posting."""
    posting_id = make_posting(db_session, JD)
    db_session.commit()

    handler = make_handler(
        session_factory=session_factory,
        run_id="run-queue-dup",
        resolver=no_resolver,
        llm_call=stub_llm([PAYLOAD]),
    )
    producer = Producer(fake_redis, STREAM_ENRICH)
    payload = {"posting_id": posting_id, "jd_sha256": "deadbeef", "adapter": "lever",
               "char_count": len(JD)}
    producer.publish(payload)
    producer.publish(payload)

    group = build_consumer(fake_redis, "consumer-a", claim_interval_sec=1e9)
    group.ensure_group()
    messages = group.read()
    assert len(messages) == 2
    for message in messages:
        handler(message)
        group.ack(message.id)

    assert _count(db_session, posting_id) == 4


def test_posting_without_jd_text_is_a_permanent_failure(
    db_session, make_posting, stub_llm, no_resolver, session_factory, fake_redis
):
    posting_id = make_posting(db_session, "")
    db_session.commit()

    llm = stub_llm([PAYLOAD])
    handler = make_handler(
        session_factory=session_factory, resolver=no_resolver, llm_call=llm
    )
    producer = Producer(fake_redis, STREAM_ENRICH)
    message_id = producer.publish(
        {"posting_id": posting_id, "jd_sha256": "", "adapter": "fallback", "char_count": 0}
    )

    group = build_consumer(fake_redis, "consumer-a", claim_interval_sec=1e9)
    group.ensure_group()
    message = group.read()[0]

    with pytest.raises(PermanentFailure, match="no job description text"):
        handler(message)
    assert llm.call_count == 0, "no LLM spend on a posting with nothing to extract"

    # what the consumer loop does with a PermanentFailure
    group.dead_letter(message.id, message.payload, "no job description text", 1)
    dead = fake_redis.xrange(STREAM_ENRICH_DEAD)
    assert len(dead) == 1
    assert dead[0][1]["reason"] == "no job description text"
    assert fake_redis.xpending(STREAM_ENRICH, GROUP_ENRICH)["pending"] == 0
    assert message_id


def test_unknown_posting_is_a_permanent_failure(db_session, stub_llm, no_resolver):
    with pytest.raises(PermanentFailure, match="no job description text"):
        process_message(
            db_session,
            {"posting_id": 999_999_999, "jd_sha256": "x", "adapter": "lever", "char_count": 1},
            resolver=no_resolver,
            llm_call=stub_llm([PAYLOAD]),
        )


def test_malformed_payload_is_a_permanent_failure(db_session, stub_llm, no_resolver):
    with pytest.raises(PermanentFailure, match="posting_id"):
        process_message(
            db_session,
            {"jd_sha256": "x"},
            resolver=no_resolver,
            llm_call=stub_llm([PAYLOAD]),
        )


def test_handler_passes_company_and_title_into_the_prompt(
    db_session, make_posting, stub_llm, no_resolver, session_factory, fake_redis
):
    posting_id = make_posting(db_session, JD, company="Queue Co", title="SWE New Grad")
    db_session.commit()

    llm = stub_llm([PAYLOAD])
    handler = make_handler(
        session_factory=session_factory, resolver=no_resolver, llm_call=llm
    )
    producer = Producer(fake_redis, STREAM_ENRICH)
    producer.publish({"posting_id": posting_id, "jd_sha256": "d", "adapter": "greenhouse",
                      "char_count": len(JD)})
    group = build_consumer(fake_redis, "consumer-a", claim_interval_sec=1e9)
    group.ensure_group()
    handler(group.read()[0])

    prompt = llm.calls[0]["user"]
    assert "Company: Queue Co" in prompt
    assert "Role: SWE New Grad" in prompt
