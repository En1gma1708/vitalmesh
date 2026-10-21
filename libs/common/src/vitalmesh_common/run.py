"""Domain vocabulary of a T3 run (D68) and its mapping to the wire enums.

``UNSPECIFIED`` on the wire is invalid input, never a default: a consumer that receives it
raises ``EnvelopeError`` (not retryable) instead of guessing a specialist or a risk level.
"""

from __future__ import annotations

from enum import StrEnum

from vitalmesh.events.v1 import common_pb2

from vitalmesh_common.failure import EnvelopeError


class Specialist(StrEnum):
    NUTRITION = "nutrition"
    MOVEMENT_RECOVERY = "movement_recovery"
    CONDITION_MEDICATION = "condition_medication"


class Risk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    @property
    def rank(self) -> int:
        return _RISK_RANK[self]

    def at_least(self, other: Risk) -> bool:
        return self.rank >= other.rank


_RISK_RANK = {Risk.LOW: 1, Risk.MEDIUM: 2, Risk.HIGH: 3}

_SPECIALIST_TO_PROTO = {
    Specialist.NUTRITION: common_pb2.SPECIALIST_NUTRITION,
    Specialist.MOVEMENT_RECOVERY: common_pb2.SPECIALIST_MOVEMENT_RECOVERY,
    Specialist.CONDITION_MEDICATION: common_pb2.SPECIALIST_CONDITION_MEDICATION,
}
_PROTO_TO_SPECIALIST = {v: k for k, v in _SPECIALIST_TO_PROTO.items()}

_RISK_TO_PROTO = {
    Risk.LOW: common_pb2.RISK_LOW,
    Risk.MEDIUM: common_pb2.RISK_MEDIUM,
    Risk.HIGH: common_pb2.RISK_HIGH,
}
_PROTO_TO_RISK = {v: k for k, v in _RISK_TO_PROTO.items()}


def specialist_to_proto(value: Specialist) -> int:
    return _SPECIALIST_TO_PROTO[value]


def specialist_from_proto(value: int) -> Specialist:
    try:
        return _PROTO_TO_SPECIALIST[value]
    except KeyError:
        raise EnvelopeError(f"unknown or unspecified specialist ({value})") from None


def risk_to_proto(value: Risk) -> int:
    return _RISK_TO_PROTO[value]


def risk_from_proto(value: int) -> Risk:
    try:
        return _PROTO_TO_RISK[value]
    except KeyError:
        raise EnvelopeError(f"unknown or unspecified risk level ({value})") from None
