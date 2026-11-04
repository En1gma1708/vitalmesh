"""Measure the account layer (D81): what one process of it can do, and where it stops.

    uv run python scripts/load_accounts.py [--seconds 15] [--concurrency 64] [--users 50]

Needs the compose stack (Postgres, Redis, Kafka). It starts ONE API process itself (loopback
only) and stops it at the end. Four measurements, each reported with percentiles:

  A. argon2id verify: the cost of a password check (a sign-in is mostly this), per core.
  B. account store on Postgres: session lookup latency, single thread and 16 threads.
  C. rate limiter on Redis: hits per second.
  D. HTTP: an authenticated GET /v1/me (token check + session lookup + user lookup) against the
     real API process, plus GET /healthz as the framework-only baseline.

The numbers are for ONE laptop process with the load generator on the same machine, so they are a
floor for the code, not a capacity claim for a deployment. Scale statements built from them are
arithmetic, and say so (see docs/DECISIONS.md D81). Test users are created and removed by the
script; nothing else is touched.
"""

from __future__ import annotations

import argparse
import asyncio
import multiprocessing
import os
import secrets
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from datetime import UTC, datetime

import httpx
import redis
from vitalmesh_accounts import (
    Claims,
    PostgresAccountStore,
    RedisRateLimiter,
    TokenIssuer,
    hash_password,
    verify_password,
)

DSN = os.environ.get("VITALMESH_PG_DSN", "postgresql://vitalmesh@127.0.0.1:5432/vitalmesh")
PORT = int(os.environ.get("VITALMESH_LOAD_PORT", "8000"))  # children read this at import
BASE = f"http://127.0.0.1:{PORT}"


def pct(values: list[float], p: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, int(len(s) * p))]


def line(name: str, ms: list[float], seconds: float | None = None) -> None:
    rate = f" | {len(ms) / seconds:,.0f}/s" if seconds else ""
    print(
        f"  {name:<34} n={len(ms):<6} p50 {pct(ms, 0.50):7.2f} ms  p95 {pct(ms, 0.95):7.2f} ms  "
        f"p99 {pct(ms, 0.99):7.2f} ms{rate}"
    )


def measure_argon2() -> None:
    print("A. argon2id password check (one core)")
    stored = hash_password("correct horse battery staple")
    times = []
    for _ in range(20):
        t = time.perf_counter()
        assert verify_password(stored, "correct horse battery staple")
        times.append((time.perf_counter() - t) * 1000)
    line("verify_password", times)
    print(f"  -> about {1000 / statistics.mean(times):.0f} password checks per second per core\n")


def make_users(store: PostgresAccountStore, issuer: TokenIssuer, n: int) -> list[tuple[str, str]]:
    stored = hash_password("correct horse battery staple")
    out = []
    tag = secrets.token_hex(3)
    for i in range(n):
        user = store.create_user(f"load-{tag}-{i}@example.com", stored)
        store.mark_email_verified(user.user_id)
        session = store.create_session(
            user.user_id, secrets.token_hex(16), datetime.now(UTC).replace(year=2099)
        )
        token = issuer.access(Claims(user.user_id, "user", session.session_id), datetime.now(UTC))
        out.append((user.user_id, token))
    return out


