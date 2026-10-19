"""Profile read model for the T0 path.

Phase 1 limitation (documented, not hidden): profiles and the last nights of sleep are
seeded by the synthetic generator; the state processor does not yet maintain windowed
history, so ``recent_sleep_hours`` comes from this record. Replacing it with a windowed
read of real events is tracked in docs/IMPLEMENTATION.md.
"""

from __future__ import annotations

from typing import Protocol

import redis
from pydantic import BaseModel, ConfigDict
from vitalmesh_wellness.engine import Profile


class ProfileRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    profile: Profile
    recent_sleep_hours: tuple[float, ...] = ()


class ProfileStore(Protocol):
    def get(self, user_id: str) -> ProfileRecord | None: ...

    def put(self, record: ProfileRecord) -> None: ...


class InMemoryProfileStore:
    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    def get(self, user_id: str) -> ProfileRecord | None:
        raw = self._data.get(user_id)
        return ProfileRecord.model_validate_json(raw) if raw else None

    def put(self, record: ProfileRecord) -> None:
        self._data[record.profile.user_id] = record.model_dump_json()


class RedisProfileStore:
    def __init__(self, client: redis.Redis, prefix: str = "t0:profile") -> None:
        self._r = client
        self._prefix = prefix

    def get(self, user_id: str) -> ProfileRecord | None:
        raw = self._r.get(f"{self._prefix}:{user_id}")
        return ProfileRecord.model_validate_json(raw) if raw else None

    def put(self, record: ProfileRecord) -> None:
        self._r.set(f"{self._prefix}:{record.profile.user_id}", record.model_dump_json())
