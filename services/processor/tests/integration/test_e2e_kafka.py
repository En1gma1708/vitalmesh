"""End-to-end: produce -> Kafka -> processor -> state + user.state, against the real stack.

Each test uses a unique user id and consumer groups, and filters on it, because the dev
topics keep history across runs.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import redis
from confluent_kafka import Consumer
from confluent_kafka.schema_registry import SchemaRegistryClient
from confluent_kafka.schema_registry.protobuf import ProtobufDeserializer
from confluent_kafka.serialization import MessageField, SerializationContext
from vitalmesh.events.v1 import envelope_pb2, payloads_pb2
from vitalmesh_common.envelope import EventMeta
from vitalmesh_common.idempotency import RedisIdempotencyStore
from vitalmesh_common.kafka import (
    KafkaPublisher,
    KafkaSettings,
    SchemaRegistrySerde,
    build_consumer,
    ensure_topics,
    run_consumer,
)
from vitalmesh_common.processor import EventProcessor, Handler
from vitalmesh_common.retry import RetryPolicy
from vitalmesh_common.topics import USER_EVENTS, USER_STATE, dlq_topic, retry_topic
from vitalmesh_processor.handler import derived_event_id, make_handler
from vitalmesh_processor.state import RedisStateStore

pytestmark = pytest.mark.integration

T0 = datetime(2026, 10, 20, 8, 0, tzinfo=UTC)
FAST = RetryPolicy(backoff=(timedelta(seconds=1), timedelta(seconds=1), timedelta(seconds=1)))


# ------------------------------------------------------------------------- helpers
def wait_for(predicate: Callable[[], bool], timeout: float = 45.0, interval: float = 0.25) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def measurement(
    uid: str, kg: float, minutes: int = 0, metric: str = "body_weight"
) -> tuple[EventMeta, payloads_pb2.MeasurementRecorded]:
    meta = EventMeta(
        correlation_id=f"corr-{uuid.uuid4()}",
        user_id=uid,
        household_id="h-e2e",
        type="measurement.recorded",
        occurred_at=T0,
        trace_id="trace-e2e",
    )
    m = payloads_pb2.MeasurementRecorded(metric_type=metric)
    m.value.value, m.value.unit = kg, "kg"
    m.measured_at.FromDatetime(T0 + timedelta(minutes=minutes))
    return meta, m


def only_for(uid: str, inner: Handler, seen: Counter[str] | None = None) -> Handler:
    """The dev topics keep history, so ignore events that belong to other test runs."""

    def handler(meta: EventMeta, payload: Any) -> None:
        if meta.user_id != uid:
            return
        if seen is not None:
            seen[meta.event_id] += 1
        inner(meta, payload)

    return handler


@pytest.fixture(scope="module")
def serde(kafka_settings: KafkaSettings) -> SchemaRegistrySerde:
    ensure_topics(kafka_settings)
    return SchemaRegistrySerde(kafka_settings)


@pytest.fixture()
def publisher(kafka_settings: KafkaSettings) -> KafkaPublisher:
    return KafkaPublisher(kafka_settings, client_id="e2e")


@contextmanager
def running_service(
    settings: KafkaSettings,
    serde: SchemaRegistrySerde,
    publisher: KafkaPublisher,
    client: redis.Redis,
    handler: Handler,
    policy: RetryPolicy = FAST,
) -> Iterator[None]:
    """The processor's consumer topology: the main topic plus one consumer per retry tier."""
    name = f"e2e-{uuid.uuid4()}"
    processor = EventProcessor(
        group=name,
        serde=serde,
        publisher=publisher,
        store=RedisIdempotencyStore(client, prefix=name),
        handler=handler,
        policy=policy,
    )
    stop = threading.Event()
    topics = [USER_EVENTS, *[retry_topic(USER_EVENTS, t) for t in range(1, policy.max_retries + 1)]]
    threads = [
        threading.Thread(
            target=run_consumer,
            args=(build_consumer(settings, f"{name}.{t}"), [t], processor, stop),
            daemon=True,
        )
        for t in topics
    ]
    for t in threads:
        t.start()
    try:
        yield
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=20)


def produce(
    publisher: KafkaPublisher,
    serde: SchemaRegistrySerde,
    meta: EventMeta,
    payload: Any,
    topic: str = USER_EVENTS,
) -> None:
    publisher.publish(topic, meta.user_id, serde.serialize_event(topic, meta, payload))
    publisher.flush()


def fetch_through(
    settings: KafkaSettings,
    topic: str,
    decode: Callable[[bytes], Any],
    stop_when: Callable[[Any], bool],
    timeout: float = 45.0,
) -> list[Any]:
    """Read a topic from the start with a throwaway group until an item satisfies ``stop_when``.

    ``stop_when`` sees one decoded item at a time, so the whole read is linear in the topic
    size. Returns every item read, including the one that stopped the read.
    """
    consumer = Consumer(
        {
            "bootstrap.servers": settings.bootstrap_servers,
            "group.id": f"probe-{uuid.uuid4()}",
            "auto.offset.reset": "earliest",
            "enable.auto.commit": False,
        }
    )
    consumer.subscribe([topic])
    got: list[Any] = []
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            msg = consumer.poll(0.5)
            if msg is None or msg.error():
                continue
            try:
                item = decode(msg.value())
            except Exception:  # not every record on a shared dev topic is ours
                continue
            got.append(item)
            if stop_when(item):
                break
    finally:
        consumer.close()
    return got


