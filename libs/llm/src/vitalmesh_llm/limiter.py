"""Per-provider rate limiting: requests per minute and tokens per minute (token buckets).

Free tiers are small (Groq developer plan: 30 requests/min, 8,000 tokens/min), so the router
asks the limiter BEFORE calling and skips to the next provider when there is no budget,
instead of burning a request on a guaranteed 429. The clock is injectable for tests.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable


class _Bucket:
    def __init__(self, capacity: float, per_minute: float, now: float) -> None:
        self.capacity = capacity
        self.rate = per_minute / 60.0
        self.level = capacity
        self.updated = now

    def refill(self, now: float) -> None:
        self.level = min(self.capacity, self.level + (now - self.updated) * self.rate)
        self.updated = now


class RateLimiter:
    def __init__(
        self,
        rpm: int,
        tpm: int,
        clock: Callable[[], float] = time.monotonic,
        rpd: int | None = None,
    ) -> None:
        self._clock = clock
        now = clock()
        self._req = _Bucket(rpm, rpm, now)
        self._tok = _Bucket(tpm, tpm, now)
        # a daily cap refills evenly over 24 h (rpd per 1,440 minutes)
        self._day = _Bucket(rpd, rpd / 1440, now) if rpd is not None else None
        self._lock = threading.Lock()

    def seconds_until(self, tokens: int) -> float | None:
        """Seconds until a request of ``tokens`` could be admitted, or None if it never can
        (larger than the whole token bucket)."""
        with self._lock:
            now = self._clock()
            buckets = [(self._req, 1.0), (self._tok, float(tokens))]
            if self._day is not None:
                buckets.append((self._day, 1.0))
            wait = 0.0
            for b, need in buckets:
                if need > b.capacity:
                    return None
                b.refill(now)
                if b.level < need:
                    wait = max(wait, (need - b.level) / b.rate)
            return wait

    def try_acquire(self, tokens: int) -> bool:
        """Reserve one request and ``tokens`` tokens, or reserve nothing and return False."""
        with self._lock:
            now = self._clock()
            self._req.refill(now)
            self._tok.refill(now)
            if self._day is not None:
                self._day.refill(now)
            if tokens > self._tok.capacity or self._req.level < 1 or self._tok.level < tokens:
                return False
            if self._day is not None:
                if self._day.level < 1:
                    return False
                self._day.level -= 1
            self._req.level -= 1
            self._tok.level -= tokens
            return True
