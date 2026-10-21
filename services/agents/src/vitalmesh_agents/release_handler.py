"""Release handler: routes the three events that can end a run (D68).

======================  =====================================================================
event                   action
======================  =====================================================================
safety.cleared, low     release (final Guardian re-check inside the gate)
review.completed        approved + medium: release; approved + high: ask a human;
                        revise on revision 0: not mine (the Synthesizer rewrites);
                        revise on revision 1 or rejected: ask a human
review.decided          resume the waiting run (ignored when nothing is waiting)
======================  =====================================================================
"""

from __future__ import annotations

from typing import Any

from vitalmesh.events.v1 import payloads_pb2
from vitalmesh_common.envelope import EventMeta
from vitalmesh_common.failure import EnvelopeError
from vitalmesh_common.processor import Handler
from vitalmesh_common.run import Risk, risk_from_proto

from vitalmesh_agents.release import ReleaseGate


def make_release_handler(gate: ReleaseGate) -> Handler:
    def handle(meta: EventMeta, payload: Any) -> None:
        if meta.type == "safety.cleared":
            draft = payload.draft
            if risk_from_proto(draft.risk) is Risk.LOW:
                gate.release_auto(meta, draft, payload.rules_version)
        elif meta.type == "review.completed":
            _on_review(gate, meta, payload)
        elif meta.type == "review.decided":
            if not payload.reviewer_id.strip():
                raise EnvelopeError("a review decision needs a reviewer id")
            if payload.decision not in (
                payloads_pb2.DECISION_APPROVE,
                payloads_pb2.DECISION_REJECT,
            ):
                raise EnvelopeError("a review decision must be approve or reject")
            gate.resume(payload)

    return handle


def _on_review(gate: ReleaseGate, meta: EventMeta, review: Any) -> None:
    draft = review.draft
    risk = risk_from_proto(draft.risk)
    problems = list(review.problems)
    if review.verdict == payloads_pb2.REVIEW_APPROVED:
        if risk is Risk.HIGH:
            gate.escalate(meta, draft, review.rules_version, ["risk.high"], problems)
        else:
            gate.release_auto(meta, draft, review.rules_version)
    elif review.verdict == payloads_pb2.REVIEW_REVISE:
        if draft.revision >= 1:
            gate.escalate(
                meta, draft, review.rules_version, ["checker.revision_not_enough"], problems
            )
        # revision 0: the Synthesizer rewrites once; that rewrite comes back through the Guardian
    elif review.verdict == payloads_pb2.REVIEW_REJECTED:
        gate.escalate(meta, draft, review.rules_version, ["checker.rejected"], problems)
    else:
        raise EnvelopeError("review verdict is unspecified")
