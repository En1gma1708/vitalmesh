from __future__ import annotations

from datetime import UTC, datetime

import pytest
from vitalmesh.events.v1 import payloads_pb2
from vitalmesh_common.envelope import EventMeta
from vitalmesh_common.failure import EnvelopeError
from vitalmesh_common.processor import Handler
from vitalmesh_common.testing import PlainSerde, RecordingPublisher
from vitalmesh_processor.handler import make_handler
from vitalmesh_processor.labstore import InMemoryLabStore
from vitalmesh_processor.state import InMemoryStateStore
from vitalmesh_wellness.labs import Flag

T = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)


def meta(event_id: str = "e1") -> EventMeta:
    return EventMeta(
        event_id=event_id,
        correlation_id="c",
        user_id="u-1",
        household_id="h",
        type="lab.result.recorded",
        occurred_at=T,
    )


def lab(
    name: str = "ALT", value: float = 64.0, unit: str = "U/L", ref: str = "<50"
) -> payloads_pb2.LabResultRecorded:
    p = payloads_pb2.LabResultRecorded(marker_name=name)
    p.numeric.value, p.numeric.unit = value, unit
    p.reference.raw_text = ref
    p.collected_at.FromDatetime(T.replace(tzinfo=None))
    return p


def build() -> tuple[Handler, InMemoryLabStore, RecordingPublisher]:
    labs, pub = InMemoryLabStore(), RecordingPublisher()
    return make_handler(InMemoryStateStore(), PlainSerde(), pub, labs=labs), labs, pub


def test_lab_is_interpreted_stored_and_emits_no_state_event() -> None:
    handle, labs, pub = build()
    handle(meta(), lab())
    [r] = labs.list("u-1")
    assert (r.marker, r.flag, r.canonical_unit) == ("alt", Flag.HIGH, "u/l")
    assert pub.sent == []  # labs do not change recommendations yet


def test_redelivery_and_reordering_do_not_duplicate() -> None:
    handle, labs, _ = build()
    handle(meta("e1"), lab())
    handle(meta("e1"), lab())
    handle(meta("e2"), lab(name="alt"))  # same series, same time: one entry
    assert len(labs.list("u-1")) == 1


def test_uninterpretable_lab_is_not_retryable() -> None:
    handle, labs, _ = build()
    with pytest.raises(EnvelopeError):
        handle(meta(), lab(value=9000.0, unit="mg/dL", name="Glucose Fasting", ref=""))
    assert labs.list("u-1") == []


def test_missing_collected_at_is_not_retryable() -> None:
    handle, _, _ = build()
    p = payloads_pb2.LabResultRecorded(marker_name="ALT")
    p.numeric.value, p.numeric.unit = 30.0, "U/L"
    with pytest.raises(EnvelopeError):
        handle(meta(), p)


def test_text_result_is_stored_unclassified() -> None:
    handle, labs, _ = build()
    p = payloads_pb2.LabResultRecorded(marker_name="RBC Morphology", text="Normocytic")
    p.collected_at.FromDatetime(T.replace(tzinfo=None))
    handle(meta(), p)
    [r] = labs.list("u-1")
    assert r.text == "Normocytic" and r.flag is Flag.UNCLASSIFIED


def test_handler_without_a_lab_store_ignores_labs() -> None:
    pub = RecordingPublisher()
    make_handler(InMemoryStateStore(), PlainSerde(), pub)(meta(), lab())
    assert pub.sent == []
