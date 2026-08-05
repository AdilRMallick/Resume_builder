"""Redis Streams producer and consumer-group harness (Python side).

The Go fetcher has a mirror of this in `fetcher/internal/queue`. Both must agree on:
  * entry shape: a single field `payload` holding a JSON object
  * attempt tracking: a Redis hash `<stream>:attempts` keyed by message id
  * dead letter shape: the original payload plus `reason`, `attempts`, `failed_at`

Delivery semantics, deliberately chosen:
  * XREADGROUP with a blocking read gives at-least-once delivery
  * XACK happens only after the handler returns and its database work has committed
  * XAUTOCLAIM sweeps messages pending longer than the visibility timeout, so a worker
    that is SIGKILLed mid-job does not strand its message
  * after max_attempts the message goes to a dead letter stream and is XACKed, so it
    stops being redelivered but is never silently dropped
"""

from __future__ import annotations

import datetime as dt
import json
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import redis

from jme.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class Message:
    id: str
    payload: dict[str, Any]
    attempt: int


class PermanentFailure(Exception):
    """Raised by a handler to send a message straight to the dead letter stream."""


def connect(url: str) -> redis.Redis:
    return redis.Redis.from_url(url, decode_responses=True)


class Producer:
    def __init__(self, client: redis.Redis, stream: str, maxlen: int | None = 100_000) -> None:
        self.client = client
        self.stream = stream
        self.maxlen = maxlen

    def publish(self, payload: dict[str, Any]) -> str:
        return self.client.xadd(
            self.stream,
            {"payload": json.dumps(payload, separators=(",", ":"))},
            maxlen=self.maxlen,
            approximate=True,
        )

    def publish_many(self, payloads: list[dict[str, Any]]) -> list[str]:
        pipe = self.client.pipeline(transaction=False)
        for payload in payloads:
            pipe.xadd(
                self.stream,
                {"payload": json.dumps(payload, separators=(",", ":"))},
                maxlen=self.maxlen,
                approximate=True,
            )
        return pipe.execute()


