"""Handler for the state processor: ``user.events`` -> state update -> ``user.state``.

The handler is safe to retry: the state update is idempotent, and the emitted
``health.state.updated`` event has a *deterministic* id derived from the input event, so a
retry produces the same output id and downstream consumers dedupe it.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from vitalmesh.events.v1 import payloads_pb2
from vitalmesh_common.envelope import EventMeta, utcnow
from vitalmesh_common.failure import EnvelopeError
from vitalmesh_common.processor import EventSerde, Handler, Publisher
from vitalmesh_common.topics import USER_STATE
from vitalmesh_wellness.labs import LabError, interpret

from vitalmesh_processor.labstore import LabStore
from vitalmesh_processor.state import HealthState, StateStore, apply_measurement

STATE_EVENT_TYPE = "health.state.updated"
# Fixed namespace: the same input event id always maps to the same output event id.
_OUT_NAMESPACE = uuid.UUID("6f1d6f0e-5a8c-4f6e-9b0e-2f3f0c6f7a11")


def derived_event_id(source_event_id: str) -> str:
    return str(uuid.uuid5(_OUT_NAMESPACE, f"{source_event_id}:{STATE_EVENT_TYPE}"))


def to_state_payload(state: HealthState) -> payloads_pb2.HealthStateUpdated:
    out = payloads_pb2.HealthStateUpdated(state_version=str(state.version))
    if state.as_of is not None:
        out.as_of.FromDatetime(state.as_of)
    for name, metric in state.metrics.items():
        out.latest_metrics[name].value = metric.value
        out.latest_metrics[name].unit = metric.unit
    return out


def make_handler(
    store: StateStore,
    serde: EventSerde,
    publisher: Publisher,
    clock: Callable[[], datetime] = utcnow,
    labs: LabStore | None = None,
) -> Handler:
    def handle_lab(meta: EventMeta, payload: Any) -> None:
        if labs is None:
            return
        lab: payloads_pb2.LabResultRecorded = payload
        if not lab.HasField("collected_at"):
            raise EnvelopeError("lab result needs collected_at")
        which = lab.WhichOneof("result")
        ref = lab.reference.raw_text if lab.HasField("reference") else ""
        try:
            result = interpret(
                lab.marker_name,
                lab.collected_at.ToDatetime(tzinfo=UTC),
                value=lab.numeric.value if which == "numeric" else None,
                unit=lab.numeric.unit if which == "numeric" else None,
                text=lab.text if which == "text" else None,
                reference_text=ref,
                derived=lab.derived,
            )
        except LabError as exc:  # passed ingress but cannot be interpreted: not retryable
            raise EnvelopeError(f"lab result rejected: {exc}") from exc
        labs.put(meta.user_id, result)

    def handle(meta: EventMeta, payload: Any) -> None:
        if meta.type == "lab.result.recorded":
            handle_lab(meta, payload)  # stored only: lab results do not change recommendations yet
            return
        if meta.type != "measurement.recorded":
            return  # other families are acknowledged as no-ops
        m: payloads_pb2.MeasurementRecorded = payload
        if not m.HasField("measured_at") or not m.HasField("value"):
            raise EnvelopeError("measurement needs value and measured_at")

        state = store.get(meta.user_id)
        updated = apply_measurement(
            state,
            m.metric_type,
            m.value.value,
            m.value.unit,
            m.measured_at.ToDatetime(tzinfo=UTC),
        )
        if updated is not state:
            store.put(updated)

        out_meta = EventMeta(
            event_id=derived_event_id(meta.event_id),
            correlation_id=meta.correlation_id,
            causation_id=meta.event_id,
            user_id=meta.user_id,
            household_id=meta.household_id,
            type=STATE_EVENT_TYPE,
            occurred_at=clock(),
            trace_id=meta.trace_id,
            tier=meta.tier,
        )
        value = serde.serialize_event(USER_STATE, out_meta, to_state_payload(updated))
        publisher.publish(USER_STATE, meta.user_id, value)
        publisher.flush()  # the offset commits only after the state event is durable

    return handle
