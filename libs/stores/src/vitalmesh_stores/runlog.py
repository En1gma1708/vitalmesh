"""The timeline of one question's run, for the app and the operator view (D74).

A ``tracker`` consumer in the agents service appends one entry per event of a run (by correlation
id); the API reads it. Redis is the shared store, so any API replica can serve any run and the API
never needs a Kafka consumer of its own. Entries are plain dicts with a stable shape (see
``vitalmesh_agents.tracker``); they carry no user identifiers beyond the user id that owns the run,
which the API checks before showing anything. Entries expire (a run log is a convenience, not a
record of truth: the topics are).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

import redis

RUN_TTL_S = 3600


@dataclass(frozen=True)
class RunRecord:
    user_id: str
    entries: list[dict[str, Any]] = field(default_factory=list)


class RunLog(Protocol):
    def append(self, correlation_id: str, user_id: str, entry: dict[str, Any]) -> None: ...

    def read(self, correlation_id: str) -> RunRecord | None: ...


class InMemoryRunLog:
    def __init__(self) -> None:
        self._runs: dict[str, RunRecord] = {}

    def append(self, correlation_id: str, user_id: str, entry: dict[str, Any]) -> None:
        record = self._runs.setdefault(correlation_id, RunRecord(user_id))
        if not any(e.get("event_id") == entry.get("event_id") for e in record.entries):
            record.entries.append(entry)  # at-least-once delivery: a redelivery adds nothing

    def read(self, correlation_id: str) -> RunRecord | None:
        return self._runs.get(correlation_id)


class RedisRunLog:
    def __init__(self, client: redis.Redis, prefix: str = "run", ttl_s: int = RUN_TTL_S) -> None:
        self._r, self._prefix, self._ttl = client, prefix, ttl_s

    def _key(self, correlation_id: str, part: str) -> str:
        return f"{self._prefix}:{correlation_id}:{part}"

    def append(self, correlation_id: str, user_id: str, entry: dict[str, Any]) -> None:
        seen, entries, owner = (
            self._key(correlation_id, "seen"),
            self._key(correlation_id, "entries"),
            self._key(correlation_id, "user"),
        )
        # the event id makes the append idempotent under redelivery
        if self._r.sadd(seen, str(entry.get("event_id", ""))) == 0:
            return
        pipe = self._r.pipeline()
        pipe.rpush(entries, json.dumps(entry))
        pipe.set(owner, user_id)
        for key in (seen, entries, owner):
            pipe.expire(key, self._ttl)
        pipe.execute()

    def read(self, correlation_id: str) -> RunRecord | None:
        owner = self._r.get(self._key(correlation_id, "user"))
        if owner is None:
            return None
        raw = self._r.lrange(self._key(correlation_id, "entries"), 0, -1)
        entries = [json.loads(r.decode() if isinstance(r, bytes) else str(r)) for r in raw]
        return RunRecord(owner.decode() if isinstance(owner, bytes) else str(owner), entries)
