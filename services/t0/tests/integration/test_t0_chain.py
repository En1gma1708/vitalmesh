"""The T0 path end to end on the real stack:

measurement -> user.events -> state processor -> user.state -> T0 service
-> recommendations.released

Latency reported here is service-side (time the event was produced until the T0 service stamped
its output), measured on one machine against a single-node local Kafka, after a warm-up event so
consumer start-up and partition assignment are excluded. It is not a scale number.
"""

from __future__ import annotations

import statistics
import time
import uuid
from datetime import UTC, datetime

import pytest
import redis
from vitalmesh.events.v1 import common_pb2, payloads_pb2
from vitalmesh_common.envelope import EventMeta, Tier
from vitalmesh_common.kafka import (
    KafkaPublisher,
    KafkaSettings,
    SchemaRegistrySerde,
    ensure_topics,
)
from vitalmesh_common.kafka_testing import fetch_through, running_consumers
from vitalmesh_common.topics import RECOMMENDATIONS_RELEASED, USER_EVENTS, USER_STATE
from vitalmesh_processor.handler import make_handler as make_state_handler
from vitalmesh_processor.state import RedisStateStore
from vitalmesh_t0.handler import make_handler as make_t0_handler
from vitalmesh_t0.profiles import ProfileRecord, RedisProfileStore
from vitalmesh_wellness.guardian import RULES_VERSION
from vitalmesh_wellness.synthetic import synthetic_user

pytestmark = pytest.mark.integration

BATCH = 50


@pytest.fixture(scope="module")
def serde(kafka_settings: KafkaSettings) -> SchemaRegistrySerde:
    ensure_topics(kafka_settings)
    return SchemaRegistrySerde(kafka_settings)


@pytest.fixture()
def publisher(kafka_settings: KafkaSettings) -> KafkaPublisher:
    return KafkaPublisher(kafka_settings, client_id="t0-it")


def seed_user(profiles: RedisProfileStore, index: int, run: str) -> tuple[str, float]:
    profile, sleep = synthetic_user(42, index)
    uid = f"{profile.user_id}-{run}"
    profile = profile.model_copy(update={"user_id": uid})
    profiles.put(ProfileRecord(profile=profile, recent_sleep_hours=tuple(sleep)))
    return uid, profile.weight_kg


def weight_event(
    uid: str, kg: float, corr: str
) -> tuple[EventMeta, payloads_pb2.MeasurementRecorded]:
    meta = EventMeta(
        correlation_id=corr,
        user_id=uid,
        household_id="h-it",
        type="measurement.recorded",
        occurred_at=datetime.now(UTC),
        trace_id="trace-t0",
    )
    m = payloads_pb2.MeasurementRecorded(metric_type="body_weight")
    m.value.value, m.value.unit = kg, "kg"
    m.measured_at.FromDatetime(datetime.now(UTC).replace(tzinfo=None))
    return meta, m


def test_t0_path_is_cleared_traceable_and_fast(
    kafka_settings: KafkaSettings,
    serde: SchemaRegistrySerde,
    publisher: KafkaPublisher,
    redis_client: redis.Redis,
) -> None:
    run = uuid.uuid4().hex[:8]
    prefix = f"it-{run}"
    profiles = RedisProfileStore(redis_client, prefix=f"{prefix}:profile")
    state = RedisStateStore(redis_client, prefix=f"{prefix}:state")
    users: dict[str, float] = {}
    for i in range(BATCH + 1):
        seeded_uid, kg = seed_user(profiles, i, run)
        users[seeded_uid] = kg
    uids = list(users)
    warm_uid, batch_uids = uids[0], uids[1:]

    def decode(data: bytes):  # type: ignore[no-untyped-def]
        return serde.deserialize_event(RECOMMENDATIONS_RELEASED, data)

    def mine(uid_set: set[str]):  # type: ignore[no-untyped-def]
        return lambda item: item[0].user_id in uid_set

    with (
        running_consumers(
            kafka_settings,
            serde,
            publisher,
            redis_client,
            USER_EVENTS,
            make_state_handler(state, serde, publisher),
        ),
        running_consumers(
            kafka_settings,
            serde,
            publisher,
            redis_client,
            USER_STATE,
            make_t0_handler(profiles, serde, publisher),
        ),
    ):
        # Warm-up: one event through the whole chain so assignment and connections are excluded.
        meta, m = weight_event(warm_uid, users[warm_uid], "corr-warm")
        publisher.publish(USER_EVENTS, warm_uid, serde.serialize_event(USER_EVENTS, meta, m))
        publisher.flush()
        fetch_through(kafka_settings, RECOMMENDATIONS_RELEASED, decode, mine({warm_uid}))

        sent_at: dict[str, float] = {}
        corr: dict[str, str] = {}
        for uid in batch_uids:
            corr[uid] = f"corr-{uuid.uuid4()}"
            meta, m = weight_event(uid, users[uid], corr[uid])
            sent_at[uid] = time.time()
            publisher.publish(USER_EVENTS, uid, serde.serialize_event(USER_EVENTS, meta, m))
        publisher.flush()
        remaining = set(batch_uids)

        def got_all(item) -> bool:  # type: ignore[no-untyped-def]
            remaining.discard(item[0].user_id)
            return not remaining

        out = fetch_through(kafka_settings, RECOMMENDATIONS_RELEASED, decode, got_all, timeout=60)

    assert not remaining, f"{len(remaining)} users never got a recommendation"
    latencies = []
    seen: set[str] = set()
    for out_meta, rec in out:
        uid = out_meta.user_id
        if uid not in sent_at or uid in seen:
            continue
        seen.add(uid)
        assert out_meta.type == "recommendation.released"
        assert out_meta.tier is Tier.T0
        assert out_meta.rules_version == RULES_VERSION
        assert out_meta.correlation_id == corr[uid]  # one run, traceable across services
        assert {c.metric for c in rec.claims} == {
            "daily_energy_target",
            "target_hr_zone",
            "sleep_target",
        }
        assert all(c.provenance == common_pb2.PROVENANCE_COMPUTED for c in rec.claims)
        assert all(c.quantity.unit for c in rec.claims)  # every number carries a unit
        latencies.append((out_meta.occurred_at.timestamp() - sent_at[uid]) * 1000)

    assert len(latencies) == BATCH
    latencies.sort()
    p50 = statistics.median(latencies)
    p95 = latencies[int(0.95 * (len(latencies) - 1))]
    print(
        f"\nT0 chain, burst of {BATCH} users, warmed up, single machine: "
        f"p50 {p50:.0f} ms, p95 {p95:.0f} ms, max {latencies[-1]:.0f} ms"
    )
    assert latencies[-1] < 30_000