def measure_store(store: PostgresAccountStore, users: list[tuple[str, str]]) -> None:
    print("B. account store on Postgres (session lookup)")
    sessions = []
    for uid, _ in users:
        found = [
            s for s in [store._one("SELECT session_id FROM sessions WHERE user_id=%s", (uid,))] if s
        ]
        sessions.append(str(found[0][0]))
    single = []
    for i in range(300):
        t = time.perf_counter()
        store.session(sessions[i % len(sessions)])
        single.append((time.perf_counter() - t) * 1000)
    line("session lookup, 1 thread", single)

    lat: list[float] = []
    lock = threading.Lock()
    stop = time.perf_counter() + 5

    def worker(k: int) -> None:
        mine = []
        i = k
        while time.perf_counter() < stop:
            t = time.perf_counter()
            store.session(sessions[i % len(sessions)])
            mine.append((time.perf_counter() - t) * 1000)
            i += 1
        with lock:
            lat.extend(mine)

    threads = [threading.Thread(target=worker, args=(k,)) for k in range(16)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    line("session lookup, 16 threads", lat, 5)
    print()


def measure_limiter() -> None:
    print("C. rate limiter on Redis")
    limiter = RedisRateLimiter(redis.Redis.from_url("redis://127.0.0.1:6379/0"))
    key = f"loadtest:{secrets.token_hex(4)}"
    times = []
    for _ in range(500):
        t = time.perf_counter()
        limiter.hit(key, 10**9, 60)
        times.append((time.perf_counter() - t) * 1000)
    line("limiter.hit", times)
    print()


async def _connection(
    path: str, tokens: list[str], stop: float, offset: int, out: list[float], errors: list[int]
) -> None:
    """One keep-alive connection speaking minimal HTTP/1.1 (httpx tops out near 500 req/s here,
    which would measure the generator instead of the server)."""
    reader, writer = await asyncio.open_connection("127.0.0.1", PORT)
    i = offset
    try:
        while time.perf_counter() < stop:
            auth = f"Authorization: Bearer {tokens[i % len(tokens)]}\r\n" if tokens else ""
            writer.write(f"GET {path} HTTP/1.1\r\nHost: x\r\n{auth}\r\n".encode())
            t = time.perf_counter()
            head = await reader.readuntil(b"\r\n\r\n")
            length = 0
            for header in head.split(b"\r\n"):
                if header.lower().startswith(b"content-length:"):
                    length = int(header.split(b":")[1])
            if length:
                await reader.readexactly(length)
            out.append((time.perf_counter() - t) * 1000)
            errors[0] += 0 if head.split(b" ", 2)[1] == b"200" else 1
            i += 1
    finally:
        writer.close()


def _drive(job: tuple[str, list[str], float, int, int]) -> tuple[list[float], int]:
    path, tokens, seconds, conns, proc = job

    async def go() -> tuple[list[float], int]:
        out: list[float] = []
        errors = [0]
        stop = time.perf_counter() + seconds
        await asyncio.gather(
            *(_connection(path, tokens, stop, proc * 1000 + k, out, errors) for k in range(conns))
        )
        return out, errors[0]

    return asyncio.run(go())


def http_load(
    name: str, path: str, tokens: list[str], seconds: float, total: int, procs: int
) -> None:
    jobs = [(path, tokens, seconds, max(1, total // procs), p) for p in range(procs)]
    with multiprocessing.Pool(procs) as pool:
        results = pool.map(_drive, jobs)
    lat = [x for r, _ in results for x in r]
    errors = sum(e for _, e in results)
    line(name, lat, seconds)
    if errors:
        print(f"  !! {errors} non-200 answers of {len(lat)}")


HELLO = "from fastapi import FastAPI\napp = FastAPI()\n@app.get('/healthz')\ndef h():\n    return {'status': 'ok'}\n"


def ceiling(args: argparse.Namespace) -> None:
    """The generator against a do-nothing FastAPI app: the most any server here could show."""
    with tempfile.TemporaryDirectory() as tmp:
        open(os.path.join(tmp, "hello_app.py"), "w", encoding="utf-8").write(HELLO)
        srv = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "hello_app:app", "--port", "8001", "--log-level", "error"],
            cwd=tmp, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )  # fmt: skip
        try:
            for _ in range(30):
                try:
                    if httpx.get("http://127.0.0.1:8001/healthz", timeout=2).status_code == 200:
                        break
                except httpx.HTTPError:
                    time.sleep(1)
            os.environ["VITALMESH_LOAD_PORT"] = "8001"
            http_load(
                "do-nothing app (ceiling)",
                "/healthz",
                [],
                args.seconds,
                args.concurrency,
                args.procs,
            )
        finally:
            srv.terminate()
            srv.wait(timeout=20)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=15)
    ap.add_argument("--concurrency", type=int, default=64, help="total connections")
    ap.add_argument("--procs", type=int, default=4, help="load generator processes")
    ap.add_argument("--users", type=int, default=50)
    args = ap.parse_args()

    secret = secrets.token_hex(32)
    issuer = TokenIssuer(secret.encode())
    measure_argon2()
    store = PostgresAccountStore(DSN)
    users = make_users(store, issuer, args.users)
    server = None
    try:
        measure_store(store, users)
        measure_limiter()
        env = {**os.environ, "VITALMESH_AUTH_SECRET": secret}
        server = subprocess.Popen(
            [sys.executable, "-m", "vitalmesh_api"], env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )  # fmt: skip
        for _ in range(60):
            try:
                if httpx.get(BASE + "/healthz", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(1)
        print(
            f"D. HTTP ({args.concurrency} connections over {args.procs} generator processes, "
            f"{args.seconds:.0f} s each)"
        )
        ceiling(args)
        os.environ["VITALMESH_LOAD_PORT"] = "8000"
        tokens = [t for _, t in users]
        http_load(
            "API GET /healthz (no auth)", "/healthz", [], args.seconds, args.concurrency, args.procs
        )
        http_load(
            "API GET /v1/me (authenticated)",
            "/v1/me",
            tokens,
            args.seconds,
            args.concurrency,
            args.procs,
        )
    finally:
        if server is not None:
            server.terminate()
            server.wait(timeout=20)
        for uid, _ in users:
            store.delete_user(uid)
        store.close()
        print("\n(test users removed; the API process was stopped)")


if __name__ == "__main__":
    main()
