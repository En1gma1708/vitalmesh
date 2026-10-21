"""Helpers shared by every agent handler: deterministic ids and the next envelope in a run.

A handler may run twice for the same input (at-least-once delivery), so every event it emits has
an id derived from the input event id and the kind of output (D28). A redelivery re-publishes the
same id and the consumer's idempotency store drops the duplicate.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from vitalmesh_common.envelope import EventMeta, Tier
from vitalmesh_common.processor import EventSerde, Publisher

_NAMESPACE = uuid.UUID("5e2f8c14-93ab-4d6e-b7a1-2c4d9e0f1a33")

# After the Aggregator closes a run, downstream stages get their own time budget: a partial
# bundle is emitted AT the specialists' deadline, so reusing that deadline would make the next
# stage reject it as expired (D68).
POST_BUNDLE_BUDGET_S = 120.0


def derived_id(source_event_id: str, kind: str) -> str:
    return str(uuid.uuid5(_NAMESPACE, f"{source_event_id}:{kind}"))


def next_meta(
    parent: EventMeta,
    type_: str,
    kind: str,
    now: datetime,
    *,
    tier: Tier | None = None,
    deadline_at: datetime | None = None,
    rules_version: str | None = None,
    event_id: str | None = None,
) -> EventMeta:
    """The envelope of the event that follows ``parent`` in the same run."""
    return EventMeta(
        event_id=event_id or derived_id(parent.event_id, kind),
        correlation_id=parent.correlation_id,
        causation_id=parent.event_id,
        user_id=parent.user_id,
        household_id=parent.household_id,
        type=type_,
        occurred_at=now,
        trace_id=parent.trace_id,
        tier=tier if tier is not None else parent.tier,
        deadline_at=deadline_at,
        rules_version=rules_version,
    )


def emit(
    serde: EventSerde, publisher: Publisher, topic: str, meta: EventMeta, payload: Any
) -> None:
    """Serialise and publish; the caller flushes once, before the offset commits."""
    publisher.publish(topic, meta.user_id, serde.serialize_event(topic, meta, payload))