class ConsumerGroup:
    def __init__(
        self,
        client: redis.Redis,
        stream: str,
        group: str,
        consumer: str,
        dead_letter_stream: str,
        *,
        block_ms: int = 5000,
        batch_size: int = 8,
        visibility_timeout_ms: int = 120_000,
        max_attempts: int = 3,
        claim_interval_sec: float = 30.0,
    ) -> None:
        self.client = client
        self.stream = stream
        self.group = group
        self.consumer = consumer
        self.dead_letter_stream = dead_letter_stream
        self.block_ms = block_ms
        self.batch_size = batch_size
        self.visibility_timeout_ms = visibility_timeout_ms
        self.max_attempts = max_attempts
        self.claim_interval_sec = claim_interval_sec

        self._attempts_key = f"{stream}:attempts"
        self._stopping = False
        self._last_claim = 0.0

    # -- lifecycle ---------------------------------------------------------------

    def ensure_group(self) -> None:
        try:
            self.client.xgroup_create(self.stream, self.group, id="0", mkstream=True)
            log.info("consumer_group_created", stream=self.stream, group=self.group)
        except redis.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    def install_signal_handlers(self) -> None:
        def _handle(signum: int, _frame: object) -> None:
            log.info("shutdown_signal", signal=signum, consumer=self.consumer)
            self._stopping = True

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _handle)
            except (ValueError, OSError):
                # not on the main thread; caller is responsible for calling stop()
                pass

    def stop(self) -> None:
        self._stopping = True

    # -- reading -----------------------------------------------------------------

    def _attempt_of(self, message_id: str) -> int:
        raw = self.client.hget(self._attempts_key, message_id)
        return int(raw) if raw else 0

    def _bump_attempt(self, message_id: str) -> int:
        return int(self.client.hincrby(self._attempts_key, message_id, 1))

    def _decode(self, message_id: str, values: dict[str, str]) -> Message | None:
        raw = values.get("payload")
        if raw is None:
            log.warning("entry_missing_payload", id=message_id, stream=self.stream)
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("entry_bad_json", id=message_id, stream=self.stream)
            return None
        return Message(id=message_id, payload=payload, attempt=self._attempt_of(message_id))

    def read(self) -> list[Message]:
        """One blocking read of new messages, plus a periodic reclaim sweep."""
        messages: list[Message] = []

        now = time.monotonic()
        if now - self._last_claim >= self.claim_interval_sec:
            self._last_claim = now
            messages.extend(self.autoclaim())

        if messages:
            return messages

        response = self.client.xreadgroup(
            groupname=self.group,
            consumername=self.consumer,
            streams={self.stream: ">"},
            count=self.batch_size,
            block=self.block_ms,
        )
        for _stream, entries in response or []:
            for message_id, values in entries:
                decoded = self._decode(message_id, values)
                if decoded is None:
                    self.dead_letter(message_id, {"raw": values}, "undecodable", 0)
                    continue
                messages.append(decoded)
        return messages

    def autoclaim(self) -> list[Message]:
        """Reclaim messages pending longer than the visibility timeout.

        XAUTOCLAIM is the reason a SIGKILLed worker does not strand work: its messages
        stay in the PEL with an idle time that keeps growing, and the next sweep by any
        live consumer takes ownership of them.
        """
        claimed: list[Message] = []
        cursor = "0-0"
        seen_cursors: set[str] = set()
        while True:
            result = self.client.xautoclaim(
                name=self.stream,
                groupname=self.group,
                consumername=self.consumer,
                min_idle_time=self.visibility_timeout_ms,
                start_id=cursor,
                count=self.batch_size,
            )
            # redis-py returns (next_cursor, entries) or (next_cursor, entries, deleted)
            next_cursor, entries = result[0], result[1]

            for message_id, values in entries:
                decoded = self._decode(message_id, values)
                if decoded is None:
                    self.dead_letter(message_id, {"raw": values}, "undecodable", 0)
                    continue
                log.info(
                    "message_reclaimed",
                    id=message_id,
                    stream=self.stream,
                    consumer=self.consumer,
                    attempt=decoded.attempt,
                )
                claimed.append(decoded)

            # Stop when the PEL has been walked. Real Redis signals that with a
            # "0-0" cursor, having returned the id *after* the last one scanned.
            # Some fakes return the last id scanned instead, so the cursor never
            # advances and a naive loop would reclaim the same entry forever;
            # refusing to revisit a cursor bounds the sweep under either
            # behaviour, and an empty batch ends it in the normal case.
            if next_cursor == "0-0" or not entries or next_cursor in seen_cursors:
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        return claimed

    # -- completion --------------------------------------------------------------

    def ack(self, message_id: str) -> None:
        pipe = self.client.pipeline()
        pipe.xack(self.stream, self.group, message_id)
        pipe.hdel(self._attempts_key, message_id)
        pipe.execute()

    def dead_letter(
        self, message_id: str, payload: dict[str, Any], reason: str, attempts: int
    ) -> None:
        entry = {
            "payload": json.dumps(payload, separators=(",", ":")),
            "reason": reason,
            "attempts": str(attempts),
            "source_stream": self.stream,
            "failed_at": dt.datetime.now(dt.UTC).isoformat(),
        }
        pipe = self.client.pipeline()
        pipe.xadd(self.dead_letter_stream, entry)
        pipe.xack(self.stream, self.group, message_id)
        pipe.hdel(self._attempts_key, message_id)
        pipe.execute()
        log.warning(
            "message_dead_lettered", id=message_id, reason=reason, attempts=attempts,
            stream=self.stream,
        )

    # -- the loop ----------------------------------------------------------------

    def run(self, handler: Callable[[Message], None]) -> None:
        """Consume until stopped. The handler must be idempotent.

        A handler that returns cleanly gets an XACK. A handler that raises
        PermanentFailure is dead lettered immediately. Any other exception is retried
        until max_attempts, then dead lettered.
        """
        self.ensure_group()
        self.install_signal_handlers()
        log.info(
            "consumer_started",
            stream=self.stream,
            group=self.group,
            consumer=self.consumer,
            max_attempts=self.max_attempts,
        )

        while not self._stopping:
            try:
                messages = self.read()
            except redis.RedisError as exc:
                log.error("read_failed", error=str(exc))
                time.sleep(1.0)
                continue

            for message in messages:
                # in-flight work is finished even while shutting down; only new reads stop
                attempt = self._bump_attempt(message.id)
                try:
                    handler(Message(message.id, message.payload, attempt))
                except PermanentFailure as exc:
                    self.dead_letter(message.id, message.payload, str(exc), attempt)
                except Exception as exc:  # noqa: BLE001 - the loop must not die
                    log.error(
                        "handler_failed",
                        id=message.id,
                        attempt=attempt,
                        max_attempts=self.max_attempts,
                        error=str(exc),
                    )
                    if attempt >= self.max_attempts:
                        self.dead_letter(message.id, message.payload, str(exc), attempt)
                    # else: leave it pending, autoclaim will redeliver it
                else:
                    self.ack(message.id)

        log.info("consumer_stopped", consumer=self.consumer)

    # -- observability -----------------------------------------------------------

    def depth(self) -> int:
        return int(self.client.xlen(self.stream))

    def pending_summary(self) -> dict[str, Any]:
        summary = self.client.xpending(self.stream, self.group)
        return dict(summary) if summary else {}
