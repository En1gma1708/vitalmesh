"""Tracker: every event of a run becomes one timeline entry for the app and the operator (D74).

No model and no decisions: it only records what the other agents emitted, in a stable plain-dict
shape that the API turns into a plain-language view for the user (``vitalmesh_api.runview``) and
the full view for the operator. It subscribes to every T3 topic in its own consumer group, so it
cannot slow or change a run, and a tracker outage loses visibility, never an answer.
"""

from __future__ import annotations

from typing import Any

from vitalmesh.events.v1 import common_pb2, payloads_pb2
from vitalmesh_common.envelope import EventMeta
from vitalmesh_common.processor import Handler
from vitalmesh_common.run import risk_from_proto, specialist_from_proto
from vitalmesh_common.topics import (
    ADVICE_BUNDLES,
    ADVICE_COMPLETED,
    DRAFTS_CLEARED,
    QUESTIONS_ASKED,
    RECOMMENDATIONS_DRAFTED,
    RECOMMENDATIONS_RELEASED,
    REVIEWS_COMPLETED,
    REVIEWS_DECIDED,
    REVIEWS_REQUESTED,
    RUNS_PLANNED,
    SAFETY_BLOCKED,
)
from vitalmesh_stores import RunLog

TRACKED_TOPICS = (
    QUESTIONS_ASKED,
    RUNS_PLANNED,
    ADVICE_COMPLETED,
    ADVICE_BUNDLES,
    RECOMMENDATIONS_DRAFTED,
    DRAFTS_CLEARED,
    REVIEWS_COMPLETED,
    REVIEWS_REQUESTED,
    REVIEWS_DECIDED,
    RECOMMENDATIONS_RELEASED,
    SAFETY_BLOCKED,
)

_BASIS = {
    common_pb2.PROVENANCE_COMPUTED: "computed",
    common_pb2.PROVENANCE_CITED: "cited",
    common_pb2.PROVENANCE_INFERRED: "inferred",
}
_STATUS = {
    payloads_pb2.ADVICE_ANSWERED: "answered",
    payloads_pb2.ADVICE_FALLBACK: "fallback",
    payloads_pb2.ADVICE_DECLINED: "declined",
    payloads_pb2.ADVICE_FAILED: "failed",
}
_VERDICT = {
    payloads_pb2.REVIEW_APPROVED: "approve",
    payloads_pb2.REVIEW_REVISE: "revise",
    payloads_pb2.REVIEW_REJECTED: "reject",
}


def _claims(claims: Any) -> list[dict[str, str]]:
    return [{"text": c.text, "basis": _BASIS.get(c.provenance, "unknown")} for c in claims]


def _citations(cites: Any) -> list[dict[str, str]]:
    return [
        {
            "document": c.document,
            "locator": c.locator,
            "licence": c.licence,
            "url": c.source_url,
        }
        for c in cites
    ]


def entry_from_event(meta: EventMeta, payload: Any) -> dict[str, Any] | None:
    """The timeline entry for one event, or None for a type the timeline does not show."""
    entry: dict[str, Any] = {
        "event_id": meta.event_id,
        "at": meta.occurred_at.isoformat(),
        "type": meta.type,
        "tier": meta.tier.value if meta.tier else None,
    }
    t = meta.type
    if t == "question.asked":
        entry["question"] = payload.question
    elif t == "run.planned":
        entry.update(
            specialists=[specialist_from_proto(s).value for s in payload.specialists],
            risk=risk_from_proto(payload.risk).value,
            rules=list(payload.rule_ids),
            degraded=bool(payload.degraded),
        )
    elif t == "advice.completed":
        entry.update(
            specialist=specialist_from_proto(payload.specialist).value,
            status=_STATUS.get(payload.status, "unknown"),
            model_calls=int(payload.model_calls),
            failure=payload.failure_code,
        )
    elif t == "advice.bundle.ready":
        entry.update(
            partial=bool(payload.partial),
            missing=[specialist_from_proto(s).value for s in payload.missing],
            reason=payload.reason,
        )
    elif t == "recommendation.drafted":
        entry.update(revision=int(payload.revision), used_fallback=bool(payload.used_fallback))
    elif t == "safety.cleared":
        entry.update(rules_version=payload.rules_version)
    elif t == "recommendation.blocked":
        entry.update(rules=sorted({v.rule for v in payload.violations}))
    elif t == "review.completed":
        entry.update(
            verdict=_VERDICT.get(payload.verdict, "unknown"), problems=list(payload.problems)
        )
    elif t == "review.requested":
        entry.update(recommendation_id=payload.recommendation_id, reasons=list(payload.reasons))
    elif t == "review.decided":
        entry.update(
            decision="approve" if payload.decision == payloads_pb2.DECISION_APPROVE else "reject"
        )
    elif t == "recommendation.released":
        entry.update(
            text=payload.summary,
            partial=bool(payload.partial),
            missing=[specialist_from_proto(s).value for s in payload.missing],
            claims=_claims(payload.claims),
            citations=_citations(payload.citations),
        )
    else:
        return None
    return entry


def make_tracker_handler(log: RunLog) -> Handler:
    def handle(meta: EventMeta, payload: Any) -> None:
        entry = entry_from_event(meta, payload)
        if entry is not None:
            log.append(meta.correlation_id, meta.user_id, entry)

    return handle
