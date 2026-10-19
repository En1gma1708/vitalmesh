"""Root test configuration: integration tests skip cleanly when the compose stack is down."""

from __future__ import annotations

import time

import pytest
import redis
from confluent_kafka.admin import AdminClient
from vitalmesh_common.kafka import KafkaSettings, ensure_topics
from vitalmesh_common.retry import RetryPolicy
from vitalmesh_common.testing import service_reachable
from vitalmesh_common.topics import all_topic_specs

_STACK_PORTS = {"kafka": 9092, "schema-registry": 8081, "redis": 6379}


def _missing_services() -> list[str]:
    return [n for n, port in _STACK_PORTS.items() if not service_reachable("127.0.0.1", port)]


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    integration = [i for i in items if "integration" in i.keywords]
    if not integration:
        return
    missing = _missing_services()
    if missing:
        skip = pytest.mark.skip(
            reason=f"compose stack not running ({', '.join(missing)} unreachable): "
            "docker compose -f infra/docker-compose.yml up -d --wait"
        )
        for item in integration:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def kafka_settings() -> KafkaSettings:
    return KafkaSettings.from_env()


@pytest.fixture(scope="session")
def redis_client() -> redis.Redis:
    return redis.Redis.from_url("redis://127.0.0.1:6379/0")


@pytest.fixture(scope="session", autouse=True)
def fresh_topics(request: pytest.FixtureRequest) -> None:
    """Start every integration session from empty contract topics.

    A fresh consumer group replays a topic from the beginning, and replaying old poison
    messages and in-flight conflicts re-creates retry and DLQ records. Left alone, the shared
    dev topics grow with every run and each run gets slower. This resets only the topics in
    the topic contract on the local dev stack; it is a no-op when no integration test runs.
    """
    if not any("integration" in i.keywords for i in request.session.items):
        return
    if _missing_services():
        return
    settings = KafkaSettings.from_env()
    admin = AdminClient({"bootstrap.servers": settings.bootstrap_servers})
    names = [s.name for s in all_topic_specs(RetryPolicy().max_retries)]
    existing = [n for n in names if n in admin.list_topics(timeout=15).topics]
    if existing:
        for future in admin.delete_topics(existing, operation_timeout=30).values():
            future.result()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if not set(existing) & set(admin.list_topics(timeout=15).topics):
                break
            time.sleep(0.5)
    ensure_topics(settings)
    # Topic metadata is eventually consistent: wait until every topic is actually visible.
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if set(names) <= set(admin.list_topics(timeout=15).topics):
            break
        time.sleep(0.5)
