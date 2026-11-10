"""What workout session the system last suggested to a person (D104).

The workout planner avoids giving the same muscle group two days in a row, so it needs to know the
last session it suggested. It records the SUGGESTION (the system cannot see what the person did),
one session name per day, the last 14 days only. Every key is registered in the per-person index
(``personkeys.track``) before the write, so export and erase reach it.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol

import redis

from vitalmesh_stores.personkeys import track

KEEP_DAYS = 14
TTL_SECONDS = 60 * 24 * 3600


class WorkoutLog(Protocol):
    def recent(self, user_id: str) -> dict[date, str]: ...

    def record(self, user_id: str, day: date, session: str) -> None: ...


def _trim(entries: dict[date, str]) -> dict[date, str]:
    keep = sorted(entries)[-KEEP_DAYS:]
    return {d: entries[d] for d in keep}


class InMemoryWorkoutLog:
    def __init__(self) -> None:
        self._data: dict[str, dict[date, str]] = {}

    def recent(self, user_id: str) -> dict[date, str]:
        return dict(self._data.get(user_id, {}))

    def record(self, user_id: str, day: date, session: str) -> None:
        entries = self._data.setdefault(user_id, {})
        entries[day] = session
        self._data[user_id] = _trim(entries)


class RedisWorkoutLog:
    def __init__(self, client: redis.Redis, prefix: str = "workoutlog") -> None:
        self._r = client
        self._prefix = prefix

    def _key(self, user_id: str) -> str:
        return f"{self._prefix}:{user_id}"

    def recent(self, user_id: str) -> dict[date, str]:
        out: dict[date, str] = {}
        for field, value in self._r.hgetall(self._key(user_id)).items():
            try:
                out[date.fromisoformat(_text(field))] = _text(value)
            except ValueError:
                continue  # a malformed field is ignored, never trusted
        return out

    def record(self, user_id: str, day: date, session: str) -> None:
        key = self._key(user_id)
        pipe = self._r.pipeline()
        track(pipe, user_id, key)  # registered BEFORE the write, in the same round trip
        pipe.hset(key, day.isoformat(), session)
        pipe.expire(key, TTL_SECONDS)
        pipe.execute()
        entries = self.recent(user_id)
        stale = [d for d in sorted(entries) if d not in _trim(entries)]
        if stale:
            self._r.hdel(key, *[d.isoformat() for d in stale])


def _text(raw: object) -> str:
    return raw.decode() if isinstance(raw, bytes) else str(raw)
