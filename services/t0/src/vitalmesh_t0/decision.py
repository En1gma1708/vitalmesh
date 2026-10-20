"""The T0 decision with no I/O beyond the profile read: profile -> engine -> Guardian.

Shared by the Kafka handler (``handler.py``) and the API gateway's synchronous path, so both
apply the same rules and the same Guardian gate.
"""

from __future__ import annotations

from dataclasses import dataclass

from vitalmesh_common.failure import EnvelopeError
from vitalmesh_wellness.calculators import OutOfRange
from vitalmesh_wellness.claims import Recommendation
from vitalmesh_wellness.engine import build_recommendation
from vitalmesh_wellness.guardian import Guardian, Verdict

from vitalmesh_t0.profiles import ProfileRecord


@dataclass(frozen=True)
class Decision:
    recommendation: Recommendation
    verdict: Verdict


def decide(record: ProfileRecord, recommendation_id: str, guardian: Guardian) -> Decision:
    try:
        rec = build_recommendation(record.profile, record.recent_sleep_hours, recommendation_id)
    except OutOfRange as exc:  # implausible stored data: never retryable, never guessed
        raise EnvelopeError(f"profile data out of range: {exc}") from exc
    return Decision(rec, guardian.evaluate(rec))
