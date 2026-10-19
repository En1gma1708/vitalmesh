"""Retry / DLQ routing as a pure function (docs/EVENT_BACKBONE.md sections 6 and 11).

Retry topics only ever receive ``retryable=True`` failures; everything else, and
anything that exhausted its attempts, goes straight to the DLQ with its remediation hint.
An expired event is never retried (fail closed).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from vitalmesh_common.envelope import EventMeta, utcnow
from vitalmesh_common.failure import Failure
from vitalmesh_common.topics import base_topic, dlq_topic, retry_topic


@dataclass(frozen=True)
class RetryPolicy:
    """``backoff`` has one delay per allowed retry; its length is the retry budget."""

    backoff: tuple[timedelta, ...] = (
        timedelta(seconds=5),
        timedelta(seconds=30),
        timedelta(minutes=2),
    )

    @property
    def max_retries(self) -> int:
        return len(self.backoff)


@dataclass(frozen=True)
class RetryRoute:
    topic: str
    not_before: datetime
    next_attempt: int


@dataclass(frozen=True)
class DlqRoute:
    topic: str
    reason: str


Route = RetryRoute | DlqRoute


def route_failure(
    topic: str,
    meta: EventMeta,
    failure: Failure,
    policy: RetryPolicy | None = None,
    now: datetime | None = None,
) -> Route:
    """Decide where a failed event goes next."""
    policy = policy or RetryPolicy()
    now = now or utcnow()
    origin = base_topic(topic)

    if meta.is_expired(now):
        return DlqRoute(dlq_topic(origin), "deadline expired; not retried")
    if not failure.retryable:
        return DlqRoute(dlq_topic(origin), f"non-retryable: {failure.category.value}")
    if meta.attempt >= policy.max_retries:
        return DlqRoute(dlq_topic(origin), f"retry budget exhausted after {meta.attempt} retries")
    delay = policy.backoff[meta.attempt]
    return RetryRoute(
        topic=retry_topic(origin), not_before=now + delay, next_attempt=meta.attempt + 1
    )
