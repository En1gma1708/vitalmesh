"""Load test of the multi-agent graph over Kafka with a SIMULATED model (D91).

What it measures: the cost of the graph itself (triage -> three specialists -> aggregator ->
synthesizer -> Guardian -> checker -> release gate -> tracker; every hop is a Kafka event, state
is in Redis and Postgres) with a model that answers instantly or after a latency you choose. It
makes no model call and spends no quota. The answers are fixed text, so this says nothing about
answer quality; it says how many questions per second the graph can move and how that scales when
you add consumer processes.

Runs ONLY against the isolated bench stack (Kafka 9192, Redis 6390, Postgres 5442):

    docker compose -f infra/docker-compose.bench.yml up -d --wait
    export VITALMESH_KAFKA_BOOTSTRAP=127.0.0.1:9192 VITALMESH_SCHEMA_REGISTRY_URL=http://127.0.0.1:8181
    export VITALMESH_REDIS_URL=redis://127.0.0.1:6390/0
    uv run python scripts/load_agents.py --questions 300 --procs 1 --sim-ms 800 --json out.json

Report it only as: synthetic people, simulated model, one laptop, time compressed.
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import random
import statistics
import subprocess
import sys
import time
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import chaos_lib
import psutil
import redis
from confluent_kafka import Consumer
from confluent_kafka.admin import AdminClient
from load_pipeline import (
    BENCH_KAFKA,
    BENCH_REDIS,
    end_offsets,
    kill_tree,
    pct,
    reset_topics,
    tree_cpu,
)
from vitalmesh.events.v1 import payloads_pb2
from vitalmesh_common.envelope import EventMeta
from vitalmesh_common.kafka import KafkaPublisher, KafkaSettings, SchemaRegistrySerde
from vitalmesh_common.lag import consumer_lag
from vitalmesh_common.topics import (
    ADVICE_COMPLETED,
    QUESTIONS_ASKED,
    RECOMMENDATIONS_RELEASED,
    REVIEWS_REQUESTED,
    SAFETY_BLOCKED,
)
from vitalmesh_t0.profiles import ProfileRecord, RedisProfileStore
from vitalmesh_wellness.synthetic import synthetic_user

BENCH_PG = "127.0.0.1:5442"
PG_DSN = f"postgresql://vitalmesh@{BENCH_PG}/vitalmesh"
ROLES = [
    "triage",
    "nutrition",
    "movement",
    "condition",
    "aggregator",
    "synthesizer",
    "guardian",
    "checker",
    "release",
    "tracker",
]
QUESTIONS = (
    "What should I eat this week and how should I train?",
    "How many calories should I aim for and how much should I move?",
    "What is a sensible plan for food and exercise for me?",
    "Can you suggest meals and a workout routine?",
)
FINAL_TYPES = {
    RECOMMENDATIONS_RELEASED: "released",
    SAFETY_BLOCKED: "blocked",
    REVIEWS_REQUESTED: "review_requested",
}
ADVICE_NAMES = {
    payloads_pb2.ADVICE_ANSWERED: "answered",
    payloads_pb2.ADVICE_FALLBACK: "fallback",
    payloads_pb2.ADVICE_DECLINED: "declined",
    payloads_pb2.ADVICE_FAILED: "failed",
}


def refuse_unless_bench(settings: KafkaSettings, redis_url: str) -> None:
    if settings.bootstrap_servers != BENCH_KAFKA or BENCH_REDIS not in redis_url:
        raise SystemExit(
            f"refusing to run: bench stack only ({BENCH_KAFKA}, {BENCH_REDIS}, {BENCH_PG}); "
            f"got {settings.bootstrap_servers} and {redis_url}"
        )


def parse_scale(text: str, default: int) -> dict[str, int]:
    scale = dict.fromkeys(ROLES, default)
    for part in filter(None, text.split(",")):
        role, _, n = part.partition("=")
        if role not in scale or not n.isdigit() or int(n) < 1:
            raise SystemExit(f"bad --scale entry {part!r}; roles: {ROLES}")
        scale[role] = int(n)
    return scale


def seed_people(r: redis.Redis, n: int) -> list[str]:
    store = RedisProfileStore(r)
    users = []
    for i in range(n):
        profile, sleep = synthetic_user(2026, i)
        profile = profile.model_copy(update={"user_id": f"ag-{i:05d}"})
        store.put(ProfileRecord(profile=profile, recent_sleep_hours=tuple(sleep)))
        users.append(profile.user_id)
    return users


def wait_ready(settings: KafkaSettings, scale: dict[str, int], timeout: float = 240) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            rows = consumer_lag(settings, ("agent-",))
        except Exception:
            rows = []
        members: dict[str, int] = {}
        for row in rows:
            role = row.group.removeprefix("agent-").split(".")[0]
            members[role] = max(members.get(role, 0), row.members)
        if all(members.get(role, 0) >= n for role, n in scale.items()):
            return
        time.sleep(2)
    raise SystemExit(f"the agent processes did not all join their groups in {timeout:.0f} s")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--questions", type=int, default=300)
    ap.add_argument("--users", type=int, default=200)
    ap.add_argument("--rate", type=float, default=0.0, help="questions/s; 0 = all at once")
    ap.add_argument("--procs", type=int, default=1, help="processes per role")
    ap.add_argument("--scale", default="", help="per-role override, e.g. nutrition=3,movement=3")
    ap.add_argument("--sim-ms", type=float, default=0.0, help="simulated model latency per call")
    ap.add_argument("--deadline-s", type=float, default=120.0, help="run deadline in the graph")
    ap.add_argument("--stall-seconds", type=float, default=90.0)
    ap.add_argument("--timeout", type=float, default=1800.0)
    ap.add_argument("--log-level", default="WARNING", help="agent process log level")
    ap.add_argument("--chaos", default="", help="scheduled failures, see scripts/chaos_lib.py")
    ap.add_argument(
        "--expect-degraded",
        action="store_true",
        help="a failed or fallback specialist is an expected outcome (model-outage runs)",
    )
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    settings = KafkaSettings.from_env()
    redis_url = os.environ.get("VITALMESH_REDIS_URL", "")
    refuse_unless_bench(settings, redis_url)
    scale = parse_scale(args.scale, args.procs)
    r = redis.Redis.from_url(redis_url)
    r.flushall()
    reset_topics(settings)
    users = seed_people(r, args.users)
    chaos_actions = chaos_lib.parse(args.chaos)
    outage_flag = Path(os.environ.get("TEMP", ".")) / f"vitalmesh-sim-outage-{uuid.uuid4().hex[:6]}"

    env = {
        **os.environ,
        "VITALMESH_LLM_MODE": "simulated",
        "VITALMESH_ALLOW_SIMULATED_LLM": "benchmark-only",
        "VITALMESH_SIM_LLM_MS": str(args.sim_ms),
        "VITALMESH_PG_DSN": PG_DSN,
        "VITALMESH_RUN_DEADLINE_S": str(args.deadline_s),
        "VITALMESH_LOG_LEVEL": args.log_level,
        "VITALMESH_SIM_LLM_OUTAGE_FILE": str(outage_flag),
    }
    procs: dict[str, list[subprocess.Popen[bytes]]] = {}
    logdir = Path(os.environ.get("TEMP", "."))

    def spawn(role: str) -> subprocess.Popen[bytes]:
        n = len(procs.get(role, []))
        out = (logdir / f"bench-agent-{role}-{n}.log").open("w")  # kept for the child's life
        proc = subprocess.Popen(
            [sys.executable, "-m", "vitalmesh_agents", role],
            env=env,
            stdout=out,
            stderr=subprocess.STDOUT,
        )
        procs.setdefault(role, []).append(proc)
        return proc

    # a crash of this script must not leave dozens of agent processes running
    atexit.register(lambda: [kill_tree(p.pid) for g in procs.values() for p in g])
    for role, n in scale.items():
        for _ in range(n):
            spawn(role)
    try:
        wait_ready(settings, scale)
    except SystemExit:
        for group in procs.values():
            for p in group:
                p.kill()
        raise
    time.sleep(8)  # let the rebalances settle
    print(f"agents ready: {scale}  simulated model {args.sim_ms:.0f} ms per call")

    serde = SchemaRegistrySerde(settings)
    publisher = KafkaPublisher(settings, client_id="load-agents")
    collector = Consumer(
        {
            "bootstrap.servers": settings.bootstrap_servers,
            "group.id": f"load-agents-{uuid.uuid4().hex[:6]}",
            "auto.offset.reset": "earliest",
            "enable.auto.commit": False,
        }
    )
    collector.subscribe([ADVICE_COMPLETED, *FINAL_TYPES])
    ps = {role: [psutil.Process(p.pid) for p in group] for role, group in procs.items()}
    cpu0 = {role: sum(tree_cpu(p) for p in group) for role, group in ps.items()}

    # unique per run: the release gate keeps a checkpoint per recommendation in Postgres and
    # treats an id it has seen as a redelivery (publishes nothing), so reused ids would hide results
    prefix = f"ag-{uuid.uuid4().hex[:6]}-"
    sent: dict[str, float] = {}
    finals: dict[str, list[tuple[float, str]]] = {}
    advice: Counter[str] = Counter()
    partial_released = 0
    seen_events: set[str] = set()
    rng = random.Random(11)

    def drain(timeout: float) -> None:
        nonlocal partial_released
        msg = collector.poll(timeout)
        if msg is None or msg.error():
            return
        try:
            meta, payload = serde.deserialize_event(msg.topic(), msg.value())
        except Exception:
            return
        if meta.event_id in seen_events or not meta.correlation_id.startswith(prefix):
            return
        seen_events.add(meta.event_id)
        if msg.topic() == ADVICE_COMPLETED:
            advice[ADVICE_NAMES.get(payload.status, "unknown")] += 1
        else:
            finals.setdefault(meta.correlation_id, []).append(
                (time.monotonic(), FINAL_TYPES[msg.topic()])
            )
            if msg.topic() == RECOMMENDATIONS_RELEASED and payload.partial:
                partial_released += 1

    def do_kill(role: str) -> str:
        for proc in procs.get(role, []):
            if proc.poll() is None:
                try:
                    root = psutil.Process(proc.pid)
                    for child in root.children(recursive=True):
                        child.kill()
                    root.kill()
                except psutil.Error:
                    proc.kill()
                return f"killed pid {proc.pid} ({role})"
        return f"no live process for {role}"

    def do_respawn(role: str) -> str:
        return f"started pid {spawn(role).pid} ({role})"

    def outage_on(_: str) -> str:
        outage_flag.write_text("down")
        return "simulated model down"

    def outage_off(_: str) -> str:
        outage_flag.unlink(missing_ok=True)
        return "simulated model back"

    t0 = time.monotonic()
    chaos = chaos_lib.Chaos(
        chaos_actions,
        {
            "kill": do_kill,
            "respawn": do_respawn,
            "broker": chaos_lib.restart_bench_broker,
            "outage_on": outage_on,
            "outage_off": outage_off,
        },
        t0,
    )
    chaos.start()
    for i in range(args.questions):
        corr = f"{prefix}{i:06d}"
        user = users[i % len(users)]
        meta = EventMeta(
            correlation_id=corr,
            user_id=user,
            household_id="h-bench",
            type="question.asked",
            occurred_at=datetime.now(UTC),
            trace_id=corr,
        )
        publisher.publish(
            QUESTIONS_ASKED,
            user,
            serde.serialize_event(
                QUESTIONS_ASKED,
                meta,
                payloads_pb2.QuestionAsked(question=rng.choice(QUESTIONS), session_id="bench"),
            ),
        )
        sent[corr] = time.monotonic()
        if args.rate:
            ahead = t0 + (i + 1) / args.rate - time.monotonic()
            if ahead > 0:
                drain(ahead)
    publisher.flush(60)
    t_published = time.monotonic()
    print(f"published {args.questions} questions in {t_published - t0:.1f} s")

    stalled = False
    last_progress, last_count = time.monotonic(), 0
    while len(finals) < args.questions and time.monotonic() - t0 < args.timeout:
        drain(0.5)
        if len(finals) > last_count:
            last_progress, last_count = time.monotonic(), len(finals)
        elif time.monotonic() - last_progress > args.stall_seconds:
            stalled = True
            print(
                f"STALLED: no question finished for {args.stall_seconds:.0f} s ({len(finals)} done)"
            )
            break
    chaos.stop_event.set()
    outage_flag.unlink(missing_ok=True)
    for _ in range(40):  # a short tail to pick up late duplicate or extra final events
        drain(0.1)

    cpu_s = {role: sum(tree_cpu(p) for p in group) - cpu0[role] for role, group in ps.items()}
    for group in procs.values():
        for p in group:
            p.terminate()
    for group in procs.values():
        for p in group:
            try:
                p.wait(20)
            except subprocess.TimeoutExpired:
                p.kill()
    collector.close()

    first_final = {c: min(t for t, _ in v) for c, v in finals.items()}
    lat_ms = [(first_final[c] - sent[c]) * 1000 for c in first_final if c in sent]
    outcomes = Counter(v[0][1] for v in finals.values())
    extra_finals = sum(len(v) - 1 for v in finals.values())
    admin = AdminClient({"bootstrap.servers": settings.bootstrap_servers})
    dlq_topics = [t for t in admin.list_topics(timeout=15).topics if t.endswith(".dlq")]
    dlq = sum(end_offsets(settings, t) for t in dlq_topics)
    span = max(first_final.values(), default=t0) - t0
    result: dict[str, Any] = {
        "questions": args.questions,
        "users": len(users),
        "processes_per_role": scale,
        "total_processes": sum(scale.values()),
        "sim_model_ms_per_call": args.sim_ms,
        "rate_target": args.rate,
        "finished": len(finals),
        "outcomes": dict(outcomes),
        "released_partial": partial_released,
        "advice_status": dict(advice),
        "extra_final_events": extra_finals,
        "dlq_messages": dlq,
        "seconds_first_publish_to_last_final": round(span, 1),
        "questions_per_second": round(len(finals) / span, 2) if span else None,
        "latency_ms": {
            "p50": pct(lat_ms, 0.5),
            "p95": pct(lat_ms, 0.95),
            "p99": pct(lat_ms, 0.99),
            "max": max(lat_ms, default=None),
            "mean": round(statistics.mean(lat_ms)) if lat_ms else None,
        },
        "chaos": args.chaos,
        "chaos_actions_run": chaos.done,
        "cpu_seconds_by_role": {k: round(v, 1) for k, v in cpu_s.items()},
        "check": {
            "every_question_finished_exactly_once": len(finals) == args.questions
            and extra_finals == 0,
            "no_dead_lettered_messages": dlq == 0,
            "no_failed_specialist": args.expect_degraded or advice.get("failed", 0) == 0,
            "not_stalled": not stalled,
        },
    }
    print(json.dumps(result, indent=1))
    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=1), encoding="utf-8")
    if not all(result["check"].values()):
        raise SystemExit("a correctness check failed (see above)")


if __name__ == "__main__":
    main()
