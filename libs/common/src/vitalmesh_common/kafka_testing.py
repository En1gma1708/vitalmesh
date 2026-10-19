"""Helpers for integration tests against the real Kafka stack (not used by production code)."""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Any

import redis
from confluent_kafka import Consumer

from vitalmesh_common.envelope import EventMeta
from vitalmesh_common.idempotency import RedisIdempotencyStore
from vitalmesh_common.kafka import (
    KafkaPublisher,
    KafkaSettings,
    SchemaRegistrySerde,
    build_consumer,
    run_consumer,
)
from vitalmesh_common.processor import EventProcessor, Handler
from vitalmesh_common.retry import RetryPolicy
from vitalmesh_common.topics import retry_topic

FAST = RetryPolicy(backoff=(timedelta(seconds=1), timedelta(seconds=1), timedelta(seconds=1)))


def wait_for(predicate: Callable[[], bool], timeout: float = 45.0, interval: float = 0.25) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def only_for(uid: str, inner: Handler) -> Handler:
    """Shared dev topics keep history, so ignore events that belong to other test runs."""

    def handler(meta: EventMeta, payload: Any) -> None:
        if meta.user_id == uid:
            inner(meta, payload)

    return handler


@contextmanager
def running_consumers(
    settings: KafkaSettings,
    serde: SchemaRegistrySerde,
    publisher: KafkaPublisher,
    client: redis.Redis,
    input_topic: str,
    handler: Handler,
    policy: RetryPolicy = FAST,
) -> Iterator[None]:
    """A service's consumer topology: the input topic plus one consumer per retry tier."""
    name = f"it-{uuid.uuid4()}"
    processor = EventProcessor(
        group=name,
        serde=serde,
        publisher=publisher,
        store=RedisIdempotencyStore(client, prefix=name),
        handler=handler,
        policy=policy,
    )
    stop = threading.Event()
    topics = [input_topic, *[retry_topic(input_topic, t) for t in range(1, policy.max_retries + 1)]]
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


def fetch_through(
    settings: KafkaSettings,
    topic: str,
    decode: Callable[[bytes], Any],
    stop_when: Callable[[Any], bool],
    timeout: float = 45.0,
) -> list[Any]:
    """Read a topic from the start with a throwaway group until an item satisfies ``stop_when``."""
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
