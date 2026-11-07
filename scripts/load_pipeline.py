"""Load test of the event pipeline: synthetic users -> Kafka -> processors -> Redis (D90).

    # the isolated benchmark stack (never the development stack):
    docker compose -f infra/docker-compose.bench.yml up -d --wait
    export VITALMESH_KAFKA_BOOTSTRAP=127.0.0.1:9192 VITALMESH_SCHEMA_REGISTRY_URL=http://127.0.0.1:8181
    export VITALMESH_REDIS_URL=redis://127.0.0.1:6390/0
    uv run python scripts/load_pipeline.py --events 100000 --users 100000 --procs 3 [--rate 800]
    docker compose -f infra/docker-compose.bench.yml down -v

What it does: resets the contract topics and flushes Redis on the BENCH stack (it refuses anything
that is not 127.0.0.1:9192 and 127.0.0.1:6390), starts ``--procs`` real ``vitalmesh_processor``
processes, generates ``--events`` events for ``--users`` distinct synthetic people (measurements,
about 3% lab results), and samples the broker once a second. Probe events (one at a time, on a
separate synthetic person) measure end-to-end latency: from the publish call until the new reading
is visible in Redis. After the drain it checks nothing was lost or duplicated.

What the numbers mean (state this whenever they are quoted): synthetic users, one laptop, one
local broker with 6 partitions, time compressed (a day of events in minutes). Saturated runs show
throughput; their latency is queueing time. Use ``--rate`` below capacity for latency.
"""

from __future__ import annotations

import argparse
import atexit
import itertools
import json
import os
import random
import statistics
import subprocess
import sys
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import chaos_lib
import psutil
import redis
from confluent_kafka.admin import AdminClient
from vitalmesh.events.v1 import payloads_pb2
from vitalmesh_common.envelope import EventMeta
from vitalmesh_common.kafka import (
    KafkaPublisher,
    KafkaSettings,
    SchemaRegistrySerde,
    ensure_topics,
)
from vitalmesh_common.lag import consumer_lag
from vitalmesh_common.retry import RetryPolicy
from vitalmesh_common.topics import USER_EVENTS, all_topic_specs, dlq_topic
from vitalmesh_stores import RedisHistoryStore

BENCH_KAFKA, BENCH_REDIS = "127.0.0.1:9192", "127.0.0.1:6390"
METRICS = {  # metric -> (unit, low, high), plausible values as the API would accept
    "step_count": ("count", 500, 20000),
    "body_weight": ("kg", 45, 110),
    "resting_heart_rate": ("bpm", 50, 90),
    "sleep_duration": ("h", 4, 9),
    "active_minutes": ("min", 0, 120),
    "energy_intake": ("kcal", 1200, 3200),
}
PROBE_USER = "bench-probe"
LAB_SHARE = 0.03


def refuse_unless_bench(settings: KafkaSettings, redis_url: str) -> None:
    if settings.bootstrap_servers != BENCH_KAFKA or BENCH_REDIS not in redis_url:
        raise SystemExit(
            f"refusing to run: this test only runs against the bench stack ({BENCH_KAFKA}, "
            f"{BENCH_REDIS}); got {settings.bootstrap_servers} and {redis_url}"
        )


def reset_topics(settings: KafkaSettings) -> None:
    admin = AdminClient({"bootstrap.servers": settings.bootstrap_servers})
    names = [s.name for s in all_topic_specs(RetryPolicy().max_retries)]
    existing = [n for n in names if n in admin.list_topics(timeout=15).topics]
    if existing:
        for future in admin.delete_topics(existing, operation_timeout=30).values():
            future.result()
    deadline = time.monotonic() + 30
    while existing and set(existing) & set(admin.list_topics(timeout=15).topics):
        if time.monotonic() > deadline:
            raise SystemExit("topics were not deleted in time")
        time.sleep(0.5)
    ensure_topics(settings)


