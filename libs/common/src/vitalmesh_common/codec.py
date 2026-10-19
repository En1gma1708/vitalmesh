"""Adapter between the pure-domain ``EventMeta`` and the wire ``Envelope`` (Protobuf).

Invariant enforced here: the envelope ``type`` string must match the payload ``oneof``
case, so a producer cannot claim one event type and send another. All decode failures
are raised as ``EnvelopeError`` (non-retryable, goes straight to the DLQ).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from google.protobuf.message import DecodeError
from google.protobuf.timestamp_pb2 import Timestamp
from pydantic import ValidationError
from vitalmesh.events.v1 import common_pb2, envelope_pb2

from vitalmesh_common.envelope import EventMeta, Tier
from vitalmesh_common.failure import EnvelopeError

# event type string  ->  Envelope.payload oneof field
PAYLOAD_FIELDS: dict[str, str] = {
    "measurement.recorded": "measurement",
    "meal.logged": "meal",
    "workout.session.completed": "workout",
    "sleep.session.recorded": "sleep",
    "feedback.recorded": "feedback",
    "mealplan.entry.changed": "mealplan_entry",
    "health.state.updated": "health_state",
    "lab.result.recorded": "lab_result",
    "recommendation.released": "recommendation_released",
    "recommendation.blocked": "recommendation_blocked",
}

_TIER_TO_PROTO = {
    Tier.T0: common_pb2.TIER_T0,
    Tier.T1: common_pb2.TIER_T1,
    Tier.T2: common_pb2.TIER_T2,
    Tier.T3: common_pb2.TIER_T3,
}
_PROTO_TO_TIER = {v: k for k, v in _TIER_TO_PROTO.items()}


def _ts(dt: datetime) -> Timestamp:
    ts = Timestamp()
    ts.FromDatetime(dt.astimezone(UTC))
    return ts


def _dt(ts: Timestamp) -> datetime:
    value: datetime = ts.ToDatetime(tzinfo=UTC)
    return value


def to_envelope(meta: EventMeta, payload: Any) -> envelope_pb2.Envelope:
    field = PAYLOAD_FIELDS.get(meta.type)
    if field is None:
        raise EnvelopeError(f"unknown event type {meta.type!r}")
    expected = envelope_pb2.Envelope.DESCRIPTOR.fields_by_name[field].message_type
    if payload.DESCRIPTOR.full_name != expected.full_name:
        raise EnvelopeError(
            f"payload {payload.DESCRIPTOR.full_name} does not match type {meta.type!r}"
        )

    env = envelope_pb2.Envelope(
        event_id=meta.event_id,
        correlation_id=meta.correlation_id,
        user_id=meta.user_id,
        household_id=meta.household_id,
        type=meta.type,
        schema_version=meta.schema_version,
        attempt=meta.attempt,
    )
    env.occurred_at.CopyFrom(_ts(meta.occurred_at))
    if meta.causation_id is not None:
        env.causation_id = meta.causation_id
    if meta.source is not None and meta.external_id is not None:
        env.source = meta.source
        env.external_id = meta.external_id
    if meta.trace_id is not None:
        env.trace_id = meta.trace_id
    if meta.tier is not None:
        env.tier = _TIER_TO_PROTO[meta.tier]
    if meta.deadline_at is not None:
        env.deadline_at.CopyFrom(_ts(meta.deadline_at))
    if meta.rules_version is not None:
        env.rules_version = meta.rules_version
    getattr(env, field).CopyFrom(payload)
    return env


def from_envelope(env: envelope_pb2.Envelope) -> tuple[EventMeta, Any]:
    which = env.WhichOneof("payload")
    if which is None:
        raise EnvelopeError("envelope has no payload")
    expected_field = PAYLOAD_FIELDS.get(env.type)
    if expected_field is None:
        raise EnvelopeError(f"unknown event type {env.type!r}")
    if which != expected_field:
        raise EnvelopeError(f"type {env.type!r} does not match payload {which!r}")
    if not env.HasField("occurred_at"):
        raise EnvelopeError("occurred_at is required")

    try:
        meta = EventMeta(
            event_id=env.event_id,
            correlation_id=env.correlation_id,
            causation_id=env.causation_id if env.HasField("causation_id") else None,
            user_id=env.user_id,
            household_id=env.household_id,
            source=env.source if env.HasField("source") else None,
            external_id=env.external_id if env.HasField("external_id") else None,
            type=env.type,
            schema_version=env.schema_version or 1,
            occurred_at=_dt(env.occurred_at),
            trace_id=env.trace_id if env.HasField("trace_id") else None,
            tier=_PROTO_TO_TIER.get(env.tier),
            deadline_at=_dt(env.deadline_at) if env.HasField("deadline_at") else None,
            attempt=env.attempt,
            rules_version=env.rules_version if env.HasField("rules_version") else None,
        )
    except ValidationError as exc:
        raise EnvelopeError(str(exc)) from exc
    return meta, getattr(env, which)


def encode(meta: EventMeta, payload: Any) -> bytes:
    """Raw Protobuf bytes (no schema-registry framing); used by tests and tooling."""
    data: bytes = to_envelope(meta, payload).SerializeToString()
    return data


def decode(data: bytes) -> tuple[EventMeta, Any]:
    env = envelope_pb2.Envelope()
    try:
        env.ParseFromString(data)
    except DecodeError as exc:
        raise EnvelopeError(f"undecodable envelope: {exc}") from exc
    return from_envelope(env)
