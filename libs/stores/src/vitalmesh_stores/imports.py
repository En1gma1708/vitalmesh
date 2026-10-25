"""Imports waiting for the person's confirmation (D79).

An uploaded health file is parsed into daily readings and shown to the person; nothing is stored as
their data until they confirm. In between, the parsed readings wait here under a random import id,
for a short time, readable only with the owner's user id (a wrong owner gets nothing, exactly like
an unknown id). Health data lives in this store for at most the time to live, then disappears, and
``purge_user`` removes it at once when the account is deleted.
"""

from __future__ import annotations

import json
import time
from typing import Any, Protocol

import redis

PENDING_TTL_S = 900
PREFIX = "pending-import"


class PendingImports(Protocol):
    def put(self, user_id: str, import_id: str, payload: dict[str, Any]) -> None: ...

    def get(self, user_id: str, import_id: str) -> dict[str, Any] | None: ...

    def delete(self, user_id: str, import_id: str) -> None: ...


class InMemoryPendingImports:
    def __init__(self, ttl_s: int = PENDING_TTL_S, clock: Any = time.monotonic) -> None:
        self._items: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
        self._ttl, self._clock = ttl_s, clock

    def put(self, user_id: str, import_id: str, payload: dict[str, Any]) -> None:
        self._items[(user_id, import_id)] = (self._clock() + self._ttl, payload)

    def get(self, user_id: str, import_id: str) -> dict[str, Any] | None:
        entry = self._items.get((user_id, import_id))
        if entry is None or entry[0] <= self._clock():
            self._items.pop((user_id, import_id), None)
            return None
        return entry[1]

    def delete(self, user_id: str, import_id: str) -> None:
        self._items.pop((user_id, import_id), None)


class RedisPendingImports:
    def __init__(self, client: redis.Redis, ttl_s: int = PENDING_TTL_S) -> None:
        self._r, self._ttl = client, ttl_s

    @staticmethod
    def _key(user_id: str, import_id: str) -> str:
        return f"{PREFIX}:{user_id}:{import_id}"

    def put(self, user_id: str, import_id: str, payload: dict[str, Any]) -> None:
        self._r.set(self._key(user_id, import_id), json.dumps(payload), ex=self._ttl)

    def get(self, user_id: str, import_id: str) -> dict[str, Any] | None:
        raw = self._r.get(self._key(user_id, import_id))
        if raw is None:
            return None
        loaded = json.loads(raw.decode() if isinstance(raw, bytes) else str(raw))
        return loaded if isinstance(loaded, dict) else None

    def delete(self, user_id: str, import_id: str) -> None:
        self._r.delete(self._key(user_id, import_id))
