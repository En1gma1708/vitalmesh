"""Idempotency store contract (docs/EVENT_BACKBONE.md section 6).

Kafka is at-least-once, so every consumer dedupes. A plain "seen" flag is wrong: if the
handler fails after the flag is set, the retry would be dropped as a duplicate. So the
protocol is three-state: ``acquire`` marks the key in flight (short TTL, so a crashed
worker does not lock it forever), ``commit`` marks it done (long TTL), ``release``
clears it so a retry can run.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from enum import StrEnum
from typing import Protocol

import redis


class Acquired(StrEnum):
    NEW = "new"  # first time: caller must process
    IN_FLIGHT = "in_flight"  # another worker holds it right now
    DONE = "done"  # already processed: caller must skip


class IdempotencyStore(Protocol):
    def acquire(self, namespace: str, key: str) -> Acquired: ...

    def commit(self, namespace: str, key: str) -> None: ...

    def release(self, namespace: str, key: str) -> None: ...


IN_FLIGHT_TTL_S = 60
DONE_TTL_S = 24 * 60 * 60


class InMemoryIdempotencyStore:
    """Test double with an injectable clock."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._items: dict[str, tuple[str, float]] = {}  # key -> (state, expires_at)

    @staticmethod
    def _k(namespace: str, key: str) -> str:
        return f"{namespace}\x00{key}"

    def acquire(self, namespace: str, key: str) -> Acquired:
        k = self._k(namespace, key)
        now = self._clock()
        item = self._items.get(k)
        if item is not None and item[1] > now:
            return Acquired.DONE if item[0] == "done" else Acquired.IN_FLIGHT
        self._items[k] = ("in_flight", now + IN_FLIGHT_TTL_S)
        return Acquired.NEW

    def commit(self, namespace: str, key: str) -> None:
        self._items[self._k(namespace, key)] = ("done", self._clock() + DONE_TTL_S)

    def release(self, namespace: str, key: str) -> None:
        self._items.pop(self._k(namespace, key), None)


class RedisIdempotencyStore:
    """Redis-backed store (Redis is used as a store only, never as a bus: docs D5)."""

    def __init__(self, client: redis.Redis, prefix: str = "idem") -> None:
        self._r = client
        self._prefix = prefix

    def _k(self, namespace: str, key: str) -> str:
        return f"{self._prefix}:{namespace}:{key}"

    def acquire(self, namespace: str, key: str) -> Acquired:
        k = self._k(namespace, key)
        if self._r.set(k, "in_flight", nx=True, ex=IN_FLIGHT_TTL_S):
            return Acquired.NEW
        state = self._r.get(k)
        if state is None:  # expired between SET NX and GET: try once more
            if self._r.set(k, "in_flight", nx=True, ex=IN_FLIGHT_TTL_S):
                return Acquired.NEW
            state = self._r.get(k)
        return Acquired.DONE if state in (b"done", "done") else Acquired.IN_FLIGHT

    def commit(self, namespace: str, key: str) -> None:
        self._r.set(self._k(namespace, key), "done", ex=DONE_TTL_S)

    def release(self, namespace: str, key: str) -> None:
        self._r.delete(self._k(namespace, key))
