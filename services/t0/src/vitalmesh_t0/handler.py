"""T0 handler: ``health.state.updated`` -> engine -> Guardian -> released or blocked.

No LLM. The Guardian is the only gate to ``recommendations.released``: a recommendation that
is not cleared goes to ``safety.blocked`` and never to the released topic. The handler is
retry-safe: recommendation and output event ids are deterministic functions of the input
event id, so a redelivery re-publishes the same ids and consumers dedupe them (D28).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

from vitalmesh_common.envelope import EventMeta, Tier, utcnow
from vitalmesh_common.failure import EnvelopeError
from vitalmesh_common.processor import EventSerde, Handler, Publisher
from vitalmesh_common.topics import RECOMMENDATIONS_RELEASED, SAFETY_BLOCKED
from vitalmesh_wellness.calculators import OutOfRange
from vitalmesh_wellness.engine import ENGINE_VERSION, build_recommendation
from vitalmesh_wellness.guardian import Guardian

from vitalmesh_t0.convert import to_blocked, to_released
from vitalmesh_t0.profiles import ProfileStore

INPUT_TYPE = "health.state.updated"
RELEASED_TYPE = "recommendation.released"
BLOCKED_TYPE = "recommendation.blocked"
_NAMESPACE = uuid.UUID("b3d5a1c2-7e4f-4a9b-8c1d-5f6a7b8c9d01")


class UnknownUser(LookupError):
    """No profile yet. Retryable: the profile may be seeded moments later; bounded by the
    retry policy, then dead-lettered."""


def derived_id(source_event_id: str, kind: str) -> str:
    return str(uuid.uuid5(_NAMESPACE, f"{source_event_id}:{kind}"))


def make_handler(
    profiles: ProfileStore,
    serde: EventSerde,
    publisher: Publisher,
    guardian: Guardian | None = None,
    clock: Callable[[], datetime] = utcnow,
) -> Handler:
    gate = guardian or Guardian()

    def handle(meta: EventMeta, payload: Any) -> None:
        if meta.type != INPUT_TYPE:
            return
        record = profiles.get(meta.user_id)
        if record is None:
            raise UnknownUser(f"no profile for user {meta.user_id!r}")

        rec_id = derived_id(meta.event_id, "recommendation")
        try:
            rec = build_recommendation(record.profile, record.recent_sleep_hours, rec_id)
        except OutOfRange as exc:  # implausible stored data: never retryable, never guessed
            raise EnvelopeError(f"profile data out of range: {exc}") from exc
        verdict = gate.evaluate(rec)

        now = clock()
        if verdict.cleared:
            topic, type_ = RECOMMENDATIONS_RELEASED, RELEASED_TYPE
            out: Any = to_released(rec, verdict, ENGINE_VERSION, now)
        else:
            topic, type_ = SAFETY_BLOCKED, BLOCKED_TYPE
            out = to_blocked(rec, verdict)

        out_meta = EventMeta(
            event_id=derived_id(meta.event_id, type_),
            correlation_id=meta.correlation_id,
            causation_id=meta.event_id,
            user_id=meta.user_id,
            household_id=meta.household_id,
            type=type_,
            occurred_at=now,
            trace_id=meta.trace_id,
            tier=Tier.T0,
            rules_version=verdict.rules_version,
        )
        publisher.publish(topic, meta.user_id, serde.serialize_event(topic, out_meta, out))
        publisher.flush()  # the offset commits only after the result is durable

    return handle
