"""Demo: one question travels the whole multi-agent graph over Kafka and the trail is printed.

Needs the stack, a seeded user and the agents running, e.g. in one terminal:
    uv run python scripts/seed_demo.py demo-1
    VITALMESH_RUN_DEADLINE_S=240 uv run python -m vitalmesh_agents \
        triage nutrition movement condition aggregator synthesizer guardian checker release
then:
    uv run python scripts/demo_t3.py demo-1 "What should I eat and how should I train this week?"

A high-risk run stops at ``review.requested``; answer it with
    uv run python scripts/review.py <recommendation_id> approve|reject
"""

from __future__ import annotations

import sys
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from confluent_kafka import Consumer
from vitalmesh.events.v1 import common_pb2, payloads_pb2
from vitalmesh_common.envelope import EventMeta
from vitalmesh_common.kafka import KafkaPublisher, KafkaSettings, SchemaRegistrySerde
from vitalmesh_common.topics import (
    ADVICE_BUNDLES,
    ADVICE_COMPLETED,
    DRAFTS_CLEARED,
    QUESTIONS_ASKED,
    RECOMMENDATIONS_DRAFTED,
    RECOMMENDATIONS_RELEASED,
    REVIEWS_COMPLETED,
    REVIEWS_DECIDED,
    REVIEWS_REQUESTED,
    RUNS_PLANNED,
    SAFETY_BLOCKED,
)

TOPICS = [
    QUESTIONS_ASKED,
    RUNS_PLANNED,
    ADVICE_COMPLETED,
    ADVICE_BUNDLES,
    RECOMMENDATIONS_DRAFTED,
    DRAFTS_CLEARED,
    REVIEWS_COMPLETED,
    REVIEWS_REQUESTED,
    REVIEWS_DECIDED,
    RECOMMENDATIONS_RELEASED,
    SAFETY_BLOCKED,
]
FINAL = {"recommendation.released", "recommendation.blocked", "review.requested"}
SPEC = {
    common_pb2.SPECIALIST_NUTRITION: "nutrition",
    common_pb2.SPECIALIST_MOVEMENT_RECOVERY: "movement_recovery",
    common_pb2.SPECIALIST_CONDITION_MEDICATION: "condition_medication",
}
RISK = {common_pb2.RISK_LOW: "low", common_pb2.RISK_MEDIUM: "medium", common_pb2.RISK_HIGH: "high"}
STATUS = {
    payloads_pb2.ADVICE_ANSWERED: "answered",
    payloads_pb2.ADVICE_FALLBACK: "fallback",
    payloads_pb2.ADVICE_DECLINED: "declined",
    payloads_pb2.ADVICE_FAILED: "FAILED",
}
VERDICT = {
    payloads_pb2.REVIEW_APPROVED: "approve",
    payloads_pb2.REVIEW_REVISE: "revise",
    payloads_pb2.REVIEW_REJECTED: "reject",
}


def describe(meta: EventMeta, p: Any) -> str:
    t = meta.type
    if t == "question.asked":
        return f"question: {p.question!r}"
    if t == "run.planned":
        who = ", ".join(SPEC[s] for s in p.specialists)
        tier = meta.tier.value if meta.tier else "?"
        return (
            f"tier {tier}, risk {RISK[p.risk]}, specialists [{who}], "
            f"max {p.max_model_calls} model calls each, rules {list(p.rule_ids)}"
        )
    if t == "advice.completed":
        return (
            f"{SPEC[p.specialist]}: {STATUS[p.status]}, {p.model_calls} model calls, "
            f"{len(p.claims)} claims, {len(p.citations)} citations"
            + (f", failure {p.failure_code}" if p.failure_code else "")
            + f"\n       text: {p.text}"
        )
    if t == "advice.bundle.ready":
        miss = ", ".join(SPEC[s] for s in p.missing) or "none"
        return f"{p.reason}, partial={p.partial}, missing: {miss}, {len(p.parts)} parts"
    if t == "recommendation.drafted":
        return f"revision {p.revision}, fallback={p.used_fallback}\n       text: {p.text}"
    if t == "safety.cleared":
        return f"Guardian cleared (rules {p.rules_version}), risk {RISK[p.draft.risk]}"
    if t == "review.completed":
        return f"Checker verdict: {VERDICT[p.verdict]} {list(p.problems)}"
    if t == "review.requested":
        return (
            f"HUMAN REVIEW REQUESTED {p.recommendation_id}: reasons {list(p.reasons)}"
            f"\n       briefing: {p.briefing}"
        )
    if t == "review.decided":
        return f"human decision {p.decision} by {p.reviewer_id}"
    if t == "recommendation.released":
        cites = [f"{c.document} {c.locator}" for c in p.citations]
        return (
            f"RELEASED (partial={p.partial}), {len(p.claims)} claims, citations {cites}"
            f"\n       summary: {p.summary}"
        )
    if t == "recommendation.blocked":
        return "BLOCKED: " + "; ".join(f"{v.rule}: {v.message}" for v in p.violations)
    return t


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:  # model text can hold characters a Windows console cannot print
            reconfigure(encoding="utf-8", errors="replace")
    user = sys.argv[1] if len(sys.argv) > 1 else "demo-1"
    text = sys.argv[2] if len(sys.argv) > 2 else "What should I eat and how should I train?"
    timeout = float(sys.argv[3]) if len(sys.argv) > 3 else 300.0
    settings = KafkaSettings.from_env()
    serde = SchemaRegistrySerde(settings)
    publisher = KafkaPublisher(settings, client_id="demo-t3")

    replay = sys.argv[4] if len(sys.argv) > 4 else None  # a correlation id to print, no new run
    corr = replay or f"demo-{uuid.uuid4().hex[:8]}"
    meta = EventMeta(
        correlation_id=corr,
        user_id=user,
        household_id="h-demo",
        type="question.asked",
        occurred_at=datetime.now(UTC),
        trace_id=corr,
    )
    started = time.perf_counter()
    if replay is None:
        publisher.publish(
            QUESTIONS_ASKED,
            user,
            serde.serialize_event(
                QUESTIONS_ASKED, meta, payloads_pb2.QuestionAsked(question=text, session_id="demo")
            ),
        )
        publisher.flush()
        print(f"asked as {user}, correlation_id={corr}\n")
    else:
        print(f"replaying correlation_id={corr}\n")

    consumer = Consumer(
        {
            "bootstrap.servers": settings.bootstrap_servers,
            "group.id": f"demo-t3-{uuid.uuid4()}",
            "auto.offset.reset": "earliest",
            "enable.auto.commit": False,
        }
    )
    consumer.subscribe(TOPICS)
    seen: dict[str, tuple[float, EventMeta, Any]] = {}
    final_at: float | None = None
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            msg = consumer.poll(0.5)
            if final_at is not None and time.monotonic() - final_at > 3:
                break  # drained: nothing more arrived after the final event
            if msg is None or msg.error():
                continue
            try:
                m, p = serde.deserialize_event(msg.topic(), msg.value())
            except Exception:
                continue
            if m.correlation_id != corr or m.event_id in seen:
                continue
            seen[m.event_id] = (time.perf_counter() - started, m, p)
            if m.type in FINAL:
                final_at = time.monotonic()
    finally:
        consumer.close()

    for elapsed, m, p in sorted(seen.values(), key=lambda it: it[1].occurred_at):
        print(f"[{elapsed:6.1f}s] {m.type:24} {describe(m, p)}")
    if final_at is None:
        print("\nno final event before the timeout: are the agents running?")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