def event_decoder(
    serde: SchemaRegistrySerde, topic: str
) -> Callable[[bytes], tuple[EventMeta, Any]]:
    return lambda data: serde.deserialize_event(topic, data)


def dlq_decoder(settings: KafkaSettings, topic: str) -> Callable[[bytes], envelope_pb2.DlqRecord]:
    de = ProtobufDeserializer(
        envelope_pb2.DlqRecord, {}, SchemaRegistryClient({"url": settings.schema_registry_url})
    )
    return lambda data: de(data, SerializationContext(topic, MessageField.VALUE))


# ------------------------------------------------------------------------- tests
def test_measurement_flows_end_to_end(
    kafka_settings: KafkaSettings,
    serde: SchemaRegistrySerde,
    publisher: KafkaPublisher,
    redis_client: redis.Redis,
) -> None:
    uid = f"u-{uuid.uuid4()}"
    meta, payload = measurement(uid, 72.5)
    state = RedisStateStore(redis_client, prefix=f"e2e-{uuid.uuid4()}")
    handler = only_for(uid, make_handler(state, serde, publisher))

    with running_service(kafka_settings, serde, publisher, redis_client, handler):
        produce(publisher, serde, meta, payload)
        assert wait_for(lambda: state.get(uid).version == 1), "state was never updated"
        out = fetch_through(
            kafka_settings,
            USER_STATE,
            event_decoder(serde, USER_STATE),
            lambda item: item[0].causation_id == meta.event_id,
        )

    (out_meta, out_payload) = next(x for x in out if x[0].causation_id == meta.event_id)
    assert out_meta.type == "health.state.updated"
    assert out_meta.event_id == derived_event_id(meta.event_id)
    assert out_meta.correlation_id == meta.correlation_id  # the chain is traceable end to end
    assert out_meta.trace_id == "trace-e2e"
    assert out_payload.latest_metrics["body_weight"].value == 72.5
    assert out_payload.latest_metrics["body_weight"].unit == "kg"


def test_duplicate_delivery_changes_state_once_and_emits_one_event(
    kafka_settings: KafkaSettings,
    serde: SchemaRegistrySerde,
    publisher: KafkaPublisher,
    redis_client: redis.Redis,
) -> None:
    uid = f"u-{uuid.uuid4()}"
    dup_meta, dup_payload = measurement(uid, 72.5, 0)
    sentinel_meta, sentinel_payload = measurement(uid, 58.0, 5, metric="resting_heart_rate")
    state = RedisStateStore(redis_client, prefix=f"e2e-{uuid.uuid4()}")
    seen: Counter[str] = Counter()
    handler = only_for(uid, make_handler(state, serde, publisher), seen)

    with running_service(kafka_settings, serde, publisher, redis_client, handler):
        # At-least-once delivery: the same event arrives twice, then a sentinel. Per-user
        # ordering means the sentinel's output proves both duplicates were already consumed.
        produce(publisher, serde, dup_meta, dup_payload)
        produce(publisher, serde, dup_meta, dup_payload)
        produce(publisher, serde, sentinel_meta, sentinel_payload)
        assert wait_for(lambda: state.get(uid).version == 2), "sentinel was never processed"
        out = fetch_through(
            kafka_settings,
            USER_STATE,
            event_decoder(serde, USER_STATE),
            lambda item: item[0].causation_id == sentinel_meta.event_id,
        )

    assert seen[dup_meta.event_id] == 1  # the handler ran once for the duplicated event
    emitted = [m for m, _ in out if m.causation_id == dup_meta.event_id]
    assert len(emitted) == 1  # and downstream saw one state event, not two
    assert state.get(uid).version == 2


def test_poison_message_goes_to_the_dlq_with_its_original_bytes(
    kafka_settings: KafkaSettings,
    serde: SchemaRegistrySerde,
    publisher: KafkaPublisher,
    redis_client: redis.Redis,
) -> None:
    uid = f"u-{uuid.uuid4()}"
    garbage = f"not a protobuf envelope {uuid.uuid4()}".encode()
    seen: Counter[str] = Counter()
    state = RedisStateStore(redis_client, prefix=f"e2e-{uuid.uuid4()}")
    handler = only_for(uid, make_handler(state, serde, publisher), seen)
    topic = dlq_topic(USER_EVENTS)

    with running_service(kafka_settings, serde, publisher, redis_client, handler):
        publisher.publish(USER_EVENTS, uid, garbage)
        publisher.flush()
        records = fetch_through(
            kafka_settings,
            topic,
            dlq_decoder(kafka_settings, topic),
            lambda r: r.original_value == garbage,
        )

    record = next(r for r in records if r.original_value == garbage)
    assert record.category == "invalid_input" and not record.retryable
    assert record.source_topic == USER_EVENTS
    assert record.remediation_action
    assert not seen  # it never reached the handler


