from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from vitalmesh_api.app import METRICS, create_app
from vitalmesh_common import codec
from vitalmesh_common.testing import PlainSerde, RecordingPublisher
from vitalmesh_common.topics import USER_EVENTS
from vitalmesh_t0.profiles import InMemoryProfileStore, ProfileRecord
from vitalmesh_wellness.guardian import Guardian, Rule, Violation
from vitalmesh_wellness.synthetic import synthetic_user

NOW = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)


def make(
    guardian: Guardian | None = None, fail_flush: bool = False
) -> tuple[TestClient, RecordingPublisher, str]:
    profile, sleep = synthetic_user(3, 1)
    profiles = InMemoryProfileStore()
    profiles.put(ProfileRecord(profile=profile, recent_sleep_hours=tuple(sleep)))
    pub = RecordingPublisher(fail_flush=fail_flush)
    app = create_app(profiles, PlainSerde(), pub, guardian, clock=lambda: NOW)
    return TestClient(app), pub, profile.user_id


def body(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "user_id": "u-1",
        "metric_type": "body_weight",
        "value": 72.5,
        "unit": "kg",
        "measured_at": (NOW - timedelta(minutes=1)).isoformat(),
    }
    return {**base, **over}


def test_valid_measurement_is_published_keyed_by_user() -> None:
    client, pub, _ = make()
    r = client.post("/v1/measurements", json=body())
    assert r.status_code == 202
    assert len(pub.sent) == 1 and pub.sent[0].topic == USER_EVENTS and pub.sent[0].key == "u-1"
    assert pub.log == ["publish", "flush"]  # acknowledged before 202
    meta, payload = codec.decode(pub.sent[0].value)
    assert meta.type == "measurement.recorded"
    assert meta.event_id == r.json()["event_id"]
    assert meta.correlation_id == r.json()["correlation_id"]
    assert (payload.metric_type, payload.value.value, payload.value.unit) == (
        "body_weight",
        72.5,
        "kg",
    )


def test_client_event_id_makes_the_event_id_deterministic() -> None:
    client, pub, _ = make()
    a = client.post("/v1/measurements", json=body(client_event_id="abc")).json()
    b = client.post("/v1/measurements", json=body(client_event_id="abc")).json()
    c = client.post("/v1/measurements", json=body(client_event_id="abd")).json()
    assert a["event_id"] == b["event_id"] != c["event_id"]
    meta, _ = codec.decode(pub.sent[0].value)
    assert (meta.source, meta.external_id) == ("api", "abc")


@pytest.mark.parametrize(
    "over",
    [
        {"metric_type": "mystery"},
        {"unit": "lb"},
        {"value": 19.9},
        {"value": 400.1},
        {"value": -1},
        {"measured_at": (NOW + timedelta(hours=1)).isoformat()},
        {"measured_at": "2026-10-20T08:00:00"},  # no timezone
        {"user_id": "bad id!"},
        {"user_id": "x" * 65},
        {"extra": 1},
    ],
)
def test_invalid_input_is_rejected_and_nothing_is_published(over: dict[str, Any]) -> None:
    client, pub, _ = make()
    assert client.post("/v1/measurements", json=body(**over)).status_code == 422
    assert pub.sent == []


@pytest.mark.parametrize("bad", ["NaN", "Infinity"])
def test_non_finite_numbers_are_rejected(bad: str) -> None:
    client, pub, _ = make()
    raw = (
        '{"user_id":"u","metric_type":"body_weight","value":' + bad + ',"unit":"kg",'
        '"measured_at":"2026-10-20T08:59:00+00:00"}'
    )
    r = client.post("/v1/measurements", content=raw, headers={"content-type": "application/json"})
    assert r.status_code == 422 and pub.sent == []


def test_every_registered_metric_accepts_its_own_bounds() -> None:
    client, pub, _ = make()
    for name, spec in METRICS.items():
        for v in (spec.low, spec.high):
            r = client.post(
                "/v1/measurements", json=body(metric_type=name, unit=spec.unit, value=v)
            )
            assert r.status_code == 202, (name, v, r.text)
    assert len(pub.sent) == 2 * len(METRICS)


def test_broker_failure_returns_503_without_leaking_detail() -> None:
    client, _, _ = make(fail_flush=True)
    r = client.post("/v1/measurements", json=body())
    assert r.status_code == 503
    assert "broker" not in r.text


def test_recommendation_is_health_only_and_cleared() -> None:
    client, _, uid = make()
    r = client.get(f"/v1/users/{uid}/recommendation")
    assert r.status_code == 200
    out = r.json()
    assert out["items"] and all(i["unit"] for i in out["items"])
    text = r.text.lower()
    for internal in ("tier", "guardian", "rules_version", "t0", "agent", "provenance"):
        assert internal not in text, internal
    assert all(i["basis"] for i in out["items"])


def test_unknown_user_is_404_and_bad_id_is_422() -> None:
    client, _, _ = make()
    assert client.get("/v1/users/nobody/recommendation").status_code == 404
    assert client.get("/v1/users/bad%20id/recommendation").status_code == 422


def test_blocked_recommendation_is_never_returned() -> None:
    def always_block(_rec: Any) -> list[Violation]:
        return [Violation(rule="test", message="blocked for the test")]

    rules: tuple[Rule, ...] = (always_block,)
    client, _, uid = make(guardian=Guardian(rules))
    r = client.get(f"/v1/users/{uid}/recommendation")
    assert r.status_code == 409
    assert "blocked for the test" not in r.text  # the reason stays internal
