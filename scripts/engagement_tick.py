"""Publish the daily engagement tick for some people (the code-only ticker, D87).

    uv run python scripts/engagement_tick.py <user_id> [<user_id> ...]
    uv run python scripts/engagement_tick.py --opted-in        # everyone who switched nudges on

A tick is an ordinary event on ``engagement.ticks`` keyed by the person. Its id is derived from the
person and the day, so running this twice on one day is harmless (the consumers skip the duplicate).
In a deployment a scheduler runs this once a day, at the hour that suits each person's timezone;
here it is run by hand. It decides nothing: the Engagement agent and its policy do.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import UTC, datetime

import redis
from vitalmesh.events.v1 import payloads_pb2
from vitalmesh_common.envelope import EventMeta
from vitalmesh_common.kafka import KafkaPublisher, KafkaSettings, SchemaRegistrySerde
from vitalmesh_common.topics import ENGAGEMENT_TICKS

_NS = uuid.UUID("6f1d3c52-0f6e-4a47-9a0e-5f0c1d2b7a11")


def opted_in(client: redis.Redis) -> list[str]:
    users = []
    for key in client.scan_iter(match="nudge:prefs:*"):
        name = key.decode() if isinstance(key, bytes) else str(key)
        if client.hget(key, "enabled") in (b"1", "1"):
            users.append(name.split(":", 2)[2])
    return sorted(users)


def main() -> None:
    args = sys.argv[1:]
    if not args:
        raise SystemExit(__doc__)
    if args == ["--opted-in"]:
        client = redis.Redis.from_url(
            os.environ.get("VITALMESH_REDIS_URL", "redis://127.0.0.1:6379/0")
        )
        users = opted_in(client)
    else:
        users = args
    settings = KafkaSettings.from_env()
    serde = SchemaRegistrySerde(settings)
    publisher = KafkaPublisher(settings, client_id="engagement-ticker")
    now = datetime.now(UTC)
    day = now.date().isoformat()
    for user in users:
        meta = EventMeta(
            event_id=str(uuid.uuid5(_NS, f"{user}:{day}")),
            correlation_id=f"tick-{uuid.uuid5(_NS, f'{user}:{day}').hex[:12]}",
            user_id=user,
            household_id="default",
            type="engagement.tick",
            occurred_at=now,
        )
        publisher.publish(
            ENGAGEMENT_TICKS,
            user,
            serde.serialize_event(ENGAGEMENT_TICKS, meta, payloads_pb2.EngagementTick(day=day)),
        )
    publisher.flush()
    print(f"published {len(users)} tick(s) for {day}")


if __name__ == "__main__":
    main()
