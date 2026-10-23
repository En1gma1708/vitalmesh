"""The load signal behind graceful degradation, and the per-tier token budget (D70, D73).

Triage's tier policy needs to know when the model-backed paths are being asked for more than the
provider will serve. The signal is TOKEN-WEIGHTED: every T2 or T3 plan adds its tier's expected
token cost to a sliding window, and the window is compared with the provider's tokens-per-window
budget: at ``HIGH_AT`` of the budget Triage reduces specialists and calls, at ``CRITICAL_AT`` it
answers low-risk questions with no model at all (T0). Cache hits (T1) and shed runs (T0) cost no
tokens, so they are not load.

Where the numbers come from (be careful quoting them):
* ``TIER_TOKEN_COST``: the mean tokens per case measured in the ablation (D71, run 2, 8 cases, free
  tier): about 5,100 for the one-specialist path (T2) and 12,200 for the full graph (T3). They are
  means over a mix that includes heavy meal-plan questions; a simple question costs less, so the
  estimate errs towards shedding early.
* ``DEFAULT_TOKENS_PER_WINDOW``: Groq's free-tier limit for the model in use, 8,000 tokens a
  minute (CLAUDE.md). On a paid or self-hosted model it is set by ``VITALMESH_TOKENS_PER_WINDOW``.
At those numbers one T3 run is 1.5 times the whole free-tier minute, which is why the free tier
cannot serve concurrent full runs and why the cache and shedding matter: this is arithmetic from
measured means, not a load test. Phase 6 measures real capacity on Azure.

The window is kept in Redis so it is shared across Triage replicas.
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
DEFAULT_TOKENS_PER_WINDOW = 8000
HIGH_AT = 0.6
CRITICAL_AT = 1.0
TIER_TOKEN_COST: dict[str, int] = {"T2": 5100, "T3": 12200}  # measured means, see above

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
    def record(self, tier: str) -> None: ...

    def load(self) -> Load: ...


def classify(tokens_in_window: float, budget: float) -> Load:
    if tokens_in_window >= budget * CRITICAL_AT:
        return Load.CRITICAL
    if tokens_in_window >= budget * HIGH_AT:
        return Load.HIGH
    return Load.NORMAL


def cost_of(tier: str) -> int:
    return TIER_TOKEN_COST.get(tier, 0)


class InMemoryLoadMonitor:
    def __init__(
        self,
        tokens_per_window: int = DEFAULT_TOKENS_PER_WINDOW,
        window_s: float = DEFAULT_WINDOW_S,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._budget, self._window, self._clock = tokens_per_window, window_s, clock
        self._events: list[tuple[float, int]] = []

    def record(self, tier: str) -> None:
        self._events.append((self._clock(), cost_of(tier)))

    def load(self) -> Load:
        cutoff = self._clock() - self._window
        self._events = [(t, c) for t, c in self._events if t > cutoff]
        return classify(sum(c for _, c in self._events), self._budget)


class RedisLoadMonitor:
    def __init__(
        self,
        client: redis.Redis,
        tokens_per_window: int | None = None,
        window_s: float = DEFAULT_WINDOW_S,
        key: str = "load:model-tokens",
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._r = client
        self._budget = tokens_per_window or int(
            os.environ.get("VITALMESH_TOKENS_PER_WINDOW", DEFAULT_TOKENS_PER_WINDOW)
        )
        self._window, self._key, self._clock = window_s, key, clock

    def record(self, tier: str) -> None:
        now = self._clock()
        pipe = self._r.pipeline()
        pipe.zadd(self._key, {f"{uuid.uuid4().hex}:{cost_of(tier)}": now})
        pipe.zremrangebyscore(self._key, "-inf", now - self._window)
        pipe.expire(self._key, int(self._window * 2))
        pipe.execute()

    def load(self) -> Load:
        now = self._clock()
        members = self._r.zrangebyscore(self._key, now - self._window, "+inf")
        total = 0
        for m in members:
            raw = m.decode() if isinstance(m, bytes) else str(m)
            total += int(raw.rsplit(":", 1)[1])
        return classify(total, self._budget)
