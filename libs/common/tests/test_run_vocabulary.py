from __future__ import annotations

import pytest
from vitalmesh.events.v1 import common_pb2
from vitalmesh_common.failure import EnvelopeError
from vitalmesh_common.run import (
    Risk,
    Specialist,
    risk_from_proto,
    risk_to_proto,
    specialist_from_proto,
    specialist_to_proto,
)


@pytest.mark.parametrize("value", list(Specialist))
def test_specialist_round_trips(value: Specialist) -> None:
    assert specialist_from_proto(specialist_to_proto(value)) is value


@pytest.mark.parametrize("value", list(Risk))
def test_risk_round_trips(value: Risk) -> None:
    assert risk_from_proto(risk_to_proto(value)) is value


def test_unspecified_is_invalid_not_a_default() -> None:
    with pytest.raises(EnvelopeError):
        specialist_from_proto(common_pb2.SPECIALIST_UNSPECIFIED)
    with pytest.raises(EnvelopeError):
        risk_from_proto(common_pb2.RISK_UNSPECIFIED)
    with pytest.raises(EnvelopeError):
        risk_from_proto(99)


def test_risk_ordering() -> None:
    assert Risk.HIGH.at_least(Risk.MEDIUM)
    assert Risk.MEDIUM.at_least(Risk.MEDIUM)
    assert not Risk.LOW.at_least(Risk.MEDIUM)
    assert sorted(Risk, key=lambda r: r.rank) == [Risk.LOW, Risk.MEDIUM, Risk.HIGH]
