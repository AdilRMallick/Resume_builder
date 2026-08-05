"""Consumer of the `jme:enrich` Redis stream.

The Go fetcher publishes one message per posting whose JD text it resolved:

    {"posting_id": int, "jd_sha256": str, "adapter": str, "char_count": int}

Delivery is at-least-once, so the handler has to be idempotent. It is, for two reasons:
extraction upserts on `uq_requirement_posting_raw_prompt`, and the LLM call itself is
content-addressed in `llm_cache` - a redelivery costs a Postgres round trip, not a dollar.

Failure policy:
  * no posting row, or no JD text -> `PermanentFailure`, straight to the dead letter
    stream. Retrying cannot conjure text that the fetcher never wrote, and an infinite
    retry loop on an unfetchable posting is how a queue fills up with garbage.
  * anything else (LLM timeout, database blip) -> raise, and let the consumer group retry
    until max_attempts, then dead letter.

Graceful shutdown is `jme.queue`'s: SIGTERM stops new reads, in-flight work finishes.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any

import redis
from sqlalchemy.orm import Session

from jme.config import (
    GROUP_ENRICH,
    STREAM_ENRICH,
    STREAM_ENRICH_DEAD,
    get_settings,
)
from jme.db import session_scope
from jme.enricher.extraction import LLMCall, Resolver, extract_requirements
from jme.logging import get_logger
from jme.metrics import new_run_id
from jme.models import Posting, PostingJD
from jme.queue import ConsumerGroup, Message, PermanentFailure

log = get_logger(__name__)

SessionFactory = Callable[[], AbstractContextManager[Session]]


def default_consumer_name() -> str:
    return f"{socket.gethostname()}-{os.getpid()}"


def process_message(
    session: Session,
    payload: dict[str, Any],
    *,
    run_id: str | None = None,
    resolver: Resolver | None = None,
    llm_call: LLMCall | None = None,
) -> int:
    """Handle one enrich payload against an open session. Returns the requirement count."""
    raw_id = payload.get("posting_id")
    try:
        posting_id = int(raw_id)
    except (TypeError, ValueError):
        raise PermanentFailure(f"enrich payload has no usable posting_id: {raw_id!r}") from None

    jd = session.get(PostingJD, posting_id)
    if jd is None or not (jd.raw_text or "").strip():
        raise PermanentFailure(f"posting {posting_id} has no job description text")

    posting = session.get(Posting, posting_id)

    rows = extract_requirements(
        session,
        posting_id,
        jd.raw_text or "",
        run_id=run_id,
        resolver=resolver,
        company=posting.company if posting else None,
        title=posting.title if posting else (jd.title or None),
        llm_call=llm_call,
    )
    return len(rows)


def make_handler(
    *,
    session_factory: SessionFactory = session_scope,
    run_id: str | None = None,
    resolver: Resolver | None = None,
    llm_call: LLMCall | None = None,
) -> Callable[[Message], None]:
    """Build the queue handler. Commits before returning; the caller acks after that."""

    def handle(message: Message) -> None:
        log.info(
            "enrich_message",
            id=message.id,
            attempt=message.attempt,
            posting_id=message.payload.get("posting_id"),
        )
        with session_factory() as session:
            count = process_message(
                session,
                message.payload,
                run_id=run_id,
                resolver=resolver,
                llm_call=llm_call,
            )
        log.info("enrich_done", id=message.id, requirements=count)

    return handle


def build_consumer(
    client: redis.Redis,
    consumer: str | None = None,
    **kwargs: Any,
) -> ConsumerGroup:
    return ConsumerGroup(
        client,
        STREAM_ENRICH,
        GROUP_ENRICH,
        consumer or default_consumer_name(),
        STREAM_ENRICH_DEAD,
        **kwargs,
    )


def run_worker(
    consumer: str | None = None,
    *,
    client: redis.Redis | None = None,
    session_factory: SessionFactory = session_scope,
    run_id: str | None = None,
    resolver: Resolver | None = None,
    llm_call: LLMCall | None = None,
) -> None:
    """Consume `jme:enrich` until SIGTERM/SIGINT."""
    redis_client = client or redis.Redis.from_url(get_settings().redis_url, decode_responses=True)
    group = build_consumer(redis_client, consumer)
    run = run_id or new_run_id("enrich")
    log.info("enricher_worker_start", consumer=group.consumer, run_id=run)
    group.run(
        make_handler(
            session_factory=session_factory, run_id=run, resolver=resolver, llm_call=llm_call
        )
    )
