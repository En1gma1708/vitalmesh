"""Demo: one measurement travels API -> Kafka -> state processor -> Kafka -> T0 service -> Kafka,
and the Guardian-cleared recommendation comes out, traced by one correlation id.

Needs the stack and three processes running (see docs/DEMO_SCRIPT.md):
    uv run python scripts/seed_demo.py demo-1
    uv run python -m vitalmesh_processor      (terminal 1)
    uv run python -m vitalmesh_t0             (terminal 2)
    uv run python -m vitalmesh_api            (terminal 3)
    uv run python scripts/demo_flow.py demo-1
"""

from __future__ import annotations

import sys
import time
from datetime import UTC, datetime

import httpx
from vitalmesh.events.v1 import common_pb2
from vitalmesh_common.kafka import KafkaSettings, SchemaRegistrySerde
from vitalmesh_common.kafka_testing import fetch_through
from vitalmesh_common.topics import RECOMMENDATIONS_RELEASED, SAFETY_BLOCKED

API = "http://127.0.0.1:8000"


def main() -> None:
    user = sys.argv[1] if len(sys.argv) > 1 else "demo-1"
    settings = KafkaSettings.from_env()
    serde = SchemaRegistrySerde(settings)

    started = time.perf_counter()
    r = httpx.post(
        f"{API}/v1/measurements",
        json={
            "user_id": user,
            "metric_type": "body_weight",
            "value": 66.4,
            "unit": "kg",
            "measured_at": datetime.now(UTC).isoformat(),
        },
        timeout=10,
    )
    r.raise_for_status()
    body = r.json()
    corr = body["correlation_id"]
    print(
        f"1. API accepted the measurement (HTTP {r.status_code}) after the broker acknowledged it"
    )
    print(f"   event_id={body['event_id']}\n   correlation_id={corr}")

    def decode(topic: str):  # type: ignore[no-untyped-def]
        return lambda data: serde.deserialize_event(topic, data)

    for topic in (RECOMMENDATIONS_RELEASED, SAFETY_BLOCKED):
        items = fetch_through(
            settings, topic, decode(topic), lambda it: it[0].correlation_id == corr, timeout=12
        )
        hit = next((it for it in items if it[0].correlation_id == corr), None)
        if hit is None:
            continue
        meta, rec = hit
        elapsed = (time.perf_counter() - started) * 1000
        print(
            f"\n2. Result on topic '{topic}' (wall-clock {elapsed:.0f} ms incl. starting a reader;"
            " not the latency metric)"
        )
        print(
            f"   type={meta.type} tier={meta.tier.value if meta.tier else None} "
            f"rules_version={meta.rules_version}"
        )
        print(f"   causation_id={meta.causation_id} (the state event that caused it)")
        if topic == RECOMMENDATIONS_RELEASED:
            print("\n3. The recommendation: every number is computed by code")
            for c in rec.claims:
                print(f"   - {c.text}")
                print(
                    f"       value={c.quantity.value:g} {c.quantity.unit}  "
                    f"provenance={common_pb2.Provenance.Name(c.provenance)}  source={c.source}"
                )
        return
    print(
        "\nNo result found: are the processor, T0 service and API running, and is the user seeded?"
    )


if __name__ == "__main__":
    main()
