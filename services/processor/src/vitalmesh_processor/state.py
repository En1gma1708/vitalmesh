"""Per-user health state: pure domain logic plus a store protocol.

``apply_measurement`` is idempotent and commutative per metric (latest ``measured_at``
wins), so duplicates and out-of-order deliveries cannot corrupt state. Non-finite numbers
are rejected: a NaN or infinity must never become a "latest value" that rules act on.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Protocol

import redis
from pydantic import BaseModel, ConfigDict, Field
from vitalmesh_common.failure import EnvelopeError


class Metric(BaseModel):
    model_config = ConfigDict(frozen=True)

    value: float
    unit: str = Field(min_length=1)
    measured_at: datetime


class HealthState(BaseModel):
    user_id: str
    metrics: dict[str, Metric] = Field(default_factory=dict)
    as_of: datetime | None = None
    version: int = 0  # increments only when state actually changes


def apply_measurement(
    state: HealthState,
    metric_type: str,
    value: float,
    unit: str,
    measured_at: datetime,
) -> HealthState:
    if not metric_type:
        raise EnvelopeError("measurement has no metric_type")
    if not unit:
        raise EnvelopeError(f"measurement {metric_type!r} has no unit")
    if not math.isfinite(value):
        raise EnvelopeError(f"measurement {metric_type!r} is not a finite number")
    if measured_at.tzinfo is None:
        raise EnvelopeError("measured_at must be timezone-aware")

    current = state.metrics.get(metric_type)
    if current is not None and current.measured_at >= measured_at:
        return state  # duplicate or older: no change, so replays are harmless
    metrics = {
        **state.metrics,
        metric_type: Metric(value=value, unit=unit, measured_at=measured_at),
    }
    as_of = measured_at if state.as_of is None or measured_at > state.as_of else state.as_of
    return state.model_copy(
        update={"metrics": metrics, "as_of": as_of, "version": state.version + 1}
    )


class StateStore(Protocol):
    def get(self, user_id: str) -> HealthState: ...

    def put(self, state: HealthState) -> None: ...


class InMemoryStateStore:
    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    def get(self, user_id: str) -> HealthState:
        raw = self._data.get(user_id)
        return HealthState.model_validate_json(raw) if raw else HealthState(user_id=user_id)

    def put(self, state: HealthState) -> None:
        self._data[state.user_id] = state.model_dump_json()


class RedisStateStore:
    """One JSON document per user. Per-user ordering comes from the Kafka partition key, so
    there is a single writer per user at a time and plain GET/SET is sufficient."""

    def __init__(self, client: redis.Redis, prefix: str = "state") -> None:
        self._r = client
        self._prefix = prefix

    def get(self, user_id: str) -> HealthState:
        raw = self._r.get(f"{self._prefix}:{user_id}")
        return HealthState.model_validate_json(raw) if raw else HealthState(user_id=user_id)

    def put(self, state: HealthState) -> None:
        self._r.set(f"{self._prefix}:{state.user_id}", state.model_dump_json())
