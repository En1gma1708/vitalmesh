"""The load signal behind graceful degradation (D70).

Triage's tier policy needs to know when the model-backed paths are being asked for more than they
can serve. The signal here is the RATE of runs that need a model (T2 and T3) in a sliding window:
above ``high_runs`` Triage reduces specialists and calls, above ``critical_runs`` it answers
low-risk questions with no model at all (T0). Cache hits and shed runs are not counted: they cost
no model call, so they are not load.

Honest limits: the thresholds are CONFIGURED, not measured: on the free tier the real limit is
Groq's tokens-per-minute budget, and the defaults are only a safe starting point. Phase 6 load
tests replace them with a measured capacity. The count is per Redis, so it is shared across Triage
replicas, which is the point.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Callable
from typing import Protocol

import redis

from vitalmesh_agents.triage import Load

DEFAULT_WINDOW_S = 60.0
DEFAULT_HIGH_RUNS = 30
DEFAULT_CRITICAL_RUNS = 60


TIER_COUNT_KEY = "tiers:count"


class TierCounter(Protocol):
    def add(self, tier: str) -> None: ...

    def counts(self) -> dict[str, int]: ...


class InMemoryTierCounter:
    def __init__(self) -> None:
        self._counts: dict[str, int] = {}

    def add(self, tier: str) -> None:
        self._counts[tier] = self._counts.get(tier, 0) + 1

    def counts(self) -> dict[str, int]:
        return dict(self._counts)


class RedisTierCounter:
    """How many runs Triage planned at each tier (the live tier mix; the API ops route reads it)."""

    def __init__(self, client: redis.Redis, key: str = TIER_COUNT_KEY) -> None:
        self._r, self._key = client, key

    def add(self, tier: str) -> None:
        self._r.hincrby(self._key, tier, 1)

    def counts(self) -> dict[str, int]:
        return {
            (k.decode() if isinstance(k, bytes) else str(k)): int(v)
            for k, v in self._r.hgetall(self._key).items()
        }


class LoadMonitor(Protocol):
    def record(self) -> None: ...

    def load(self) -> Load: ...


def classify(runs_in_window: int, high_runs: int, critical_runs: int) -> Load:
    if runs_in_window >= critical_runs:
        return Load.CRITICAL
    if runs_in_window >= high_runs:
        return Load.HIGH
    return Load.NORMAL


class InMemoryLoadMonitor:
    def __init__(
        self,
        high_runs: int = DEFAULT_HIGH_RUNS,
        critical_runs: int = DEFAULT_CRITICAL_RUNS,
        window_s: float = DEFAULT_WINDOW_S,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._high, self._critical, self._window, self._clock = (
            high_runs,
            critical_runs,
            window_s,
            clock,
        )
        self._times: list[float] = []

    def record(self) -> None:
        self._times.append(self._clock())

    def load(self) -> Load:
        cutoff = self._clock() - self._window
        self._times = [t for t in self._times if t > cutoff]
        return classify(len(self._times), self._high, self._critical)


class RedisLoadMonitor:
    def __init__(
        self,
        client: redis.Redis,
        high_runs: int | None = None,
        critical_runs: int | None = None,
        window_s: float = DEFAULT_WINDOW_S,
        key: str = "load:model-runs",
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._r = client
        self._high = high_runs or int(os.environ.get("VITALMESH_LOAD_HIGH_RUNS", DEFAULT_HIGH_RUNS))
        self._critical = critical_runs or int(
            os.environ.get("VITALMESH_LOAD_CRITICAL_RUNS", DEFAULT_CRITICAL_RUNS)
        )
        self._window, self._key, self._clock = window_s, key, clock

    def record(self) -> None:
        now = self._clock()
        pipe = self._r.pipeline()
        pipe.zadd(self._key, {uuid.uuid4().hex: now})
        pipe.zremrangebyscore(self._key, "-inf", now - self._window)
        pipe.expire(self._key, int(self._window * 2))
        pipe.execute()

    def load(self) -> Load:
        now = self._clock()
        count = int(self._r.zcount(self._key, now - self._window, "+inf"))
        return classify(count, self._high, self._critical)
