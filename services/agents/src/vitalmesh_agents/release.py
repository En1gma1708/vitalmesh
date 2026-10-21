"""Release gate: the ONLY publisher of a T3 ``recommendation.released``, with event-based human
approval (D68).

It runs a LangGraph graph per recommendation, checkpointed in Postgres (thread id = the
recommendation id), so a pending human review survives a restart and any worker can resume it:

    start --auto--> release
    start --escalate--> brief -> request -> wait (interrupt) -> finalize

* ``auto``: a low-risk draft the Guardian cleared, or a medium-risk one the Checker approved.
* ``escalate``: high risk, a Checker rejection, or a Checker revision that did not fix it.
  ``brief`` builds the case file, ``request`` publishes ``review.requested`` (a side effect, so it
  is its own node BEFORE the interrupt: a resume re-runs only ``wait``), ``wait`` pauses, and a
  ``review.decided`` event resumes it. No worker is held while a human thinks.

Before anything is released the draft goes through the Guardian gate AGAIN (``final_check``): the
release step trusts no earlier stage, including a human approval, which cannot override a rule.
A human REJECT publishes ``recommendation.blocked``; an unsafe approved draft is blocked likewise.
All output ids are derived from the recommendation id, so a redelivery re-publishes the same ids.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph
from langgraph.types import Command, interrupt
from vitalmesh.events.v1 import payloads_pb2
from vitalmesh_common.envelope import EventMeta, Tier, utcnow
from vitalmesh_common.processor import EventSerde, Publisher
from vitalmesh_common.topics import RECOMMENDATIONS_RELEASED, REVIEWS_REQUESTED, SAFETY_BLOCKED
from vitalmesh_stores import ProfileStore
from vitalmesh_wellness.guardian import Guardian

from vitalmesh_agents.advice import claim_numbers
from vitalmesh_agents.briefer import Briefer, CaseInput
from vitalmesh_agents.events import derived_id, emit, next_meta
from vitalmesh_agents.guardian_gate import GateVerdict, evaluate_draft
from vitalmesh_agents.guardian_handler import to_blocked

ENGINE_VERSION = "t3-1"
RELEASED_TYPE = "recommendation.released"
BLOCKED_TYPE = "recommendation.blocked"
REQUESTED_TYPE = "review.requested"


class _State(TypedDict, total=False):
    mode: str  # "auto" | "escalate"
    draft: bytes
    rules_version: str
    reasons: list[str]
    problems: list[str]
    ctx: dict[str, Any]  # the triggering envelope: ids and routing only
    briefing: str
    decision: dict[str, str]
    outcome: str  # released | blocked


def _ctx(meta: EventMeta) -> dict[str, Any]:
    return {
        "event_id": meta.event_id,
        "correlation_id": meta.correlation_id,
        "user_id": meta.user_id,
        "household_id": meta.household_id,
        "trace_id": meta.trace_id,
        "tier": meta.tier.value if meta.tier else None,
    }


def _parent(ctx: dict[str, Any], now: datetime, type_: str) -> EventMeta:
    return EventMeta(
        event_id=ctx["event_id"],
        correlation_id=ctx["correlation_id"],
        user_id=ctx["user_id"],
        household_id=ctx["household_id"],
        type=type_,
        occurred_at=now,
        trace_id=ctx["trace_id"],
        tier=Tier(ctx["tier"]) if ctx["tier"] else None,
    )


def _draft(state: _State) -> Any:
    d = payloads_pb2.RecommendationDrafted()
    d.ParseFromString(state["draft"])
    return d


class ReleaseGate:
    def __init__(
        self,
        profiles: ProfileStore,
        briefer: Briefer,
        serde: EventSerde,
        publisher: Publisher,
        checkpointer: Any,
        guardian: Guardian | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._profiles = profiles
        self._briefer = briefer
        self._serde = serde
        self._publisher = publisher
        self._guardian = guardian
        self._clock = clock
        self._graph = self._build(checkpointer)

    # ----------------------------------------------------------------------- graph
    def _build(self, checkpointer: Any) -> Any:
        g = StateGraph(_State)
        g.add_node("release", self._release)
        g.add_node("brief", self._brief)
        g.add_node("request", self._request)
        g.add_node("wait", self._wait)
        g.add_node("finalize", self._finalize)
        g.set_conditional_entry_point(
            lambda s: "release" if s["mode"] == "auto" else "brief", ["release", "brief"]
        )
        g.add_edge("release", END)
        g.add_edge("brief", "request")
        g.add_edge("request", "wait")
        g.add_edge("wait", "finalize")
        g.add_edge("finalize", END)
        return g.compile(checkpointer=checkpointer)

    def _final_check(self, draft: Any, user_id: str) -> GateVerdict:
        record = self._profiles.get(user_id)
        return evaluate_draft(
            draft, record.profile.constraints() if record else None, self._guardian
        )

    def _publish_final(self, state: _State) -> str:
        draft = _draft(state)
        ctx = state["ctx"]
        now = self._clock()
        verdict = self._final_check(draft, ctx["user_id"])
        rec_id = draft.recommendation_id
        if not verdict.cleared:
            meta = next_meta(
                _parent(ctx, now, "review.completed"),
                BLOCKED_TYPE,
                "release.blocked",
                now,
                rules_version=verdict.rules_version,
                event_id=derived_id(rec_id, "release.blocked"),
            )
            emit(self._serde, self._publisher, SAFETY_BLOCKED, meta, to_blocked(draft, verdict))
            self._publisher.flush()
            return "blocked"
        released = payloads_pb2.RecommendationReleased(
            recommendation_id=rec_id,
            engine_version=ENGINE_VERSION,
            rules_version=state["rules_version"],
            summary=draft.text,
            partial=draft.partial,
            question=draft.question,
        )
        released.claims.extend(draft.claims)
        released.citations.extend(draft.citations)
        released.missing.extend(draft.missing)
        released.generated_at.FromDatetime(now.replace(tzinfo=None))
        meta = next_meta(
            _parent(ctx, now, "review.completed"),
            RELEASED_TYPE,
            "released",
            now,
            rules_version=state["rules_version"],
            event_id=derived_id(rec_id, "released"),
        )
        emit(self._serde, self._publisher, RECOMMENDATIONS_RELEASED, meta, released)
        self._publisher.flush()
        return "released"

    def _release(self, state: _State) -> _State:
        return {"outcome": self._publish_final(state)}

    def _brief(self, state: _State) -> _State:
        draft = _draft(state)
        case = CaseInput(
            question=draft.question,
            reply=draft.text,
            facts=tuple(c.text for c in draft.claims),
            allowed_numbers=frozenset(claim_numbers(list(draft.claims))),
            reasons=tuple(state["reasons"]),
            problems=tuple(state["problems"]),
            partial=draft.partial,
        )
        ctx = state["ctx"]
        brief = self._briefer.brief(case, user_id=ctx["user_id"], session_id=ctx["correlation_id"])
        return {"briefing": brief.text}

    def _request(self, state: _State) -> _State:
        draft = _draft(state)
        ctx = state["ctx"]
        now = self._clock()
        msg = payloads_pb2.ReviewRequested(
            recommendation_id=draft.recommendation_id,
            briefing=state["briefing"],
            rules_version=state["rules_version"],
        )
        msg.reasons.extend(state["reasons"])
        msg.draft.CopyFrom(draft)
        meta = next_meta(
            _parent(ctx, now, "review.completed"),
            REQUESTED_TYPE,
            "review.requested",
            now,
            rules_version=state["rules_version"],
            event_id=derived_id(draft.recommendation_id, "review.requested"),
        )
        emit(self._serde, self._publisher, REVIEWS_REQUESTED, meta, msg)
        self._publisher.flush()
        return {}

    def _wait(self, state: _State) -> _State:
        decision = interrupt({"awaiting": "review.decided"})  # durable pause; resumed by event
        return {"decision": decision}

    def _finalize(self, state: _State) -> _State:
        if state["decision"].get("decision") != "approve":
            draft = _draft(state)
            ctx = state["ctx"]
            now = self._clock()
            blocked = payloads_pb2.RecommendationBlocked(
                recommendation_id=draft.recommendation_id, rules_version=state["rules_version"]
            )
            blocked.violations.add(
                rule="human_review_rejected", message="rejected by a human reviewer"
            )
            meta = next_meta(
                _parent(ctx, now, "review.decided"),
                BLOCKED_TYPE,
                "human.blocked",
                now,
                rules_version=state["rules_version"],
                event_id=derived_id(draft.recommendation_id, "human.blocked"),
            )
            emit(self._serde, self._publisher, SAFETY_BLOCKED, meta, blocked)
            self._publisher.flush()
            return {"outcome": "blocked"}
        return {"outcome": self._publish_final(state)}

    # --------------------------------------------------------------------- entry points
    @staticmethod
    def _config(recommendation_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": recommendation_id}}

    def _known(self, recommendation_id: str) -> Any:
        """The stored thread state, or None. A thread that has any checkpoint has been started
        (and is either waiting for a human or finished): starting it again would repeat effects."""
        snap = self._graph.get_state(self._config(recommendation_id))
        return snap if snap and snap.values else None

    def release_auto(self, meta: EventMeta, draft: Any, rules_version: str) -> bool:
        return self._start("auto", meta, draft, rules_version, [], [])

    def escalate(
        self,
        meta: EventMeta,
        draft: Any,
        rules_version: str,
        reasons: Sequence[str],
        problems: Sequence[str] = (),
    ) -> bool:
        return self._start("escalate", meta, draft, rules_version, list(reasons), list(problems))

    def _start(
        self,
        mode: str,
        meta: EventMeta,
        draft: Any,
        rules_version: str,
        reasons: list[str],
        problems: list[str],
    ) -> bool:
        if self._known(draft.recommendation_id) is not None:
            return False  # redelivery: already released, blocked or waiting for a human
        self._graph.invoke(
            {
                "mode": mode,
                "draft": draft.SerializeToString(),
                "rules_version": rules_version,
                "reasons": reasons,
                "problems": problems,
                "ctx": _ctx(meta),
            },
            self._config(draft.recommendation_id),
        )
        return True

    def pending(self, recommendation_id: str) -> bool:
        snap = self._known(recommendation_id)
        return bool(snap and snap.next)

    def resume(self, decided: Any) -> bool:
        """Apply a human decision. Ignored (False) unless this recommendation is waiting for one."""
        if not self.pending(decided.recommendation_id):
            return False
        self._graph.invoke(
            Command(
                resume={
                    "decision": "approve"
                    if decided.decision == payloads_pb2.DECISION_APPROVE
                    else "reject",
                    "reviewer_id": decided.reviewer_id,
                }
            ),
            self._config(decided.recommendation_id),
        )
        return True
