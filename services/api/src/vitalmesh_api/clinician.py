"""The clinician review console's routes (D85).

A clinician is the one human who may read a held answer before its owner does, so this is the one
place health content is shown to someone other than its owner. The rules that follow from that:

* only the ``clinician`` role reaches ``/v1/clinician/`` (the gate checks it; operators do not);
* a clinician never sees who the person is: cases are identified by recommendation id alone, and
  the person's id never leaves the server;
* the LIST carries no content (reason codes and times only); opening a case shows the content and is
  audited, and so is every decision (the audit holds ids and the decision, never the note);
* a decision is an ordinary event (``review.decided``) on the run's own correlation id, so the
  paused run resumes exactly as it does for the command-line stand-in; it is accepted only for a
  case that is still pending (409 otherwise), and the pipeline ignores a second one anyway.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from vitalmesh.events.v1 import payloads_pb2
from vitalmesh_accounts import APPROVED, PENDING, REJECTED, ReviewCase, ReviewQueue
from vitalmesh_common.envelope import EventMeta
from vitalmesh_common.failure import EnvelopeError
from vitalmesh_common.processor import EventSerde, Publisher
from vitalmesh_common.topics import REVIEWS_DECIDED

from vitalmesh_api.security import Auth

_NAMESPACE = uuid.UUID("5b0f2a9e-71c4-4d3b-8e6a-3c9d1f7a2b84")
_REC_ID = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")]


class CaseSummary(BaseModel):
    """What the queue shows: no question, no draft, no person."""

    recommendation_id: str
    requested_at: datetime
    status: str
    risk: str
    reasons: list[str]
    decided_at: datetime | None


class CaseClaim(BaseModel):
    text: str
    basis: str  # computed | cited | inferred | unknown


class CaseSource(BaseModel):
    document: str
    locator: str


class CaseDetail(CaseSummary):
    question: str
    briefing: str
    draft_text: str
    partial: bool
    claims: list[CaseClaim]
    sources: list[CaseSource]
    note: str
    decided_by_me: bool


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject"]
    note: Annotated[str, Field(max_length=500)] = ""


class DecisionOut(BaseModel):
    recommendation_id: str
    status: str


def _summary(c: ReviewCase) -> CaseSummary:
    return CaseSummary(
        recommendation_id=c.recommendation_id,
        requested_at=c.requested_at,
        status=c.status,
        risk=str(c.draft.get("risk", "unknown")),
        reasons=list(c.reasons),
        decided_at=c.decided_at,
    )


def _detail(c: ReviewCase, me: str) -> CaseDetail:
    d: dict[str, Any] = c.draft
    return CaseDetail(
        **_summary(c).model_dump(),
        question=c.question,
        briefing=c.briefing,
        draft_text=str(d.get("text", "")),
        partial=bool(d.get("partial", False)),
        claims=[CaseClaim(text=x["text"], basis=x["basis"]) for x in d.get("claims", [])],
        sources=[
            CaseSource(document=x["document"], locator=x["locator"]) for x in d.get("citations", [])
        ],
        note=c.note,
        decided_by_me=c.decided_by == me,
    )


def install_clinician(
    app: FastAPI,
    reviews: ReviewQueue,
    serde: EventSerde,
    publisher: Publisher,
    clock: Callable[[], datetime],
    auth: Auth | None,
) -> None:
    def who(request: Request) -> str:
        claims = getattr(request.state, "claims", None)
        return str(claims.user_id) if claims is not None else "clinician-demo"

    def audit(request: Request, action: str, **detail: Any) -> None:
        if auth is not None:
            auth.audit(request, who(request), action, **detail)

    @app.get("/v1/clinician/reviews", response_model=list[CaseSummary])
    def list_cases(
        status: Literal["pending", "decided"] = "pending", limit: int = 50
    ) -> list[CaseSummary]:
        return [_summary(c) for c in reviews.cases(status, max(1, min(limit, 200)))]

    def load(recommendation_id: str) -> ReviewCase:
        case = reviews.get(recommendation_id)
        if case is None or not case.has_detail:
            raise HTTPException(status_code=404, detail="no such review")
        return case

    @app.get("/v1/clinician/reviews/{recommendation_id}", response_model=CaseDetail)
    def open_case(recommendation_id: _REC_ID, request: Request) -> CaseDetail:
        case = load(recommendation_id)
        audit(request, "review_viewed", recommendation_id=recommendation_id)
        return _detail(case, who(request))

    @app.post("/v1/clinician/reviews/{recommendation_id}/decision", response_model=DecisionOut)
    def decide(recommendation_id: _REC_ID, body: DecisionIn, request: Request) -> DecisionOut:
        case = load(recommendation_id)
        owner = reviews.owner(recommendation_id)
        if case.status != PENDING or owner is None:
            raise HTTPException(status_code=409, detail="this review was already decided")
        reviewer = who(request)
        approve = body.decision == "approve"
        meta = EventMeta(
            event_id=str(uuid.uuid5(_NAMESPACE, f"decided:{recommendation_id}")),
            correlation_id=case.correlation_id or recommendation_id,
            user_id=owner,  # keeps the person's events on one partition; never shown to anyone
            household_id="default",
            type="review.decided",
            occurred_at=clock(),
        )
        msg = payloads_pb2.ReviewDecided(
            recommendation_id=recommendation_id,
            decision=payloads_pb2.DECISION_APPROVE if approve else payloads_pb2.DECISION_REJECT,
            reviewer_id=reviewer,
            note=body.note,
        )
        try:
            publisher.publish(
                REVIEWS_DECIDED, owner, serde.serialize_event(REVIEWS_DECIDED, meta, msg)
            )
            publisher.flush()
        except EnvelopeError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:  # broker down: nothing was decided, the clinician can retry
            raise HTTPException(status_code=503, detail="temporarily unavailable") from exc
        status = APPROVED if approve else REJECTED
        # record it now so the queue and a double click agree (the tracker records it again)
        reviews.record_decision(
            recommendation_id, status, reviewer, clock(), body.note, user_id=owner
        )
        audit(
            request, "review_decided", recommendation_id=recommendation_id, decision=body.decision
        )
        return DecisionOut(recommendation_id=recommendation_id, status=status)