def end_offsets(admin_settings: KafkaSettings, topic: str) -> int:
    from confluent_kafka import Consumer, TopicPartition

    c = Consumer(
        {"bootstrap.servers": admin_settings.bootstrap_servers, "group.id": f"probe-{uuid.uuid4()}"}
    )
    try:
        parts = c.list_topics(topic, timeout=10).topics[topic].partitions
        return sum(c.get_watermark_offsets(TopicPartition(topic, p), timeout=10)[1] for p in parts)
    finally:
        c.close()


class Sampler(threading.Thread):
    """Once a second: how far the benchmark group's commits have moved, and the lag."""

    def __init__(self, settings: KafkaSettings, prefix: str) -> None:
        super().__init__(daemon=True)
        self.settings, self.prefix = settings, prefix
        # t, committed, lag, members, retry lag, retry commits (D92: an event that failed
        # once waits in a retry tier for seconds to minutes; a run is not over until they drain)
        self.points: list[tuple[float, int, int, int, int, int]] = []
        self.stop = threading.Event()

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                every = consumer_lag(self.settings, (self.prefix,))
                rows = [r for r in every if r.topic == USER_EVENTS]
                retries = [r for r in every if r.topic.startswith(USER_EVENTS + ".retry.")]
                committed = sum(max(r.committed or 0, 0) for r in rows)
                lag = sum(r.lag for r in rows)
                members = max((r.members for r in rows), default=0)
                retry_lag = sum(r.lag for r in retries)
                retry_done = sum(max(r.committed or 0, 0) for r in retries)
                self.points.append(
                    (time.monotonic(), committed, lag, members, retry_lag, retry_done)
                )
            except Exception:  # a sample may fail while the group rebalances: skip it
                pass
            self.stop.wait(1.0)


class Prober(threading.Thread):
    """Publish one probe reading at a time and time how long until Redis shows it."""

    def __init__(self, serde: SchemaRegistrySerde, publisher: KafkaPublisher, client: redis.Redis):
        super().__init__(daemon=True)
        self.serde, self.publisher = serde, publisher
        self.hist = RedisHistoryStore(client)
        self.latencies_ms: list[float] = []
        self.timeouts = 0
        self.sent = 0
        self.stop = threading.Event()

    def run(self) -> None:
        n = 0
        while not self.stop.is_set():
            n += 1
            self.sent += 1
            value = float(1000 + n)
            sent = time.perf_counter()
            self.publisher.publish(
                USER_EVENTS, PROBE_USER, event(PROBE_USER, "step_count", value, n)
            )
            self.publisher.flush(10)
            deadline = sent + 60
            while time.perf_counter() < deadline and not self.stop.is_set():
                if any(p.value == value for p in self.hist.series(PROBE_USER, "step_count")[-5:]):
                    self.latencies_ms.append((time.perf_counter() - sent) * 1000)
                    break
                time.sleep(0.005)
            else:
                if not self.stop.is_set():
                    self.timeouts += 1
            self.stop.wait(0.5)


_SERDE: SchemaRegistrySerde | None = None
_BASE = datetime.now(UTC) - timedelta(hours=6)


def event(user: str, metric: str, value: float, seq: int) -> bytes:
    """One measurement event, as the API would publish it (measured_at unique per event)."""
    assert _SERDE is not None
    unit = METRICS[metric][0]
    meta = EventMeta(
        event_id=str(uuid.uuid4()), correlation_id=f"load-{seq}", user_id=user,
        household_id="default",
        type="measurement.recorded", occurred_at=datetime.now(UTC),
    )  # fmt: skip
    payload = payloads_pb2.MeasurementRecorded(metric_type=metric)
    payload.value.value, payload.value.unit = value, unit
    payload.measured_at.FromDatetime((_BASE + timedelta(milliseconds=seq)).replace(tzinfo=None))
    return _SERDE.serialize_event(USER_EVENTS, meta, payload)


