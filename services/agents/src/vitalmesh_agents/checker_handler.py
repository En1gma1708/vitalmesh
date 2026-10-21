"""Checker handler: ``safety.cleared`` -> ``review.completed`` for medium and high risk (D68).

Low-risk drafts are not reviewed here (the Release gate takes them straight away): LLM checks run
by risk, never on every hop (EVENT_BACKBONE.md section 10). A review that cannot be completed
because no provider is available is published as a REJECTED verdict, not retried forever and not
waved through: the run then reaches a human.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from vitalmesh.events.v1 import payloads_pb2
from vitalmesh_common.envelope import EventMeta, utcnow
from vitalmesh_common.processor import EventSerde, Handler, Publisher
from vitalmesh_common.run import Risk, risk_from_proto
from vitalmesh_common.topics import REVIEWS_COMPLETED
from vitalmesh_stores import ProfileStore

from vitalmesh_agents.checker import AgentUnavailable, Checker, CheckResult
from vitalmesh_agents.events import POST_BUNDLE_BUDGET_S, emit, next_meta

INPUT_TYPE = "safety.cleared"
OUTPUT_TYPE = "review.completed"
_VERDICT = {
    "approve": payloads_pb2.REVIEW_APPROVED,
    "revise": payloads_pb2.REVIEW_REVISE,
    "reject": payloads_pb2.REVIEW_REJECTED,
}


def make_checker_handler(
    checker: Checker,
    profiles: ProfileStore,
    serde: EventSerde,
    publisher: Publisher,
    clock: Callable[[], datetime] = utcnow,
) -> Handler:
    def handle(meta: EventMeta, payload: Any) -> None:
        if meta.type != INPUT_TYPE:
            return
        draft = payload.draft
        if not risk_from_proto(draft.risk).at_least(Risk.MEDIUM):
            return  # low risk: not mine
        record = profiles.get(meta.user_id)
        conditions = sorted(record.profile.conditions) if record else []
        try:
            result = checker.review(
                question=draft.question,
                text=draft.text,
                facts=[c.text for c in draft.claims],
                quotes=[c.quote for c in draft.citations],
                conditions=conditions,
                partial=draft.partial,
                user_id=meta.user_id,
                session_id=meta.correlation_id,
            )
        except AgentUnavailable:
            result = CheckResult(
                verdict="reject",
                problems=("the independent review was unavailable",),
                reason="unavailable",
            )

        review = payloads_pb2.ReviewCompleted(
            recommendation_id=draft.recommendation_id,
            verdict=_VERDICT[result.verdict],
            rules_version=payload.rules_version,
        )
        review.problems.extend(result.problems)
        review.draft.CopyFrom(draft)

        now = clock()
        out = next_meta(
            meta,
            OUTPUT_TYPE,
            f"review.completed.r{draft.revision}",
            now,
            deadline_at=now + timedelta(seconds=POST_BUNDLE_BUDGET_S),
            rules_version=payload.rules_version,
        )
        emit(serde, publisher, REVIEWS_COMPLETED, out, review)
        publisher.flush()

    return handle
