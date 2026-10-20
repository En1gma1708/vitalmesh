"""Gateway to released recommendation on the real stack:

POST /v1/measurements -> user.events -> state processor -> user.state -> T0 service
-> recommendations.released, with the correlation id the API returned.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
import redis
from fastapi.testclient import TestClient
from vitalmesh_api.app import create_app
from vitalmesh_common.kafka import KafkaPublisher, KafkaSettings, SchemaRegistrySerde, ensure_topics
from vitalmesh_common.kafka_testing import fetch_through, running_consumers
from vitalmesh_common.topics import RECOMMENDATIONS_RELEASED, USER_EVENTS, USER_STATE
from vitalmesh_processor.handler import make_handler as make_state_handler
from vitalmesh_processor.state import RedisStateStore
from vitalmesh_t0.handler import make_handler as make_t0_handler
from vitalmesh_t0.profiles import ProfileRecord, RedisProfileStore
from vitalmesh_wellness.synthetic import synthetic_user

pytestmark = pytest.mark.integration


def test_api_measurement_reaches_a_released_recommendation(
    kafka_settings: KafkaSettings, redis_client: redis.Redis
) -> None:
    ensure_topics(kafka_settings)
    serde = SchemaRegistrySerde(kafka_settings)
    publisher = KafkaPublisher(kafka_settings, client_id="api-it")
    prefix = f"it-{uuid.uuid4().hex[:8]}"
    profiles = RedisProfileStore(redis_client, prefix=f"{prefix}:profile")
    state = RedisStateStore(redis_client, prefix=f"{prefix}:state")
    profile, sleep = synthetic_user(5, 2)
    uid = f"{profile.user_id}-{prefix}"
    profiles.put(
        ProfileRecord(
            profile=profile.model_copy(update={"user_id": uid}), recent_sleep_hours=tuple(sleep)
        )
    )
    client = TestClient(create_app(profiles, serde, publisher))

    def decode(data: bytes) -> Any:
        return serde.deserialize_event(RECOMMENDATIONS_RELEASED, data)

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
        r = client.post(
            "/v1/measurements",
            json={
                "user_id": uid,
                "metric_type": "body_weight",
                "value": profile.weight_kg,
                "unit": "kg",
                "measured_at": datetime.now(UTC).isoformat(),
            },
        )
        assert r.status_code == 202, r.text
        corr = r.json()["correlation_id"]
        out = fetch_through(
            kafka_settings,
            RECOMMENDATIONS_RELEASED,
            decode,
            lambda item: item[0].user_id == uid,
            timeout=60,
        )

    meta, rec = next(item for item in out if item[0].user_id == uid)
    assert meta.correlation_id == corr  # the id the API returned follows the event end to end
    assert {c.metric for c in rec.claims} == {
        "daily_energy_target",
        "target_hr_zone",
        "sleep_target",
    }

    # the synchronous path on the same profile gives the same numbers as the Kafka path
    sync = client.get(f"/v1/users/{uid}/recommendation").json()
    kafka_values = sorted(c.quantity.value for c in rec.claims)
    assert sorted(i["value"] for i in sync["items"]) == pytest.approx(kafka_values)


def test_lab_through_kafka_is_stored_and_listed(
    kafka_settings: KafkaSettings, redis_client: redis.Redis
) -> None:
    from vitalmesh_common.kafka_testing import wait_for
    from vitalmesh_processor.labstore import RedisLabStore

    ensure_topics(kafka_settings)
    serde = SchemaRegistrySerde(kafka_settings)
    publisher = KafkaPublisher(kafka_settings, client_id="api-lab-it")
    prefix = f"it-{uuid.uuid4().hex[:8]}"
    labs = RedisLabStore(redis_client, prefix=f"{prefix}:labs")
    state = RedisStateStore(redis_client, prefix=f"{prefix}:state")
    uid = f"lab-user-{prefix}"
    client = TestClient(
        create_app(
            RedisProfileStore(redis_client, prefix=f"{prefix}:profile"), serde, publisher, labs=labs
        )
    )
    with running_consumers(
        kafka_settings,
        serde,
        publisher,
        redis_client,
        USER_EVENTS,
        make_state_handler(state, serde, publisher, labs=labs),
    ):
        r = client.post(
            "/v1/labs",
            json={
                "user_id": uid,
                "marker_name": "ALT",
                "value": 64,
                "unit": "U/L",
                "reference_range": "< 50",
                "collected_at": datetime.now(UTC).isoformat(),
            },
        )
        assert r.status_code == 202, r.text
        assert wait_for(lambda: bool(labs.list(uid)), timeout=30)
    [item] = client.get(f"/v1/users/{uid}/labs").json()
    assert (item["name"], item["value"], item["unit"], item["status"]) == (
        "ALT",
        64,
        "U/L",
        "high",
    )
