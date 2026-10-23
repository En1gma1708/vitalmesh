"""Rate limits: a fixed window counter, per key (D75).

Used for three things, each with its own key and limit: login attempts per IP and per email (slow
down password guessing), sign-ups per IP (slow down account farming) and questions per user (each
question can spend model quota, which on the free tier is the scarcest resource in the system).
The window lives in Redis so every API replica shares it; the in-memory twin backs the tests.
A rate limiter that cannot reach Redis FAILS CLOSED for login and sign-up (the caller decides).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Protocol

import redis


class RateLimiter(Protocol):
    def hit(self, key: str, limit: int, window_s: int) -> bool:
        """Count one use of ``key``; False when it is over ``limit`` in this window."""
        ...

    def reset(self, key: str) -> None: ...


class InMemoryRateLimiter:
    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._windows: dict[str, tuple[float, int]] = {}

    def hit(self, key: str, limit: int, window_s: int) -> bool:
        now = self._clock()
        start, count = self._windows.get(key, (now, 0))
        if now - start >= window_s:
            start, count = now, 0
        count += 1
        self._windows[key] = (start, count)
        return count <= limit

    def reset(self, key: str) -> None:
        self._windows.pop(key, None)


class RedisRateLimiter:
    def __init__(self, client: redis.Redis, prefix: str = "rl") -> None:
        self._r, self._prefix = client, prefix

    def hit(self, key: str, limit: int, window_s: int) -> bool:
        k = f"{self._prefix}:{key}"
        pipe = self._r.pipeline()
        pipe.incr(k)
        pipe.expire(k, window_s, nx=True)  # the window starts at the first hit and is not extended
        count = int(pipe.execute()[0])
        return count <= limit

    def reset(self, key: str) -> None:
        self._r.delete(f"{self._prefix}:{key}")
