from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from vitalmesh.events.v1 import envelope_pb2, payloads_pb2
from vitalmesh_common import codec
from vitalmesh_common.envelope import EventMeta
from vitalmesh_common.failure import EnvelopeError
from vitalmesh_common.idempotency import InMemoryIdempotencyStore
from vitalmesh_common.processor import (
    NOT_BEFORE_HEADER,
    EventProcessor,
    Outcome,
    Record,
)
from vitalmesh_common.retry import RetryPolicy
from vitalmesh_common.testing import PlainSerde, Published, RecordingPublisher
from vitalmesh_common.topics import USER_EVENTS, dlq_topic, retry_topic

NOW = datetime(2026, 10, 20, 8, 0, tzinfo=UTC)
GROUP = "state-processor"


@dataclass
class Handler:
    effects: list[str] = field(default_factory=list)
    failures: list[BaseException] = field(default_factory=list)  # raised in order, then succeed
    always: BaseException | None = None

    def __call__(self, meta: EventMeta, payload: Any) -> None:
        if self.always is not None:
            raise self.always
        if self.failures:
            raise self.failures.pop(0)
        self.effects.append(meta.event_id)


def weight() -> payloads_pb2.MeasurementRecorded:
    m = payloads_pb2.MeasurementRecorded(metric_type="body_weight")
    m.value.value, m.value.unit = 72.5, "kg"
    return m


def make_meta(**kw: object) -> EventMeta:
    base: dict[str, object] = {
        "correlation_id": "c",
        "user_id": "u-1",
        "household_id": "h-1",
        "type": "measurement.recorded",
        "occurred_at": NOW - timedelta(seconds=5),
    }
    base.update(kw)
    return EventMeta(**base)


def record(meta: EventMeta | None = None, topic: str = USER_EVENTS, offset: int = 0) -> Record:
    meta = meta or make_meta()
    return Record(topic, 0, offset, meta.user_id, codec.encode(meta, weight()))


def as_record(p: Published, offset: int = 0) -> Record:
    return Record(p.topic, 0, offset, p.key, p.value, p.headers)


def build(
    handler: Handler | None = None, policy: RetryPolicy | None = None
) -> tuple[EventProcessor, RecordingPublisher, InMemoryIdempotencyStore, Handler]:
    handler = handler or Handler()
    pub = RecordingPublisher()
    store = InMemoryIdempotencyStore()
    proc = EventProcessor(
        group=GROUP,
        serde=PlainSerde(),
        publisher=pub,
        store=store,
        handler=handler,
        policy=policy,
        clock=lambda: NOW,
    )
    return proc, pub, store, handler


def dlq_record(p: Published) -> envelope_pb2.DlqRecord:
    r = envelope_pb2.DlqRecord()
    r.ParseFromString(p.value)
    return r


# ---------------------------------------------------------------------- happy path
def test_processes_an_event_once() -> None:
    proc, pub, _, handler = build()
    rec = record()
    assert proc.process(rec) is Outcome.PROCESSED
    assert len(handler.effects) == 1
    assert pub.sent == []


def test_redelivery_of_the_same_event_is_skipped() -> None:
    proc, _, _, handler = build()
    rec = record()
    assert proc.process(rec) is Outcome.PROCESSED
    assert proc.process(rec) is Outcome.DUPLICATE_SKIPPED  # at-least-once delivery
    assert len(handler.effects) == 1


def test_provider_identity_dedupes_across_different_event_ids() -> None:
    proc, _, _, handler = build()
    a = make_meta(source="fitbit", external_id="m-1")
    b = make_meta(source="fitbit", external_id="m-1")  # new event_id, same provider record
    assert a.event_id != b.event_id
    assert proc.process(record(a)) is Outcome.PROCESSED
    assert proc.process(record(b)) is Outcome.DUPLICATE_SKIPPED
    assert len(handler.effects) == 1


# ---------------------------------------------------------------------------- retry
def test_retryable_failure_goes_to_tier_1_with_a_delay_and_attempt_count() -> None:
    proc, pub, _, _ = build(Handler(failures=[RuntimeError("boom")]))
    assert proc.process(record()) is Outcome.RETRIED
    (sent,) = pub.sent
    assert sent.topic == retry_topic(USER_EVENTS, 1)
    assert sent.key == "u-1"  # same key: same partition affinity
    assert int(sent.headers[NOT_BEFORE_HEADER]) == int(
        (NOW + timedelta(seconds=5)).timestamp() * 1000
    )
    meta, _ = codec.decode(sent.value)
    assert meta.attempt == 1


