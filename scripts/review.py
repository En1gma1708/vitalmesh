"""Answer a pending human review (the stand-in for the clinician console, built in Phase 5).

    uv run python scripts/review.py <recommendation_id> approve|reject [reviewer_id]

The decision is an ordinary event (``review.decided``); the paused run resumes from its Postgres
checkpoint, on whichever worker consumes it. A decision for a recommendation that is not waiting
is ignored.
"""

from __future__ import annotations

import sys
import uuid
from datetime import UTC, datetime

from vitalmesh.events.v1 import payloads_pb2
from vitalmesh_common.envelope import EventMeta
from vitalmesh_common.kafka import KafkaPublisher, KafkaSettings, SchemaRegistrySerde
from vitalmesh_common.topics import REVIEWS_DECIDED


def main() -> None:
    if len(sys.argv) < 3 or sys.argv[2] not in ("approve", "reject"):
        raise SystemExit(__doc__)
    rec_id, choice = sys.argv[1], sys.argv[2]
    reviewer = sys.argv[3] if len(sys.argv) > 3 else "clinician-demo"
    settings = KafkaSettings.from_env()
    serde = SchemaRegistrySerde(settings)
    publisher = KafkaPublisher(settings, client_id="review-cli")
    meta = EventMeta(
        correlation_id=f"review-{uuid.uuid4().hex[:8]}",
        user_id=f"reviewer-{reviewer}",
        household_id="h-demo",
        type="review.decided",
        occurred_at=datetime.now(UTC),
    )
    msg = payloads_pb2.ReviewDecided(
        recommendation_id=rec_id,
        decision=(
            payloads_pb2.DECISION_APPROVE if choice == "approve" else payloads_pb2.DECISION_REJECT
        ),
        reviewer_id=reviewer,
    )
    payload = serde.serialize_event(REVIEWS_DECIDED, meta, msg)
    publisher.publish(REVIEWS_DECIDED, meta.user_id, payload)
    publisher.flush()
    print(f"published review.decided: {choice} {rec_id} by {reviewer}")


if __name__ == "__main__":
    main()