def lab_event(user: str, seq: int) -> bytes:
    assert _SERDE is not None
    meta = EventMeta(
        event_id=str(uuid.uuid4()), correlation_id=f"load-{seq}", user_id=user,
        household_id="default",
        type="lab.result.recorded", occurred_at=datetime.now(UTC),
    )  # fmt: skip
    payload = payloads_pb2.LabResultRecorded(marker_name="LDL Cholesterol", marker_code="")
    payload.numeric.value, payload.numeric.unit = 100.0 + seq % 60, "mg/dL"
    payload.collected_at.FromDatetime((_BASE + timedelta(milliseconds=seq)).replace(tzinfo=None))
    return _SERDE.serialize_event(USER_EVENTS, meta, payload)


def tree_cpu(proc: psutil.Process) -> float:
    """CPU seconds of a process and every child (a venv launcher starts the real interpreter)."""
    total = 0.0
    try:
        family = [proc, *proc.children(recursive=True)]
    except psutil.Error:  # the process is gone (a chaos test killed it): it has no CPU to count
        return 0.0
    for p in family:
        try:
            times = p.cpu_times()
            total += times.user + times.system
        except psutil.Error:
            pass
    return total


def kill_tree(pid: int) -> None:
    """Kill a process and its children; never raises (used at exit, even after a crash)."""
    try:
        root = psutil.Process(pid)
        for child in root.children(recursive=True):
            child.kill()
        root.kill()
    except psutil.Error:
        pass


def pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, max(0, round(q * len(s) + 0.5) - 1))]


