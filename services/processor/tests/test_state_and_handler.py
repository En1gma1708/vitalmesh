from __future__ import annotations

import itertools
import math
from datetime import UTC, datetime, timedelta

import pytest
from vitalmesh.events.v1 import payloads_pb2
from vitalmesh_common import codec
from vitalmesh_common.envelope import EventMeta
from vitalmesh_common.failure import EnvelopeError
from vitalmesh_common.testing import PlainSerde, RecordingPublisher
from vitalmesh_common.topics import USER_STATE
from vitalmesh_processor.handler import STATE_EVENT_TYPE, derived_event_id, make_handler
from vitalmesh_processor.state import HealthState, InMemoryStateStore, apply_measurement

T = datetime(2026, 10, 20, 8, 0, tzinfo=UTC)
NOW = T + timedelta(hours=1)


def meas(offset_min: int, kg: float) -> tuple[str, float, str, datetime]:
    return ("body_weight", kg, "kg", T + timedelta(minutes=offset_min))


# ------------------------------------------------------------------------- state
def test_first_measurement_sets_state() -> None:
    s = apply_measurement(HealthState(user_id="u"), *meas(0, 72.5))
    assert s.metrics["body_weight"].value == 72.5
    assert s.version == 1 and s.as_of == T


def test_duplicate_and_older_measurements_change_nothing() -> None:
    s1 = apply_measurement(HealthState(user_id="u"), *meas(10, 72.0))
    assert apply_measurement(s1, *meas(10, 72.0)) is s1  # duplicate
    assert apply_measurement(s1, *meas(5, 99.0)) is s1  # older: ignored, not overwritten
    assert s1.version == 1


def test_newer_measurement_replaces_and_bumps_version() -> None:
    s1 = apply_measurement(HealthState(user_id="u"), *meas(0, 72.0))
    s2 = apply_measurement(s1, *meas(10, 71.0))
    assert s2.metrics["body_weight"].value == 71.0
    assert s2.version == 2 and s2.as_of == T + timedelta(minutes=10)


def test_delivery_order_does_not_change_the_final_state() -> None:
    """Kafka may redeliver and reorder across retries; the result must be order-independent."""
    events = [meas(0, 73.0), meas(10, 72.0), meas(20, 71.0)]
    finals = set()
    for perm in itertools.permutations(events):
        s = HealthState(user_id="u")
        for e in perm:
            s = apply_measurement(s, *e)
        finals.add((s.metrics["body_weight"].value, s.as_of))
    assert finals == {(71.0, T + timedelta(minutes=20))}


def test_different_metrics_are_independent() -> None:
    s = apply_measurement(HealthState(user_id="u"), *meas(0, 72.0))
    s = apply_measurement(s, "resting_heart_rate", 58.0, "bpm", T)
    assert set(s.metrics) == {"body_weight", "resting_heart_rate"}


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_non_finite_numbers_are_rejected(bad: float) -> None:
    with pytest.raises(EnvelopeError, match="finite"):
        apply_measurement(HealthState(user_id="u"), "body_weight", bad, "kg", T)


@pytest.mark.parametrize(
    ("metric", "unit", "when"),
    [("", "kg", T), ("body_weight", "", T), ("body_weight", "kg", datetime(2026, 10, 20))],
)
def test_incomplete_measurements_are_rejected(metric: str, unit: str, when: datetime) -> None:
    with pytest.raises(EnvelopeError):
        apply_measurement(HealthState(user_id="u"), metric, 1.0, unit, when)


def test_store_round_trip_and_default() -> None:
    store = InMemoryStateStore()
    assert store.get("u").version == 0  # unknown user: empty state, not an error
    s = apply_measurement(HealthState(user_id="u"), *meas(0, 72.0))
    store.put(s)
    assert store.get("u") == s


# ----------------------------------------------------------------------- handler
def measurement_event(
    event_id: str = "e-1", kg: float = 72.5, minutes: int = 0
) -> tuple[EventMeta, payloads_pb2.MeasurementRecorded]:
    meta = EventMeta(
        event_id=event_id,
        correlation_id="corr-1",
        user_id="u-1",
        household_id="h-1",
        type="measurement.recorded",
        occurred_at=T,
        trace_id="trace-1",
    )
    m = payloads_pb2.MeasurementRecorded(metric_type="body_weight")
    m.value.value, m.value.unit = kg, "kg"
    m.measured_at.FromDatetime(T + timedelta(minutes=minutes))
    return meta, m


def setup() -> tuple[object, InMemoryStateStore, RecordingPublisher]:
    store, pub = InMemoryStateStore(), RecordingPublisher()
    return make_handler(store, PlainSerde(), pub, clock=lambda: NOW), store, pub


def test_handler_updates_state_and_emits_a_state_event() -> None:
    handler, store, pub = setup()
    meta, payload = measurement_event()
    handler(meta, payload)  # type: ignore[operator]

    assert store.get("u-1").metrics["body_weight"].value == 72.5
    (sent,) = pub.sent
    assert sent.topic == USER_STATE and sent.key == "u-1"
    out_meta, out = codec.decode(sent.value)
    assert out_meta.type == STATE_EVENT_TYPE
    assert out_meta.causation_id == "e-1"  # the chain is traceable
    assert out_meta.correlation_id == "corr-1" and out_meta.trace_id == "trace-1"
    assert out.latest_metrics["body_weight"].value == 72.5
    assert out.latest_metrics["body_weight"].unit == "kg"


def test_handler_is_retry_safe_same_output_id_and_no_double_state_change() -> None:
    handler, store, pub = setup()
    meta, payload = measurement_event()
    handler(meta, payload)  # type: ignore[operator]
    handler(meta, payload)  # type: ignore[operator]  # a retry after a downstream failure
    first, second = (codec.decode(p.value)[0] for p in pub.sent)
    assert first.event_id == second.event_id  # downstream consumers dedupe on this
    assert store.get("u-1").version == 1  # the state changed exactly once


def test_derived_event_id_is_deterministic_and_input_specific() -> None:
    assert derived_event_id("a") == derived_event_id("a")
    assert derived_event_id("a") != derived_event_id("b")


def test_state_event_is_flushed_before_the_handler_returns() -> None:
    handler, _, pub = setup()
    handler(*measurement_event())  # type: ignore[operator]
    assert pub.log == ["publish", "flush"]


def test_other_event_families_are_acknowledged_without_side_effects() -> None:
    handler, store, pub = setup()
    meta = EventMeta(
        correlation_id="c",
        user_id="u-1",
        household_id="h-1",
        type="meal.logged",
        occurred_at=T,
    )
    handler(meta, payloads_pb2.MealLogged())  # type: ignore[operator]
    assert pub.sent == [] and store.get("u-1").version == 0


def test_measurement_without_a_value_or_time_is_rejected_as_invalid_input() -> None:
    handler, _, _ = setup()
    meta, _ = measurement_event()
    with pytest.raises(EnvelopeError):
        handler(meta, payloads_pb2.MeasurementRecorded(metric_type="body_weight"))  # type: ignore[operator]
