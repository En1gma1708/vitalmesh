from __future__ import annotations

from datetime import UTC, datetime

import pytest
from vitalmesh.events.v1 import common_pb2, payloads_pb2
from vitalmesh_common import codec
from vitalmesh_common.envelope import EventMeta
from vitalmesh_common.failure import EnvelopeError

T0 = datetime(2026, 10, 20, 8, 0, tzinfo=UTC)


def meta(type_: str) -> EventMeta:
    return EventMeta(
        correlation_id="corr", user_id="u-1", household_id="h-1", type=type_, occurred_at=T0
    )


def numeric_lab() -> payloads_pb2.LabResultRecorded:
    lab = payloads_pb2.LabResultRecorded(marker_code="1742-6", marker_name="ALT")
    lab.numeric.value = 64.0
    lab.numeric.unit = "U/L"
    lab.reference.raw_text = "<50"
    lab.reference.parsed = True
    lab.reference.high = 50.0
    lab.collected_at.FromDatetime(T0.replace(tzinfo=None))
    return lab


def test_numeric_lab_result_round_trips_with_unit_and_reference() -> None:
    _, got = codec.decode(codec.encode(meta("lab.result.recorded"), numeric_lab()))
    assert got.WhichOneof("result") == "numeric"
    assert (got.numeric.value, got.numeric.unit) == (64.0, "U/L")
    assert got.reference.raw_text == "<50"
    assert got.reference.HasField("high")
    assert not got.reference.HasField("low")  # a one-sided limit must not become low = 0


def test_qualitative_lab_result_is_text_not_a_number() -> None:
    lab = payloads_pb2.LabResultRecorded(marker_name="RBC Morphology", text="Normocytic")
    _, got = codec.decode(codec.encode(meta("lab.result.recorded"), lab))
    assert got.WhichOneof("result") == "text"
    assert got.text == "Normocytic"


def test_unparsed_categorical_reference_keeps_raw_text() -> None:
    lab = numeric_lab()
    lab.reference.Clear()
    lab.reference.raw_text = "Desirable: <200; Borderline high = 200-239; High: > 240"
    _, got = codec.decode(codec.encode(meta("lab.result.recorded"), lab))
    assert not got.reference.parsed
    assert got.reference.raw_text.startswith("Desirable")


def released() -> payloads_pb2.RecommendationReleased:
    rec = payloads_pb2.RecommendationReleased(
        recommendation_id="r1", engine_version="1", rules_version="1"
    )
    claim = rec.claims.add(
        text="Aim for about 2759 kcal per day (maintain).",
        provenance=common_pb2.PROVENANCE_COMPUTED,
        source="calculators@1",
        metric="daily_energy_target",
    )
    claim.quantity.value = 2759.0
    claim.quantity.unit = "kcal"
    return rec


def test_recommendation_released_round_trips_claims_with_provenance() -> None:
    _, got = codec.decode(codec.encode(meta("recommendation.released"), released()))
    claim = got.claims[0]
    assert claim.provenance == common_pb2.PROVENANCE_COMPUTED
    assert (claim.metric, claim.quantity.value, claim.quantity.unit) == (
        "daily_energy_target",
        2759.0,
        "kcal",
    )
    assert got.rules_version == "1"


def test_recommendation_blocked_round_trips_violations() -> None:
    blocked = payloads_pb2.RecommendationBlocked(recommendation_id="r1", rules_version="1")
    blocked.violations.add(rule="energy_bounds", message="too low")
    _, got = codec.decode(codec.encode(meta("recommendation.blocked"), blocked))
    assert got.violations[0].rule == "energy_bounds"
    assert not got.violations[0].HasField("claim_text")


@pytest.mark.parametrize(
    ("type_", "payload"),
    [
        ("lab.result.recorded", released()),
        ("recommendation.released", numeric_lab()),
        ("recommendation.blocked", released()),
    ],
)
def test_type_must_match_payload(type_: str, payload: object) -> None:
    with pytest.raises(EnvelopeError):
        codec.encode(meta(type_), payload)
