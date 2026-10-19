from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from vitalmesh.events.v1 import common_pb2, payloads_pb2
from vitalmesh_common import codec
from vitalmesh_common.envelope import EventMeta, Tier
from vitalmesh_common.failure import EnvelopeError
from vitalmesh_common.testing import PlainSerde, RecordingPublisher
from vitalmesh_common.topics import RECOMMENDATIONS_RELEASED, SAFETY_BLOCKED
from vitalmesh_t0.handler import UnknownUser, derived_id, make_handler
from vitalmesh_t0.profiles import InMemoryProfileStore, ProfileRecord
from vitalmesh_wellness.guardian import RULES_VERSION, Guardian, Rule, Violation
from vitalmesh_wellness.synthetic import synthetic_user

NOW = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)


def state_event(
    user_id: str, event_id: str = "evt-1", type_: str = "health.state.updated"
) -> EventMeta:
    return EventMeta(
        event_id=event_id,
        correlation_id="corr-1",
        user_id=user_id,
        household_id="h-1",
        type=type_,
        occurred_at=NOW,
        trace_id="trace-1",
    )


def seeded(index: int = 1) -> tuple[InMemoryProfileStore, str]:
    profile, sleep = synthetic_user(7, index)
    store = InMemoryProfileStore()
    store.put(ProfileRecord(profile=profile, recent_sleep_hours=tuple(sleep)))
    return store, profile.user_id


def run(
    store: InMemoryProfileStore, uid: str, guardian: Guardian | None = None
) -> RecordingPublisher:
    pub = RecordingPublisher()
    handler = make_handler(store, PlainSerde(), pub, guardian=guardian, clock=lambda: NOW)
    handler(state_event(uid), payloads_pb2.HealthStateUpdated())
    return pub


def decode(pub: RecordingPublisher, i: int = 0) -> tuple[EventMeta, Any]:
    return codec.decode(pub.sent[i].value)


def test_cleared_recommendation_goes_to_the_released_topic_only() -> None:
    store, uid = seeded()
    pub = run(store, uid)
    assert [p.topic for p in pub.sent] == [RECOMMENDATIONS_RELEASED]
    meta, payload = decode(pub)
    assert meta.type == "recommendation.released"
    assert meta.tier is Tier.T0
    assert meta.rules_version == RULES_VERSION
    assert meta.causation_id == "evt-1"
    assert meta.correlation_id == "corr-1" and meta.trace_id == "trace-1"
    assert pub.sent[0].key == uid
    assert len(payload.claims) == 3
    assert all(c.provenance == common_pb2.PROVENANCE_COMPUTED for c in payload.claims)
    assert pub.log == ["publish", "flush"]  # durable before the offset commits


def test_blocked_recommendation_never_reaches_the_released_topic() -> None:
    def always_block(_: Any) -> list[Violation]:
        return [Violation(rule="test_block", message="blocked on purpose")]

    store, uid = seeded()
    rules: tuple[Rule, ...] = (always_block,)
    pub = run(store, uid, Guardian(rules))
    assert [p.topic for p in pub.sent] == [SAFETY_BLOCKED]
    meta, payload = decode(pub)
    assert meta.type == "recommendation.blocked"
    assert payload.violations[0].rule == "test_block"


def test_a_crashing_guardian_rule_blocks_instead_of_releasing() -> None:
    def boom(_: Any) -> list[Violation]:
        raise RuntimeError("bug")

    store, uid = seeded()
    pub = run(store, uid, Guardian((boom,)))
    assert [p.topic for p in pub.sent] == [SAFETY_BLOCKED]


def test_redelivery_publishes_the_same_ids() -> None:
    store, uid = seeded()
    a, b = run(store, uid), run(store, uid)
    (ma, pa), (mb, pb) = decode(a), decode(b)
    assert ma.event_id == mb.event_id == derived_id("evt-1", "recommendation.released")
    assert pa.recommendation_id == pb.recommendation_id == derived_id("evt-1", "recommendation")


def test_unknown_user_is_retryable_not_silently_dropped() -> None:
    pub = RecordingPublisher()
    handler = make_handler(InMemoryProfileStore(), PlainSerde(), pub, clock=lambda: NOW)
    with pytest.raises(UnknownUser):
        handler(state_event("nobody"), payloads_pb2.HealthStateUpdated())
    assert not pub.sent


def test_implausible_stored_data_is_not_retryable_and_publishes_nothing() -> None:
    store, uid = seeded()
    record = store.get(uid)
    assert record is not None
    bad = record.profile.model_copy(update={"weight_kg": 5.0})
    store.put(ProfileRecord(profile=bad, recent_sleep_hours=record.recent_sleep_hours))
    pub = RecordingPublisher()
    handler = make_handler(store, PlainSerde(), pub, clock=lambda: NOW)
    with pytest.raises(EnvelopeError):
        handler(state_event(uid), payloads_pb2.HealthStateUpdated())
    assert not pub.sent


def test_other_event_types_are_ignored() -> None:
    store, uid = seeded()
    pub = RecordingPublisher()
    handler = make_handler(store, PlainSerde(), pub, clock=lambda: NOW)
    handler(state_event(uid, type_="measurement.recorded"), payloads_pb2.MeasurementRecorded())
    assert not pub.sent


def test_failed_flush_propagates_so_the_offset_is_not_committed() -> None:
    store, uid = seeded()
    pub = RecordingPublisher(fail_flush=True)
    handler = make_handler(store, PlainSerde(), pub, clock=lambda: NOW)
    with pytest.raises(RuntimeError):
        handler(state_event(uid), payloads_pb2.HealthStateUpdated())


@pytest.mark.parametrize("index", range(25))
def test_every_synthetic_user_gets_a_released_recommendation(index: int) -> None:
    store, uid = seeded(index)
    assert [p.topic for p in run(store, uid).sent] == [RECOMMENDATIONS_RELEASED]
