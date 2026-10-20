from __future__ import annotations

from datetime import UTC, datetime, timedelta

from vitalmesh.events.v1 import payloads_pb2
from vitalmesh_common.envelope import EventMeta
from vitalmesh_common.testing import PlainSerde, RecordingPublisher
from vitalmesh_processor.handler import make_handler
from vitalmesh_processor.history import InMemoryHistoryStore, Point
from vitalmesh_processor.state import InMemoryStateStore

T = datetime(2026, 10, 5, 8, 0, tzinfo=UTC)


def test_points_are_idempotent_sorted_and_filterable() -> None:
    h = InMemoryHistoryStore()
    late, early = Point(T + timedelta(days=2), 71.5, "kg"), Point(T, 70.0, "kg")
    for p in (late, early, late, early):  # out of order and redelivered
        h.add("u", "body_weight", p)
    assert h.series("u", "body_weight") == [early, late]
    assert h.series("u", "body_weight", since=T + timedelta(days=1)) == [late]
    assert h.series("u", "sleep_duration") == []
    assert h.series("other", "body_weight") == []


def measurement(value: float, at: datetime, event_id: str) -> tuple[EventMeta, object]:
    meta = EventMeta(
        event_id=event_id,
        correlation_id="c",
        user_id="u",
        household_id="h",
        type="measurement.recorded",
        occurred_at=T,
    )
    m = payloads_pb2.MeasurementRecorded(metric_type="body_weight")
    m.value.value, m.value.unit = value, "kg"
    m.measured_at.FromDatetime(at.replace(tzinfo=None))
    return meta, m


def test_handler_records_history_including_older_readings() -> None:
    hist = InMemoryHistoryStore()
    handle = make_handler(InMemoryStateStore(), PlainSerde(), RecordingPublisher(), history=hist)
    handle(*measurement(71.0, T + timedelta(days=1), "e1"))
    handle(*measurement(70.0, T, "e2"))  # older than the latest: not "latest", still history
    handle(*measurement(70.0, T, "e2"))  # redelivery
    assert [p.value for p in hist.series("u", "body_weight")] == [70.0, 71.0]


def test_invalid_measurement_never_reaches_history() -> None:
    import pytest
    from vitalmesh_common.failure import EnvelopeError

    hist = InMemoryHistoryStore()
    handle = make_handler(InMemoryStateStore(), PlainSerde(), RecordingPublisher(), history=hist)
    meta, m = measurement(float("nan"), T, "e3")
    with pytest.raises(EnvelopeError):
        handle(meta, m)
    assert hist.series("u", "body_weight") == []
