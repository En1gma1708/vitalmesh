"""Lab result history per user.

Idempotent and order-independent: a result is keyed by (marker name, collection time), so a
redelivered or re-ordered event rewrites the same entry and nothing is duplicated. Names are
case-folded so ``ALT`` and ``alt`` are one series.
"""

from __future__ import annotations

from typing import Protocol

import redis
from vitalmesh_wellness.labs import LabResult


def _field(result: LabResult) -> str:
    return f"{result.name.strip().lower()}|{result.collected_at.isoformat()}"


class LabStore(Protocol):
    def put(self, user_id: str, result: LabResult) -> None: ...

    def list(self, user_id: str) -> list[LabResult]: ...


def _sorted(items: list[LabResult]) -> list[LabResult]:
    return sorted(items, key=lambda r: (r.collected_at, r.name.lower()))


class InMemoryLabStore:
    def __init__(self) -> None:
        self._data: dict[str, dict[str, str]] = {}

    def put(self, user_id: str, result: LabResult) -> None:
        self._data.setdefault(user_id, {})[_field(result)] = result.model_dump_json()

    def list(self, user_id: str) -> list[LabResult]:
        raw = self._data.get(user_id, {})
        return _sorted([LabResult.model_validate_json(v) for v in raw.values()])


class RedisLabStore:
    def __init__(self, client: redis.Redis, prefix: str = "labs") -> None:
        self._r = client
        self._prefix = prefix

    def put(self, user_id: str, result: LabResult) -> None:
        self._r.hset(f"{self._prefix}:{user_id}", _field(result), result.model_dump_json())

    def list(self, user_id: str) -> list[LabResult]:
        raw = self._r.hgetall(f"{self._prefix}:{user_id}")
        return _sorted([LabResult.model_validate_json(v) for v in raw.values()])
