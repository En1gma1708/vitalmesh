"""Per-run state for the Aggregator: the plan, the parts that arrived and the open deadlines.

Redis is the store (D5: store only, never a bus). Every operation is idempotent and
order-independent, because the plan (``runs.planned``) and the parts (``advice.completed``) come
from different topics and either can arrive first. State has a TTL so an abandoned run cleans
itself up; a closed run leaves only a short-lived marker so late parts are recognised and dropped.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Protocol

import redis
from pydantic import BaseModel, ConfigDict

STATE_TTL_S = 24 * 3600
CLOSING_TTL_S = 60  # a closer that dies mid-way blocks others for at most this long


class PlanRecord(BaseModel):
    """What the Aggregator needs to close a run and to address the bundle it emits."""

    model_config = ConfigDict(frozen=True)

    correlation_id: str
    plan_event_id: str
    user_id: str
    household_id: str
    trace_id: str | None
    tier: str | None
    question: str
    risk: str
    expected: tuple[str, ...]  # specialist values
    deadline_ms: int


@dataclass(frozen=True)
class Snapshot:
    plan: PlanRecord | None
    parts: dict[str, bytes] = field(default_factory=dict)  # specialist value -> AdviceCompleted


class RunStore(Protocol):
    def save_plan(self, plan: PlanRecord) -> None: ...

    def save_part(self, correlation_id: str, specialist: str, data: bytes) -> None: ...

    def snapshot(self, correlation_id: str) -> Snapshot: ...

    def is_closed(self, correlation_id: str) -> bool: ...

    def try_close(self, correlation_id: str) -> bool:
        """Take the right to close the run (one winner). False if closed or being closed."""
        ...

    def release_close(self, correlation_id: str) -> None: ...

    def finish(self, correlation_id: str) -> None:
        """Mark closed and delete the run's state."""
        ...

    def due(self, now_ms: int, limit: int = 100) -> list[str]:
        """Open runs whose deadline has passed."""
        ...


class InMemoryRunStore:
    def __init__(self) -> None:
        self._plans: dict[str, PlanRecord] = {}
        self._parts: dict[str, dict[str, bytes]] = {}
        self._closed: set[str] = set()
        self._closing: set[str] = set()

    def save_plan(self, plan: PlanRecord) -> None:
        if plan.correlation_id in self._closed:
            return
        self._plans[plan.correlation_id] = plan

    def save_part(self, correlation_id: str, specialist: str, data: bytes) -> None:
        if correlation_id in self._closed:
            return
        self._parts.setdefault(correlation_id, {})[specialist] = data

    def snapshot(self, correlation_id: str) -> Snapshot:
        return Snapshot(self._plans.get(correlation_id), dict(self._parts.get(correlation_id, {})))

    def is_closed(self, correlation_id: str) -> bool:
        return correlation_id in self._closed

    def try_close(self, correlation_id: str) -> bool:
        if correlation_id in self._closed or correlation_id in self._closing:
            return False
        self._closing.add(correlation_id)
        return True

    def release_close(self, correlation_id: str) -> None:
        self._closing.discard(correlation_id)

    def finish(self, correlation_id: str) -> None:
        self._closed.add(correlation_id)
        self._closing.discard(correlation_id)
        self._plans.pop(correlation_id, None)
        self._parts.pop(correlation_id, None)

    def due(self, now_ms: int, limit: int = 100) -> list[str]:
        late = [p for p in self._plans.values() if p.deadline_ms <= now_ms]
        return [p.correlation_id for p in sorted(late, key=lambda p: p.deadline_ms)][:limit]


class RedisRunStore:
    def __init__(self, client: redis.Redis, prefix: str = "agg") -> None:
        self._r = client
        self._p = prefix

    def _k(self, kind: str, cid: str) -> str:
        return f"{self._p}:{kind}:{cid}"

    @property
    def _deadlines(self) -> str:
        return f"{self._p}:deadlines"

    def save_plan(self, plan: PlanRecord) -> None:
        cid = plan.correlation_id
        if self._r.exists(self._k("closed", cid)):
            return
        pipe = self._r.pipeline()
        pipe.set(self._k("plan", cid), plan.model_dump_json(), ex=STATE_TTL_S)
        pipe.zadd(self._deadlines, {cid: plan.deadline_ms})
        pipe.execute()

    def save_part(self, correlation_id: str, specialist: str, data: bytes) -> None:
        if self._r.exists(self._k("closed", correlation_id)):
            return
        key = self._k("parts", correlation_id)
        pipe = self._r.pipeline()
        pipe.hset(key, specialist, data)
        pipe.expire(key, STATE_TTL_S)
        pipe.execute()

    def snapshot(self, correlation_id: str) -> Snapshot:
        raw = self._r.get(self._k("plan", correlation_id))
        plan = PlanRecord.model_validate_json(raw) if raw else None
        parts_raw = self._r.hgetall(self._k("parts", correlation_id))
        parts = {
            (k.decode() if isinstance(k, bytes) else str(k)): (
                v if isinstance(v, bytes) else str(v).encode()
            )
            for k, v in parts_raw.items()
        }
        return Snapshot(plan, parts)

    def is_closed(self, correlation_id: str) -> bool:
        return bool(self._r.exists(self._k("closed", correlation_id)))

    def try_close(self, correlation_id: str) -> bool:
        if self.is_closed(correlation_id):
            return False
        return bool(self._r.set(self._k("closing", correlation_id), "1", nx=True, ex=CLOSING_TTL_S))

    def release_close(self, correlation_id: str) -> None:
        self._r.delete(self._k("closing", correlation_id))

    def finish(self, correlation_id: str) -> None:
        pipe = self._r.pipeline()
        pipe.set(self._k("closed", correlation_id), "1", ex=STATE_TTL_S)
        pipe.delete(
            self._k("plan", correlation_id),
            self._k("parts", correlation_id),
            self._k("closing", correlation_id),
        )
        pipe.zrem(self._deadlines, correlation_id)
        pipe.execute()

    def due(self, now_ms: int, limit: int = 100) -> list[str]:
        raw = self._r.zrangebyscore(self._deadlines, "-inf", now_ms, start=0, num=limit)
        return [m.decode() if isinstance(m, bytes) else str(m) for m in raw]


def now_ms() -> int:
    return int(time.time() * 1000)
