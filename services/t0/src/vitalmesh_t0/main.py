"""Entry point: ``uv run python -m vitalmesh_t0``.

One consumer thread per input topic (``user.state`` plus its retry tiers), as in the state
processor; all share the idempotency namespace ``GROUP``.
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
from vitalmesh_common.topics import USER_STATE, retry_topic

from vitalmesh_t0.handler import make_handler
from vitalmesh_t0.profiles import RedisProfileStore

GROUP = os.environ.get("VITALMESH_CONSUMER_GROUP", "t0-recommender")
log = logging.getLogger("vitalmesh.t0")


def input_topics(policy: RetryPolicy) -> list[str]:
    return [USER_STATE, *[retry_topic(USER_STATE, t) for t in range(1, policy.max_retries + 1)]]


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("VITALMESH_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    for noisy in ("httpx", "httpx2", "httpcore", "httpcore2"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    settings = KafkaSettings.from_env()
    policy = RetryPolicy()
    redis_url = os.environ.get("VITALMESH_REDIS_URL", "redis://127.0.0.1:6379/0")
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
        handler=make_handler(RedisProfileStore(client), serde, publisher),
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
