"""One line per question: what every helper really did, and why a refusal happened (D100).

    uv run python scripts/trail_summary.py [--since 2026-11-10T00:00:00+00:00]
        [--user-contains u-] [--json out.json]

Reads the agent event topics of the LOCAL dev stack from the beginning with a throwaway consumer
group (it commits nothing, so it cannot disturb the agents) and groups the events by question. For
each question it shows the helpers' statuses (answered = the model wrote it; fallback = built from
the tools' results; FAILED = nothing), whether the bundle was partial, and the final outcome. For
a refusal it prints the Guardian's rule names and messages, which are carried inside the event.
No question text beyond the first 60 characters is printed.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from confluent_kafka import Consumer
from vitalmesh.events.v1 import common_pb2, payloads_pb2
from vitalmesh_common.kafka import KafkaSettings, SchemaRegistrySerde
from vitalmesh_common.topics import (
    ADVICE_BUNDLES,
    ADVICE_COMPLETED,
    DRAFTS_CLEARED,
    QUESTIONS_ASKED,
    RECOMMENDATIONS_DRAFTED,
    RECOMMENDATIONS_RELEASED,
    REVIEWS_REQUESTED,
    RUNS_PLANNED,
    SAFETY_BLOCKED,
)

TOPICS = [
    QUESTIONS_ASKED, RUNS_PLANNED, ADVICE_COMPLETED, ADVICE_BUNDLES, RECOMMENDATIONS_DRAFTED,
    DRAFTS_CLEARED, REVIEWS_REQUESTED, RECOMMENDATIONS_RELEASED, SAFETY_BLOCKED,
]  # fmt: skip
SPEC = {
    common_pb2.SPECIALIST_NUTRITION: "nutrition",
    common_pb2.SPECIALIST_MOVEMENT_RECOVERY: "movement",
    common_pb2.SPECIALIST_CONDITION_MEDICATION: "condition",
}
STATUS = {
    payloads_pb2.ADVICE_ANSWERED: "answered",
    payloads_pb2.ADVICE_FALLBACK: "fallback",
    payloads_pb2.ADVICE_DECLINED: "declined",
    payloads_pb2.ADVICE_FAILED: "FAILED",
}


def read_events(settings: KafkaSettings, quiet_s: float = 6.0) -> list[tuple[Any, Any]]:
    serde = SchemaRegistrySerde(settings)
    consumer = Consumer({
        "bootstrap.servers": settings.bootstrap_servers,
        "group.id": f"trail-summary-{uuid.uuid4()}",
        "auto.offset.reset": "earliest",
        "enable.auto.commit": False,
    })  # fmt: skip
    consumer.subscribe(TOPICS)
    out: list[tuple[Any, Any]] = []
    seen: set[str] = set()
    last = time.monotonic()
    try:
        while time.monotonic() - last < quiet_s:
            msg = consumer.poll(0.5)
            if msg is None or msg.error():
                continue
            last = time.monotonic()
            try:
                meta, payload = serde.deserialize_event(msg.topic(), msg.value())
            except Exception:
                continue
            if meta.event_id not in seen:
                seen.add(meta.event_id)
                out.append((meta, payload))
    finally:
        consumer.close()
    return out


def summarise(
    events: list[tuple[Any, Any]], since: datetime | None, user_contains: str
) -> list[dict[str, Any]]:
    runs: dict[str, dict[str, Any]] = defaultdict(lambda: {"parts": {}, "final": "no final"})
    for meta, p in sorted(events, key=lambda e: e[0].occurred_at):
        r = runs[meta.correlation_id]
        r.setdefault("user", meta.user_id)
        if meta.type == "question.asked":
            r["question"] = p.question
            r["asked_at"] = meta.occurred_at
        elif meta.type == "advice.completed":
            part = f"{SPEC[p.specialist]}:{STATUS[p.status]}"
            if p.failure_code:
                part += f"({p.failure_code})"
            r["parts"][SPEC[p.specialist]] = f"{part} claims={len(p.claims)} calls={p.model_calls}"
        elif meta.type == "advice.bundle.ready":
            r["bundle"] = f"{p.reason}, partial={p.partial}"
        elif meta.type == "recommendation.released":
            r["final"] = "released" + (" (partial)" if p.partial else "")
        elif meta.type == "recommendation.blocked":
            r["final"] = "BLOCKED"
            r["rules"] = [f"{v.rule}: {v.message}"[:140] for v in p.violations]
        elif meta.type == "review.requested":
            r["final"] = "held for a clinician"
    rows = []
    for cid, r in runs.items():
        if "question" not in r:
            continue
        if since and r["asked_at"] < since:
            continue
        if user_contains and user_contains not in r["user"]:
            continue
        rows.append({"correlation_id": cid, **r, "asked_at": r["asked_at"].isoformat()})
    return sorted(rows, key=lambda r: r["asked_at"])


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--since", default="")
    ap.add_argument("--user-contains", default="")
    ap.add_argument("--json", default="")
    args = ap.parse_args()
    since = datetime.fromisoformat(args.since) if args.since else None
    if since and since.tzinfo is None:
        since = since.replace(tzinfo=UTC)
    rows = summarise(read_events(KafkaSettings.from_env()), since, args.user_contains)
    for i, r in enumerate(rows, start=1):
        parts = "  ".join(
            r["parts"].get(k, f"{k}:-") for k in ("nutrition", "movement", "condition")
        )
        question = r["question"][:46]
        print(f"{i:>2} {r['final']:<22} {question!r}")
        print(f"     {parts}")
        print(f"     bundle: {r.get('bundle', '-')}")
        for rule in r.get("rules", []):
            print(f"     rule: {rule}")
    print(f"\n{len(rows)} questions")
    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=1, default=str), encoding="utf-8")


if __name__ == "__main__":
    main()