def test_a_failed_attempt_does_not_poison_idempotency() -> None:
    """The retry of a failed event must be processed, not dropped as a duplicate."""
    proc, pub, _, handler = build(Handler(failures=[RuntimeError("boom")]))
    assert proc.process(record()) is Outcome.RETRIED
    assert proc.process(as_record(pub.sent[0])) is Outcome.PROCESSED
    assert len(handler.effects) == 1  # the effect happened exactly once


def test_full_chain_main_to_retry_tiers_to_dlq() -> None:
    policy = RetryPolicy()
    handler = Handler(always=RuntimeError("poison"))
    proc, pub, _, _ = build(handler, policy)
    outcomes = [proc.process(record())]
    for i in range(policy.max_retries):
        outcomes.append(proc.process(as_record(pub.sent[-1], i + 1)))
    assert outcomes == [Outcome.RETRIED] * policy.max_retries + [Outcome.DEAD_LETTERED]
    assert [p.topic for p in pub.sent] == [
        *[retry_topic(USER_EVENTS, t) for t in range(1, policy.max_retries + 1)],
        dlq_topic(USER_EVENTS),
    ]
    last = dlq_record(pub.sent[-1])
    assert "exhausted" in last.route_reason
    assert last.source_topic == retry_topic(USER_EVENTS, policy.max_retries)


def test_non_retryable_failure_goes_straight_to_the_dlq() -> None:
    proc, pub, _, _ = build(Handler(always=EnvelopeError("bad data")))
    assert proc.process(record()) is Outcome.DEAD_LETTERED
    (sent,) = pub.sent
    assert sent.topic == dlq_topic(USER_EVENTS)
    d = dlq_record(sent)
    assert d.category == "invalid_input" and not d.retryable
    assert d.remediation_action


def test_undecodable_bytes_are_kept_verbatim_in_the_dlq_and_never_reach_the_handler() -> None:
    proc, pub, _, handler = build()
    garbage = b"\xff\xff not protobuf"
    rec = Record(USER_EVENTS, 3, 42, "u-1", garbage)
    assert proc.process(rec) is Outcome.DEAD_LETTERED
    d = dlq_record(pub.sent[0])
    assert d.original_value == garbage  # replayable after the producer is fixed
    assert (d.source_partition, d.source_offset) == (3, 42)
    assert handler.effects == []


def test_expired_events_are_never_handled_or_retried() -> None:
    proc, pub, store, handler = build()
    expired = make_meta(deadline_at=NOW - timedelta(seconds=1))
    assert proc.process(record(expired)) is Outcome.DEAD_LETTERED
    assert handler.effects == []
    assert "expired" in dlq_record(pub.sent[0]).route_reason
    # nothing was acquired, so a later valid delivery is unaffected
    assert store.acquire(GROUP, expired.idempotency_key()).value == "new"


def test_in_flight_conflict_is_retried_not_processed() -> None:
    proc, pub, store, handler = build()
    meta = make_meta()
    store.acquire(GROUP, meta.idempotency_key())  # another worker holds it
    assert proc.process(record(meta)) is Outcome.RETRIED
    assert handler.effects == []
    assert pub.sent[0].topic == retry_topic(USER_EVENTS, 1)


# ------------------------------------------------------------------- durability
def test_retry_and_dlq_are_flushed_before_returning() -> None:
    """The source offset commits after process() returns, so the route must be durable first."""
    proc, pub, _, _ = build(Handler(failures=[RuntimeError("x")]))
    proc.process(record())
    assert pub.log == ["publish", "flush"]

    proc2, pub2, _, _ = build(Handler(always=EnvelopeError("x")))
    proc2.process(record())
    assert pub2.log == ["publish", "flush"]


def test_a_failed_flush_propagates_so_the_offset_is_not_committed() -> None:
    proc, pub, _, _ = build(Handler(failures=[RuntimeError("x")]))
    pub.fail_flush = True
    with pytest.raises(RuntimeError, match="broker unavailable"):
        proc.process(record())  # never swallowed: the record will be redelivered


def test_base_exceptions_are_not_swallowed() -> None:
    proc, _, _, _ = build(Handler(always=KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        proc.process(record())


def test_not_before_header_parsing_is_tolerant() -> None:
    assert Record("t", 0, 0, "k", b"", {NOT_BEFORE_HEADER: "abc"}).not_before() is None
    assert Record("t", 0, 0, "k", b"", {}).not_before() is None
    ok = Record("t", 0, 0, "k", b"", {NOT_BEFORE_HEADER: "1000"}).not_before()
    assert ok == datetime.fromtimestamp(1, tz=UTC)