def main() -> None:
    global _SERDE
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--events", type=int, default=100_000)
    ap.add_argument("--users", type=int, default=100_000)
    ap.add_argument("--procs", type=int, default=1)
    ap.add_argument(
        "--rate", type=float, default=0.0, help="events/s to publish; 0 = as fast as possible"
    )
    ap.add_argument("--no-probe", action="store_true")
    ap.add_argument("--drain-timeout", type=float, default=3600)
    ap.add_argument("--stall-seconds", type=float, default=180.0)
    ap.add_argument("--json", default="")
    ap.add_argument(
        "--reset-only",
        action="store_true",
        help="flush the bench Redis, reset the topics and exit (step 1 of a multi-machine run)",
    )
    ap.add_argument(
        "--no-reset",
        action="store_true",
        help="do NOT reset: processors on other machines already joined after --reset-only",
    )
    ap.add_argument(
        "--chaos", default="", help="kill:processor@T, respawn:processor@T, broker@T (D92)"
    )
    ap.add_argument("--group", default="", help="consumer group name (default: random)")
    ap.add_argument(
        "--wait-members",
        type=int,
        default=0,
        help="processors to wait for in the group (default --procs); more than --procs means "
        "processors on OTHER machines join the same group (Azure runs, D92)",
    )
    args = ap.parse_args()

    settings = KafkaSettings.from_env()
    redis_url = os.environ.get("VITALMESH_REDIS_URL", "")
    refuse_unless_bench(settings, redis_url)
    r = redis.Redis.from_url(redis_url)
    if not args.no_reset:
        r.flushall()
        reset_topics(settings)
    if args.reset_only:
        print("bench Redis flushed and topics reset")
        return
    group = args.group or f"bench-proc-{uuid.uuid4().hex[:6]}"
    chaos_actions = chaos_lib.parse(args.chaos)
    wait_members = args.wait_members or args.procs
    mem0 = int(r.info("memory")["used_memory"])

    env = {**os.environ, "VITALMESH_CONSUMER_GROUP": group, "VITALMESH_LOG_LEVEL": "WARNING"}
    procs: list[subprocess.Popen[bytes]] = []

    def spawn_processor() -> subprocess.Popen[bytes]:
        out = (Path(os.environ.get("TEMP", ".")) / f"bench-proc-{len(procs)}.log").open("w")
        proc = subprocess.Popen(
            [sys.executable, "-m", "vitalmesh_processor"],
            env=env,
            stdout=out,  # kept open for the child's lifetime
            stderr=subprocess.STDOUT,
        )
        procs.append(proc)
        return proc

    for _ in range(args.procs):
        spawn_processor()
    atexit.register(lambda: [kill_tree(p.pid) for p in procs])  # a crash must not leave them
    sampler = Sampler(settings, group)
    sampler.start()
    t_wait = time.monotonic()
    while time.monotonic() - t_wait < 120:
        if sampler.points and sampler.points[-1][3] >= wait_members:
            break
        time.sleep(1)
    else:
        for p in procs:
            p.kill()
        raise SystemExit(f"{wait_members} processors did not all join the group in 120 s")
    time.sleep(5)  # let the rebalance settle
    print(f"{wait_members} processor process(es) ready in group {group}")

    _SERDE = SchemaRegistrySerde(settings)
    publisher = KafkaPublisher(settings, client_id="load-generator")
    ps = [psutil.Process(p.pid) for p in procs]
    cpu0 = [tree_cpu(p) for p in ps]
    prober = Prober(_SERDE, publisher, r)
    if not args.no_probe:
        prober.start()

    rng = random.Random(7)
    metrics = list(METRICS)
    lab_users: set[str] = set()
    meas_n = lab_n = 0
    has_state = bytearray(args.users)  # 1 once a person has a measurement event

    def do_kill(_: str) -> str:
        for proc in procs:
            if proc.poll() is None:
                kill_tree(proc.pid)
                return f"killed pid {proc.pid} (processor)"
        return "no live processor"

    def do_respawn(_: str) -> str:
        return f"started pid {spawn_processor().pid} (processor)"

    t0 = time.monotonic()
    chaos = chaos_lib.Chaos(
        chaos_actions,
        {"kill": do_kill, "respawn": do_respawn, "broker": chaos_lib.restart_bench_broker},
        t0,
    )
    chaos.start()
    produced = 0
    for i in range(args.events):
        user = f"bu-{i % args.users:07d}"
        if rng.random() < LAB_SHARE:
            lab_users.add(user)
            lab_n += 1
            publisher.publish(USER_EVENTS, user, lab_event(user, i))
        else:
            m = metrics[rng.randrange(len(metrics))]
            lo, hi = METRICS[m][1], METRICS[m][2]
            meas_n += 1
            has_state[i % args.users] = 1
            publisher.publish(USER_EVENTS, user, event(user, m, round(rng.uniform(lo, hi), 1), i))
        produced += 1
        if args.rate:
            ahead = t0 + produced / args.rate - time.monotonic()
            if ahead > 0:
                time.sleep(ahead)
    publisher.flush(120)
    t_produced = time.monotonic()
    print(
        f"produced {produced:,} events in {t_produced - t0:.1f}s "
        f"({produced / (t_produced - t0):,.0f}/s)"
    )

    total_on_topic = end_offsets(settings, USER_EVENTS)
    deadline = time.monotonic() + args.drain_timeout
    drained = False
    stalled = False
    best, best_at = -1, time.monotonic()
    while time.monotonic() < deadline:
        pt = sampler.points[-1] if sampler.points else None
        if pt and pt[1] >= total_on_topic and pt[2] == 0 and pt[4] == 0:
            drained = True
            break
        if pt and pt[1] + pt[5] > best:
            best, best_at = pt[1] + pt[5], time.monotonic()
        if (
            time.monotonic() - best_at > args.stall_seconds
        ):  # no commit moved: do not wait for hours
            stalled = True
            print(f"STALLED: no progress for {args.stall_seconds:.0f}s at {best:,} committed")
            break
        time.sleep(1)
    t_done = time.monotonic()
    chaos.stop_event.set()
    prober.stop.set()
    sampler.stop.set()
    time.sleep(1.5)
    total_on_topic = end_offsets(settings, USER_EVENTS)

    cpu1 = [tree_cpu(p) for p in ps]
    cpu_s = [b - a for a, b in zip(cpu0, cpu1, strict=True)]
    for p in procs:
        p.terminate()
    for p in procs:
        try:
            p.wait(15)
        except subprocess.TimeoutExpired:
            p.kill()

    # --- throughput: committed-offset movement per second while there was a backlog
    pts = [p for p in sampler.points if p[0] >= t0]
    rates = [
        (b[1] - a[1]) / (b[0] - a[0])
        for a, b in itertools.pairwise(pts)
        if b[0] > a[0] and a[2] > 0
    ]
    steady = rates[len(rates) // 5 : -max(len(rates) // 10, 1)] or rates  # trim ramp-up and tail
    whole = (total_on_topic) / (t_done - t0)

    # --- correctness: nothing lost, nothing duplicated
    hist_points = 0
    state_keys = 0
    lab_keys = 0
    hist_keys: list[str] = []
    for key in r.scan_iter(match="*", count=5000):
        k = key.decode()
        if k.startswith("hist:bu-"):
            hist_keys.append(k)
        elif k.startswith("state:bu-"):
            state_keys += 1
        elif k.startswith("labs:bu-"):
            lab_keys += 1
    for start in range(0, len(hist_keys), 2000):  # pipelined: one round trip per 2000 keys
        pipe = r.pipeline(transaction=False)
        for k in hist_keys[start : start + 2000]:
            pipe.zcard(k)
        hist_points += sum(int(n) for n in pipe.execute())
    dlq = end_offsets(settings, dlq_topic(USER_EVENTS))
    retries = sum(end_offsets(settings, f"{USER_EVENTS}.retry.{t}") for t in (1, 2, 3))
    mem1 = int(r.info("memory")["used_memory"])
    users_seen = sum(has_state)

    result: dict[str, Any] = {
        "procs": args.procs, "events": produced, "users": users_seen, "rate_target": args.rate,
        "produce_per_s": round(produced / (t_produced - t0)),
        "saturated_throughput_per_s": round(statistics.median(steady)) if steady else None,
        "whole_run_per_s": round(whole),
        "seconds_to_drain_after_last_publish": round(t_done - t_produced, 1),
        "cpu_seconds_per_process": [round(x, 1) for x in cpu_s],
        "events_per_cpu_second": round(total_on_topic / sum(cpu_s)) if sum(cpu_s) else None,
        "probe_ms": {"n": len(prober.latencies_ms), "timeouts": prober.timeouts,
                     "p50": pct(prober.latencies_ms, 0.5), "p95": pct(prober.latencies_ms, 0.95),
                     "p99": pct(prober.latencies_ms, 0.99)},
        "max_lag": max((p[2] for p in pts), default=0),
        "events_on_topic": total_on_topic, "probe_events": prober.sent, "dlq": dlq,
        "retry_topic_messages": retries,
        "chaos": args.chaos,
        "chaos_actions_run": chaos.done,
        "consumer_group": group,
        "processors_expected": wait_members,
        "check": {
            "all_committed_before_timeout": drained,
            "stalled": stalled,
            "history_points_equal_measurements": hist_points == meas_n,
            "people_with_state_equal_people": state_keys == users_seen,
            "people_with_labs_equal": lab_keys == len(lab_users),
            "expected_history_points": meas_n, "expected_people": users_seen,
            "expected_people_with_labs": len(lab_users),
        },
        "state_keys_for_people": state_keys, "people_with_labs": lab_keys,
        "history_points": hist_points,
        "redis_bytes_per_person": round((mem1 - mem0) / max(state_keys, 1)),
        "redis_used_mb": round(mem1 / 1e6, 1),
    }  # fmt: skip
    print(json.dumps(result, indent=2))
    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