def test_transient_failure_is_retried_and_then_succeeds_exactly_once(
    kafka_settings: KafkaSettings,
    serde: SchemaRegistrySerde,
    publisher: KafkaPublisher,
    redis_client: redis.Redis,
) -> None:
    uid = f"u-{uuid.uuid4()}"
    meta, payload = measurement(uid, 72.5)
    effects: list[str] = []
    attempts: Counter[str] = Counter()

    def flaky(m: EventMeta, p: Any) -> None:
        attempts[m.event_id] += 1
        if attempts[m.event_id] == 1:
            raise ConnectionError("downstream unavailable")  # retryable
        effects.append(m.event_id)

    started = time.monotonic()
    with running_service(kafka_settings, serde, publisher, redis_client, only_for(uid, flaky)):
        produce(publisher, serde, meta, payload)
        assert wait_for(lambda: effects == [meta.event_id]), "the retry never succeeded"
        retried = fetch_through(
            kafka_settings,
            retry_topic(USER_EVENTS, 1),
            event_decoder(serde, retry_topic(USER_EVENTS, 1)),
            lambda item: item[0].event_id == meta.event_id,
        )

    assert time.monotonic() - started >= 1.0  # the backoff delay was honoured
    assert attempts[meta.event_id] == 2 and effects == [meta.event_id]
    (retry_meta, _) = next(x for x in retried if x[0].event_id == meta.event_id)
    assert retry_meta.attempt == 1  # carried on the event, not in a side table


def test_a_persistently_failing_event_exhausts_its_retries_into_the_dlq(
    kafka_settings: KafkaSettings,
    serde: SchemaRegistrySerde,
    publisher: KafkaPublisher,
    redis_client: redis.Redis,
) -> None:
    uid = f"u-{uuid.uuid4()}"
    meta, payload = measurement(uid, 72.5)
    attempts: Counter[str] = Counter()

    def always_fails(m: EventMeta, p: Any) -> None:
        attempts[m.event_id] += 1
        raise RuntimeError("poison")

    topic = dlq_topic(USER_EVENTS)
    with running_service(
        kafka_settings, serde, publisher, redis_client, only_for(uid, always_fails)
    ):
        produce(publisher, serde, meta, payload)
        records = fetch_through(
            kafka_settings,
            topic,
            dlq_decoder(kafka_settings, topic),
            lambda r: _is_ours(r, serde, meta),
            timeout=60,
        )

    record = next(r for r in records if _is_ours(r, serde, meta))
    assert attempts[meta.event_id] == 1 + FAST.max_retries  # first try + every retry tier
    assert "exhausted" in record.route_reason
    assert record.source_topic == retry_topic(USER_EVENTS, FAST.max_retries)
    original, _ = serde.deserialize_event(record.source_topic, record.original_value)
    assert original.event_id == meta.event_id and original.attempt == FAST.max_retries


def _is_ours(record: envelope_pb2.DlqRecord, serde: SchemaRegistrySerde, meta: EventMeta) -> bool:
    try:
        original, _ = serde.deserialize_event(record.source_topic, record.original_value)
    except Exception:
        return False
    return original.event_id == meta.event_id


def test_service_entrypoint_processes_events(
    kafka_settings: KafkaSettings,
    serde: SchemaRegistrySerde,
    publisher: KafkaPublisher,
    redis_client: redis.Redis,
) -> None:
    """Start the real service (``python -m vitalmesh_processor``) and push an event through it.

    The tests above build the consumer topology by hand; this one covers main.py itself:
    environment defaults, topic creation, one consumer thread per input topic, Redis wiring.
    """
    import os
    import subprocess
    import sys

    uid = f"u-{uuid.uuid4()}"
    meta, payload = measurement(uid, 70.0)
    state = RedisStateStore(redis_client)  # the service's default key prefix
    proc = subprocess.Popen(
        [sys.executable, "-m", "vitalmesh_processor"],
        env={
            **os.environ,
            "VITALMESH_KAFKA_BOOTSTRAP": kafka_settings.bootstrap_servers,
            # a unique group: no dead members from earlier runs, and a clean idempotency namespace
            "VITALMESH_CONSUMER_GROUP": f"entrypoint-{uuid.uuid4()}",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        produce(publisher, serde, meta, payload)
        ok = wait_for(lambda: state.get(uid).version == 1, timeout=60)
        assert ok, f"service did not process the event; exit code {proc.poll()}"
        assert proc.poll() is None  # still running
        assert state.get(uid).metrics["body_weight"].value == 70.0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
        redis_client.delete(f"state:{uid}")
