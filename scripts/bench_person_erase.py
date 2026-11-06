"""Measure what the per-person index buys (D89): erase and export time against keyspace size, and
what it costs on every write.

    uv run python scripts/bench_person_erase.py [--sizes 0 100000 500000 1000000]

Method: for each size, fill Redis with that many filler keys (``bench:filler:*``, removed at the
end), then time (a) the OLD way (SCAN the whole keyspace for the person's history and pending
keys, batch 1000, as the code did after the first fix) and (b) ``purge_user`` / ``export_user``
through the index, each on a fresh person with the same small amount of data; median of 5. Then
the write overhead: ``RedisHistoryStore.add`` with and without the index entry, 5000 adds each.
One laptop, one local Redis, synthetic keys: it measures how cost scales with keyspace size, not
production capacity.
"""

from __future__ import annotations

import argparse
import os
import statistics
import time
import uuid
from datetime import UTC, datetime, timedelta

import redis
from vitalmesh_stores import (
    Point,
    RedisHistoryStore,
    RedisPendingImports,
    export_user,
    purge_user,
)

FILLER = "bench:filler:"


def fill(client: redis.Redis, target: int, have: int) -> int:
    pipe = client.pipeline()
    for i in range(have, target):
        pipe.set(f"{FILLER}{i}", "x")
        if i % 5000 == 4999:
            pipe.execute()
            pipe = client.pipeline()
    pipe.execute()
    return max(target, have)


def person(client: redis.Redis) -> str:
    user = f"bench-{uuid.uuid4().hex[:10]}"
    hist = RedisHistoryStore(client)
    now = datetime.now(UTC)
    for metric in ("body_weight", "step_count", "resting_heart_rate"):
        for d in range(30):
            hist.add(user, metric, Point(now - timedelta(days=d), 60.0 + d, "x"))
    RedisPendingImports(client).put(user, "abcdefgh1234", {"readings": []})
    client.hset(f"labs:{user}", "ldl", "{}")  # a hash, as RedisLabStore writes it
    client.set(f"state:{user}", "{}")
    return user


def old_erase(client: redis.Redis, user: str) -> None:
    keys = [f"t0:profile:{user}", f"labs:{user}", f"state:{user}"]
    for pattern in (f"hist:{user}:*", f"pending-import:{user}:*"):
        keys += [k.decode() for k in client.scan_iter(match=pattern, count=1000)]
    client.delete(*keys)


def old_export(client: redis.Redis, user: str) -> None:
    for raw in client.scan_iter(match=f"hist:{user}:*", count=1000):
        client.zrange(raw, 0, -1)


def timed(fn, *args) -> float:  # type: ignore[no-untyped-def]
    t = time.perf_counter()
    fn(*args)
    return (time.perf_counter() - t) * 1000


def median_ms(client: redis.Redis, fn, make_user: bool = True) -> float:  # type: ignore[no-untyped-def]
    runs = []
    for _ in range(5):
        user = person(client)
        runs.append(timed(fn, client, user))
        purge_user(client, user)
    return statistics.median(runs)


def write_overhead(client: redis.Redis, n: int = 5000) -> tuple[float, float]:
    hist = RedisHistoryStore(client)
    user = f"bench-{uuid.uuid4().hex[:10]}"
    now = datetime.now(UTC)
    t = time.perf_counter()
    for i in range(n):
        hist.add(user, "body_weight", Point(now - timedelta(seconds=i), float(i), "kg"))
    with_index = (time.perf_counter() - t) / n * 1e6
    key = f"hist:{user}:body_weight"
    t = time.perf_counter()
    for i in range(n):  # the code as it was before the index: same pipeline, no SADD
        pipe = client.pipeline()
        pipe.zadd(key, {f"{i}|x|kg": float(i)})
        pipe.zremrangebyrank(key, 0, -3651)
        pipe.execute()
    without = (time.perf_counter() - t) / n * 1e6
    purge_user(client, user)
    client.delete(key)
    return with_index, without


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", type=int, nargs="*", default=[0, 100_000, 500_000, 1_000_000])
    args = ap.parse_args()
    client = redis.Redis.from_url(os.environ.get("VITALMESH_REDIS_URL", "redis://127.0.0.1:6379/0"))
    base = int(client.dbsize())
    print(f"keys in Redis before the filler: {base}")
    print(
        f"{'keyspace':>12} | {'old erase':>10} | {'index erase':>11} | "
        f"{'old export':>10} | {'index export':>12}"
    )
    have = 0
    try:
        for size in args.sizes:
            have = fill(client, size, have)
            total = int(client.dbsize())
            row = (
                median_ms(client, old_erase),
                median_ms(client, purge_user),
                median_ms(client, old_export),
                median_ms(client, export_user),
            )
            print(
                f"{total:>12,} | {row[0]:>8.1f}ms | {row[1]:>9.1f}ms | "
                f"{row[2]:>8.1f}ms | {row[3]:>10.1f}ms"
            )
        with_i, without = write_overhead(client)
        print(
            f"history write: {with_i:.0f} us with the index entry, "
            f"{without:.0f} us without (5000 adds each)"
        )
    finally:
        pipe = client.pipeline()
        for i in range(have):
            pipe.delete(f"{FILLER}{i}")
            if i % 5000 == 4999:
                pipe.execute()
                pipe = client.pipeline()
        pipe.execute()
        print(f"filler removed; keys in Redis now: {client.dbsize()}")


if __name__ == "__main__":
    main()
