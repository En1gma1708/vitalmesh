"""Per-user measurement history for charts (D53).

Idempotent: a point is identified by (time, value, unit), so a redelivered event re-adds the
same point and nothing is duplicated; order of arrival does not matter because points are kept
sorted by measurement time. Bounded: only the newest ``MAX_POINTS`` per metric are kept.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

import redis

MAX_POINTS = 3650


@dataclass(frozen=True)
class Point:
    t: datetime
    value: float
    unit: str


class HistoryStore(Protocol):
    def add(self, user_id: str, metric: str, point: Point) -> None: ...

    def series(self, user_id: str, metric: str, since: datetime | None = None) -> list[Point]: ...


def _member(p: Point) -> str:
    return f"{int(p.t.timestamp() * 1000)}|{p.value!r}|{p.unit}"


def _parse(member: str) -> Point:
    ms, value, unit = member.split("|", 2)
    return Point(datetime.fromtimestamp(int(ms) / 1000, tz=UTC), float(value), unit)


class InMemoryHistoryStore:
    def __init__(self) -> None:
        self._data: dict[tuple[str, str], set[str]] = {}

    def add(self, user_id: str, metric: str, point: Point) -> None:
        self._data.setdefault((user_id, metric), set()).add(_member(point))

    def series(self, user_id: str, metric: str, since: datetime | None = None) -> list[Point]:
        pts = sorted((_parse(m) for m in self._data.get((user_id, metric), ())), key=lambda p: p.t)
        return [p for p in pts if since is None or p.t >= since]


class RedisHistoryStore:
    def __init__(self, client: redis.Redis, prefix: str = "hist") -> None:
        self._r = client
        self._prefix = prefix

    def _key(self, user_id: str, metric: str) -> str:
        return f"{self._prefix}:{user_id}:{metric}"

    def add(self, user_id: str, metric: str, point: Point) -> None:
        key = self._key(user_id, metric)
        pipe = self._r.pipeline()
        pipe.zadd(key, {_member(point): point.t.timestamp() * 1000})
        pipe.zremrangebyrank(key, 0, -(MAX_POINTS + 1))  # keep the newest MAX_POINTS
        pipe.execute()

    def series(self, user_id: str, metric: str, since: datetime | None = None) -> list[Point]:
        low = since.timestamp() * 1000 if since else "-inf"
        raw = self._r.zrangebyscore(self._key(user_id, metric), low, "+inf")
        members = [m.decode() if isinstance(m, bytes) else str(m) for m in raw]
        return [_parse(m) for m in members]
