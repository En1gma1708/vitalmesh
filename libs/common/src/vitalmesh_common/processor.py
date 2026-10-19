"""At-least-once event processing: idempotency, retry tiers, DLQ, expiry.

``EventProcessor`` contains the delivery semantics and depends only on small protocols
(serde, publisher, idempotency store), so every branch is unit-tested with fakes; the real
Kafka adapters live in ``kafka.py``. Offsets are committed by the caller only after
``process`` returns, so a crash anywhere re-delivers the record (docs/EVENT_BACKBONE.md s6).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from vitalmesh.events.v1 import envelope_pb2

from vitalmesh_common.envelope import EventMeta, utcnow
from vitalmesh_common.failure import (
    EnvelopeError,
    Failure,
    FailureCategory,
    Remediation,
    failure_from_exception,
)
from vitalmesh_common.idempotency import Acquired, IdempotencyStore
from vitalmesh_common.retry import DlqRoute, RetryPolicy, RetryRoute, route_failure
from vitalmesh_common.topics import base_topic, dlq_topic

NOT_BEFORE_HEADER = "x-not-before-ms"

Handler = Callable[[EventMeta, Any], None]


class EventSerde(Protocol):
    def serialize_event(self, topic: str, meta: EventMeta, payload: Any) -> bytes: ...

    def deserialize_event(self, topic: str, data: bytes) -> tuple[EventMeta, Any]: ...

    def serialize_dlq(self, topic: str, record: envelope_pb2.DlqRecord) -> bytes: ...


class Publisher(Protocol):
    def publish(
        self, topic: str, key: str, value: bytes, headers: dict[str, str] | None = None
    ) -> None: ...

    def flush(self) -> None:
        """Block until every published message is acknowledged; raise if any failed."""
        ...


@dataclass(frozen=True)
class Record:
    topic: str
    partition: int
    offset: int
    key: str | None
    value: bytes
    headers: dict[str, str] = field(default_factory=dict)

    def not_before(self) -> datetime | None:
        raw = self.headers.get(NOT_BEFORE_HEADER)
        if raw is None:
            return None
        try:
            return datetime.fromtimestamp(int(raw) / 1000, tz=UTC)
        except ValueError:
            return None


class Outcome(StrEnum):
    PROCESSED = "processed"
    DUPLICATE_SKIPPED = "duplicate_skipped"
    RETRIED = "retried"
    DEAD_LETTERED = "dead_lettered"


IN_FLIGHT_FAILURE = Failure(
    code="idempotency.in_flight",
    category=FailureCategory.UNAVAILABLE,
    message="the same event is being processed by another worker",
    retryable=True,
    remediation=Remediation(action="Retry after the in-flight attempt finishes or expires."),
)


class EventProcessor:
    def __init__(
        self,
        *,
        group: str,
        serde: EventSerde,
        publisher: Publisher,
        store: IdempotencyStore,
        handler: Handler,
        policy: RetryPolicy | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._group = group
        self._serde = serde
        self._publisher = publisher
        self._store = store
        self._handler = handler
        self._policy = policy or RetryPolicy()
        self._clock = clock

    def process(self, rec: Record) -> Outcome:
        try:
            meta, payload = self._serde.deserialize_event(rec.topic, rec.value)
        except EnvelopeError as exc:
            # Undecodable: nothing about it can ever succeed on retry, keep the bytes for replay.
            self._dead_letter(rec, failure_from_exception(exc), "undecodable envelope")
            return Outcome.DEAD_LETTERED

        try:
            meta.ensure_not_expired(self._clock())
        except TimeoutError as exc:
            return self._fail(rec, meta, payload, failure_from_exception(exc))

        key = meta.idempotency_key()
        state = self._store.acquire(self._group, key)
        if state is Acquired.DONE:
            return Outcome.DUPLICATE_SKIPPED
        if state is Acquired.IN_FLIGHT:
            return self._fail(rec, meta, payload, IN_FLIGHT_FAILURE)

        try:
            self._handler(meta, payload)
        except Exception as exc:  # the contract classifies it; BaseException still propagates
            self._store.release(self._group, key)  # a retry must be processed, not dropped
            return self._fail(rec, meta, payload, failure_from_exception(exc))
        self._store.commit(self._group, key)
        return Outcome.PROCESSED

    # ------------------------------------------------------------------ routing
    def _fail(self, rec: Record, meta: EventMeta, payload: Any, failure: Failure) -> Outcome:
        route = route_failure(rec.topic, meta, failure, self._policy, self._clock())
        if isinstance(route, RetryRoute):
            value = self._serde.serialize_event(route.topic, meta.next_attempt(), payload)
            headers = {NOT_BEFORE_HEADER: str(int(route.not_before.timestamp() * 1000))}
            self._publisher.publish(route.topic, meta.user_id, value, headers)
            self._publisher.flush()  # the retry must be durable before the source offset commits
            return Outcome.RETRIED
        assert isinstance(route, DlqRoute)
        self._dead_letter(rec, failure, route.reason)
        return Outcome.DEAD_LETTERED

    def _dead_letter(self, rec: Record, failure: Failure, reason: str) -> None:
        topic = dlq_topic(base_topic(rec.topic))
        record = envelope_pb2.DlqRecord(
            code=failure.code,
            category=failure.category.value,
            message=failure.message,
            retryable=failure.retryable,
            remediation_action=failure.remediation.action,
            source_topic=rec.topic,
            source_partition=rec.partition,
            source_offset=rec.offset,
            route_reason=reason,
            original_value=rec.value,
        )
        record.details.update(failure.details)
        record.failed_at.FromDatetime(self._clock().astimezone(UTC))
        self._publisher.publish(topic, rec.key or "", self._serde.serialize_dlq(topic, record))
        self._publisher.flush()
