"""Entry point: ``uv run python -m vitalmesh_processor``.

One consumer thread per input topic: the main topic plus one per retry tier. They are kept
separate so a record waiting out its retry delay never blocks another topic's partitions.
All of them share the idempotency namespace ``GROUP`` (they are one logical consumer).
"""

from __future__ import annotations

import logging
import os
import signal
import threading
from types import FrameType

import redis
from vitalmesh_common.idempotency import RedisIdempotencyStore
from vitalmesh_common.kafka import (
    KafkaPublisher,
    KafkaSettings,
    SchemaRegistrySerde,
    build_consumer,
    ensure_topics,
    run_consumer,
)
from vitalmesh_common.processor import EventProcessor
from vitalmesh_common.retry import RetryPolicy
from vitalmesh_common.topics import USER_EVENTS, retry_topic

from vitalmesh_processor.handler import make_handler
from vitalmesh_processor.state import RedisStateStore

GROUP = "state-processor"
log = logging.getLogger("vitalmesh.processor")


def input_topics(policy: RetryPolicy) -> list[str]:
    return [USER_EVENTS, *[retry_topic(USER_EVENTS, t) for t in range(1, policy.max_retries + 1)]]


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = KafkaSettings.from_env()
    policy = RetryPolicy()
    redis_url = os.environ.get("VITALMESH_REDIS_URL", "redis://localhost:6379/0")
    client = redis.Redis.from_url(redis_url)

    created = ensure_topics(settings)
    if created:
        log.info("created topics: %s", ", ".join(created))

    serde = SchemaRegistrySerde(settings)
    publisher = KafkaPublisher(settings, client_id=GROUP)
    processor = EventProcessor(
        group=GROUP,
        serde=serde,
        publisher=publisher,
        store=RedisIdempotencyStore(client),
        handler=make_handler(RedisStateStore(client), serde, publisher),
        policy=policy,
    )

    stop = threading.Event()

    def _stop(signum: int, _frame: FrameType | None) -> None:
        log.info("signal %s received, shutting down", signum)
        stop.set()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    threads = []
    for topic in input_topics(policy):
        consumer = build_consumer(settings, f"{GROUP}.{topic}")
        t = threading.Thread(
            target=run_consumer,
            args=(consumer, [topic], processor, stop),
            name=f"consume-{topic}",
            daemon=True,
        )
        t.start()
        threads.append(t)
        log.info("consuming %s", topic)
    for t in threads:
        t.join()


if __name__ == "__main__":
    main()
