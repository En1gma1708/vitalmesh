"""Guardian gate handler: ``recommendation.drafted`` -> ``safety.cleared`` or ``safety.blocked``.

Cleared drafts go to ``drafts.cleared`` (Release for low risk, the Checker for medium and up).
Blocked drafts go to ``safety.blocked`` and nowhere else: a block is final for that revision, the
user is told generically and the reason stays internal (D40). No repair loop in this version.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from vitalmesh.events.v1 import payloads_pb2
from vitalmesh_common.envelope import EventMeta, utcnow
from vitalmesh_common.processor import EventSerde, Handler, Publisher
from vitalmesh_common.topics import DRAFTS_CLEARED, SAFETY_BLOCKED
from vitalmesh_stores import ProfileStore
from vitalmesh_wellness.guardian import Guardian

from vitalmesh_agents.events import POST_BUNDLE_BUDGET_S, emit, next_meta
from vitalmesh_agents.guardian_gate import GateVerdict, evaluate_draft

INPUT_TYPE = "recommendation.drafted"
CLEARED_TYPE = "safety.cleared"
BLOCKED_TYPE = "recommendation.blocked"


def to_blocked(draft: Any, verdict: GateVerdict) -> payloads_pb2.RecommendationBlocked:
    out = payloads_pb2.RecommendationBlocked(
        recommendation_id=draft.recommendation_id, rules_version=verdict.rules_version
    )
    for v in verdict.violations:
        item = out.violations.add(rule=v.rule, message=v.message)
        if v.claim_text is not None:
            item.claim_text = v.claim_text
    return out


def make_guardian_handler(
    profiles: ProfileStore,
    serde: EventSerde,
    publisher: Publisher,
    guardian: Guardian | None = None,
    clock: Callable[[], datetime] = utcnow,
) -> Handler:
    def handle(meta: EventMeta, payload: Any) -> None:
        if meta.type != INPUT_TYPE:
            return
        record = profiles.get(meta.user_id)
        user = record.profile.constraints() if record else None
        verdict = evaluate_draft(payload, user, guardian)

        now = clock()
        revision = f"r{payload.revision}"
        if verdict.cleared:
            cleared = payloads_pb2.SafetyCleared(rules_version=verdict.rules_version)
            cleared.draft.CopyFrom(payload)
            out = next_meta(
                meta,
                CLEARED_TYPE,
                f"safety.cleared.{revision}",
                now,
                deadline_at=now + timedelta(seconds=POST_BUNDLE_BUDGET_S),
                rules_version=verdict.rules_version,
            )
            emit(serde, publisher, DRAFTS_CLEARED, out, cleared)
        else:
            out = next_meta(
                meta,
                BLOCKED_TYPE,
                f"safety.blocked.{revision}",
                now,
                rules_version=verdict.rules_version,
            )
            emit(serde, publisher, SAFETY_BLOCKED, out, to_blocked(payload, verdict))
        publisher.flush()

    return handle
