from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from vitalmesh.events.v1 import envelope_pb2, payloads_pb2
from vitalmesh_common import codec
from vitalmesh_common.envelope import EventMeta, Tier
from vitalmesh_common.failure import EnvelopeError

T0 = datetime(2026, 10, 20, 8, 0, 0, 500_000, tzinfo=UTC)


def meta(type_: str = "measurement.recorded", **kw: object) -> EventMeta:
    base: dict[str, object] = {
        "correlation_id": "corr",
        "user_id": "u-1",
        "household_id": "h-1",
        "type": type_,
        "occurred_at": T0,
    }
    base.update(kw)
    return EventMeta(**base)


def weight(kg: float = 72.5) -> payloads_pb2.MeasurementRecorded:
    m = payloads_pb2.MeasurementRecorded(metric_type="body_weight")
    m.value.value = kg
    m.value.unit = "kg"
    return m


def test_round_trip_preserves_every_field() -> None:
    original = meta(
        causation_id="cause",
        source="fitbit",
        external_id="ext-9",
        trace_id="trace-1",
        tier=Tier.T3,
        deadline_at=T0 + timedelta(seconds=30),
        attempt=2,
        rules_version="r1",
        schema_version=3,
    )
    got_meta, got_payload = codec.decode(codec.encode(original, weight()))
    assert got_meta == original
    assert got_payload.value.value == 72.5
    assert got_payload.value.unit == "kg"


def test_optional_fields_stay_none_not_empty_string() -> None:
    got_meta, _ = codec.decode(codec.encode(meta(), weight()))
    assert got_meta.causation_id is None
    assert got_meta.source is None and got_meta.external_id is None
    assert got_meta.trace_id is None and got_meta.tier is None
    assert got_meta.deadline_at is None and got_meta.rules_version is None


def test_timestamps_survive_with_microsecond_precision_and_timezone() -> None:
    got_meta, _ = codec.decode(codec.encode(meta(), weight()))
    assert got_meta.occurred_at == T0
    assert got_meta.occurred_at.tzinfo is not None


def test_idempotency_key_survives_the_wire() -> None:
    with_provider = meta(source="fitbit", external_id="abc")
    got, _ = codec.decode(codec.encode(with_provider, weight()))
    assert got.idempotency_key() == "fitbit:abc"


def test_every_declared_type_has_a_matching_payload_field() -> None:
    fields = envelope_pb2.Envelope.DESCRIPTOR.oneofs_by_name["payload"].fields
    declared = {f.name for f in fields}
    assert set(codec.PAYLOAD_FIELDS.values()) == declared  # no drift in either direction


def test_type_must_match_the_payload() -> None:
    with pytest.raises(EnvelopeError, match="does not match"):
        codec.to_envelope(meta("meal.logged"), weight())


def test_unknown_type_is_rejected() -> None:
    with pytest.raises(EnvelopeError, match="unknown event type"):
        codec.to_envelope(meta("nope.recorded"), weight())


def test_decoding_rejects_a_type_that_disagrees_with_the_payload() -> None:
    env = codec.to_envelope(meta(), weight())
    env.type = "meal.logged"  # a producer lying about its type
    with pytest.raises(EnvelopeError, match="does not match payload"):
        codec.from_envelope(env)


def test_decoding_rejects_missing_payload_and_missing_timestamp() -> None:
    env = envelope_pb2.Envelope(event_id="e", user_id="u", household_id="h", type="meal.logged")
    with pytest.raises(EnvelopeError, match="no payload"):
        codec.from_envelope(env)
    env = codec.to_envelope(meta(), weight())
    env.ClearField("occurred_at")
    with pytest.raises(EnvelopeError, match="occurred_at"):
        codec.from_envelope(env)


def test_garbage_bytes_are_an_envelope_error_not_a_crash() -> None:
    with pytest.raises(EnvelopeError):
        codec.decode(b"\xff\xff\xff\xff not protobuf")


def test_invalid_domain_values_surface_as_envelope_error() -> None:
    env = codec.to_envelope(meta(), weight())
    env.ClearField("user_id")  # empty user_id violates the domain contract
    with pytest.raises(EnvelopeError):
        codec.from_envelope(env)


def test_source_without_external_id_is_rejected_on_the_wire() -> None:
    env = codec.to_envelope(meta(), weight())
    env.source = "fitbit"  # external_id deliberately missing
    with pytest.raises(EnvelopeError):
        codec.from_envelope(env)


def test_meal_plan_free_text_entry_has_no_recipe() -> None:
    entry = payloads_pb2.MealPlanEntryChanged(
        kind=payloads_pb2.MealPlanEntryChanged.KIND_CREATED,
        plan_date="2026-10-21",
        title="Leftovers",
        text="Finish the dal",
        recipe_scale=1.0,
    )
    _, payload = codec.decode(codec.encode(meta("mealplan.entry.changed"), entry))
    assert not payload.HasField("recipe_id")  # explicit presence for the optional field
    assert payload.plan_date == "2026-10-21"
