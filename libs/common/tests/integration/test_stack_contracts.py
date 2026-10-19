"""Contracts that only a real broker, registry and Redis can prove."""

from __future__ import annotations

import time
import uuid

import pytest
import redis
from confluent_kafka.admin import AdminClient
from confluent_kafka.schema_registry import SchemaRegistryClient
from vitalmesh_common.idempotency import Acquired, RedisIdempotencyStore
from vitalmesh_common.kafka import KafkaSettings, ensure_topics
from vitalmesh_common.retry import RetryPolicy
from vitalmesh_common.topics import all_topic_specs

pytestmark = pytest.mark.integration


def test_redis_idempotency_three_state_protocol(redis_client: redis.Redis) -> None:
    store = RedisIdempotencyStore(redis_client, prefix=f"test-{uuid.uuid4()}")
    assert store.acquire("g", "k") is Acquired.NEW
    assert store.acquire("g", "k") is Acquired.IN_FLIGHT
    store.release("g", "k")  # a failed handler must let the retry through
    assert store.acquire("g", "k") is Acquired.NEW
    store.commit("g", "k")
    assert store.acquire("g", "k") is Acquired.DONE
    assert store.acquire("other-group", "k") is Acquired.NEW  # namespaces are isolated


def test_redis_in_flight_lock_expires(redis_client: redis.Redis) -> None:
    """A crashed worker must not lock a key forever (checked against the real TTL)."""
    prefix = f"test-{uuid.uuid4()}"
    store = RedisIdempotencyStore(redis_client, prefix=prefix)
    store.acquire("g", "k")
    ttl = redis_client.ttl(f"{prefix}:g:k")
    assert 0 < ttl <= 60


def test_redis_is_configured_to_never_evict(redis_client: redis.Redis) -> None:
    """Idempotency and state keys silently evicted would let duplicates through."""
    assert redis_client.config_get("maxmemory-policy")["maxmemory-policy"] == "noeviction"


def test_ensure_topics_creates_the_whole_contract_and_is_idempotent(
    kafka_settings: KafkaSettings,
) -> None:
    ensure_topics(kafka_settings)
    assert ensure_topics(kafka_settings) == []  # second call has nothing to create
    admin = AdminClient({"bootstrap.servers": kafka_settings.bootstrap_servers})
    specs = all_topic_specs(RetryPolicy().max_retries)
    wanted = {s.name for s in specs}
    deadline = time.monotonic() + 30  # metadata is eventually consistent after creation
    existing = admin.list_topics(timeout=15).topics
    while not wanted <= set(existing) and time.monotonic() < deadline:
        time.sleep(0.5)
        existing = admin.list_topics(timeout=15).topics
    for spec in specs:
        assert spec.name in existing, f"missing topic {spec.name}"
        assert len(existing[spec.name].partitions) == spec.partitions_for(kafka_settings.profile)


def test_auto_topic_creation_is_off(kafka_settings: KafkaSettings) -> None:
    """A typo in a topic name must fail loudly, not silently create a new topic."""
    from confluent_kafka import Producer

    name = f"typo.{uuid.uuid4()}"
    p = Producer(
        {
            "bootstrap.servers": kafka_settings.bootstrap_servers,
            "message.timeout.ms": 4000,
        }
    )
    errors: list[object] = []
    p.produce(name, value=b"x", on_delivery=lambda err, _msg: errors.append(err))
    p.flush(10)
    assert errors and errors[0] is not None
    admin = AdminClient({"bootstrap.servers": kafka_settings.bootstrap_servers})
    time.sleep(0.5)
    assert name not in admin.list_topics(timeout=10).topics


def test_schema_registry_default_compatibility_is_backward(
    kafka_settings: KafkaSettings,
) -> None:
    client = SchemaRegistryClient({"url": kafka_settings.schema_registry_url})
    assert client.get_compatibility() == "BACKWARD"
