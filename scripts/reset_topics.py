"""Delete and recreate every topic in the contract on the LOCAL dev stack.

Why: a consumer group that is new (a first start of the agents, a changed group name) replays a
topic from the beginning, so leftovers from earlier runs and tests are processed again, spending
LLM quota on stale questions. Run this once before the first start of the agents for a demo, with
the agents stopped. It deletes data on the local stack only; it refuses anything that is not
127.0.0.1.

    uv run python scripts/reset_topics.py
"""

from __future__ import annotations

import time

from confluent_kafka.admin import AdminClient
from vitalmesh_common.kafka import KafkaSettings, ensure_topics
from vitalmesh_common.retry import RetryPolicy
from vitalmesh_common.topics import all_topic_specs


def main() -> None:
    settings = KafkaSettings.from_env()
    if "127.0.0.1" not in settings.bootstrap_servers:
        raise SystemExit("refusing to reset a Kafka that is not on 127.0.0.1")
    admin = AdminClient({"bootstrap.servers": settings.bootstrap_servers})
    names = [s.name for s in all_topic_specs(RetryPolicy().max_retries)]
    existing = [n for n in names if n in admin.list_topics(timeout=15).topics]
    for future in admin.delete_topics(existing, operation_timeout=30).values():
        future.result()
    deadline = time.monotonic() + 30
    while existing and time.monotonic() < deadline:
        if not set(existing) & set(admin.list_topics(timeout=15).topics):
            break
        time.sleep(0.5)
    created = ensure_topics(settings)
    print(f"deleted {len(existing)} topics, created {len(created)}")


if __name__ == "__main__":
    main()
