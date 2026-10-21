"""Synthesizer handler (D68): ``advice.bundle.ready`` -> ``recommendation.drafted``, and the one
rewrite the Checker may ask for (``review.completed`` with verdict REVISE on revision 0).

The draft carries every claim and citation of the bundle unchanged (provenance preserved) next to
the merged text. When the model cannot produce a checked text, or is unavailable, the text is the
specialists' own checked notes: degraded, never invented. A partial bundle always gets a fixed
disclosure sentence appended by code, so the answer never reads as complete when it is not.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from vitalmesh.events.v1 import payloads_pb2
from vitalmesh_common.envelope import EventMeta, utcnow
from vitalmesh_common.processor import EventSerde, Handler, Publisher
from vitalmesh_common.run import (
    Risk,
    Specialist,
    risk_from_proto,
    risk_to_proto,
    specialist_from_proto,
    specialist_to_proto,
)
from vitalmesh_common.topics import RECOMMENDATIONS_DRAFTED

from vitalmesh_agents.advice import claim_numbers
from vitalmesh_agents.events import POST_BUNDLE_BUDGET_S, derived_id, emit, next_meta
from vitalmesh_agents.synthesizer import (
    NO_ANSWER,
    AgentUnavailable,
    PartView,
    Synthesizer,
    SynthInput,
    assemble,
)

OUTPUT_TYPE = "recommendation.drafted"
_STATUS = {
    payloads_pb2.ADVICE_ANSWERED: "answered",
    payloads_pb2.ADVICE_FALLBACK: "fallback",
    payloads_pb2.ADVICE_DECLINED: "declined",
}


def recommendation_id(correlation_id: str) -> str:
    return derived_id(correlation_id, "recommendation")


def partial_note(missing: list[Specialist]) -> str:
    names = ", ".join(m.value.replace("_", " ") for m in missing)
    return (
        f"Note: this answer is partial. The {names} check did not finish in time."
        if names
        else "Note: this answer is partial."
    )


def _dedupe_claims(
    parts: list[payloads_pb2.AdviceCompleted],
) -> list[payloads_pb2.RecommendationClaim]:
    seen: set[tuple[str, str]] = set()
    out: list[payloads_pb2.RecommendationClaim] = []
    for p in parts:
        for c in p.claims:
            key = (c.text, c.source)
            if key not in seen:
                seen.add(key)
                out.append(c)
    return out


def _dedupe_citations(
    parts: list[payloads_pb2.AdviceCompleted],
) -> list[payloads_pb2.Citation]:
    seen: set[tuple[str, str, str]] = set()
    out: list[payloads_pb2.Citation] = []
    for p in parts:
        for c in p.citations:
            key = (c.document, c.locator, c.quote)
            if key not in seen:
                seen.add(key)
                out.append(c)
    return out


def make_synthesizer_handler(
    synthesizer: Synthesizer,
    serde: EventSerde,
    publisher: Publisher,
    clock: Callable[[], datetime] = utcnow,
) -> Handler:
    def handle(meta: EventMeta, payload: Any) -> None:
        now = clock()
        if meta.type == "advice.bundle.ready":
            draft = _first_draft(synthesizer, meta, payload)
        elif meta.type == "review.completed":
            if payload.verdict != payloads_pb2.REVIEW_REVISE or payload.draft.revision != 0:
                return  # not mine: only one rewrite is ever allowed
            draft = _rewrite(synthesizer, meta, payload)
        else:
            return
        out = next_meta(
            meta,
            OUTPUT_TYPE,
            f"recommendation.drafted.r{draft.revision}",
            now,
            deadline_at=now + timedelta(seconds=POST_BUNDLE_BUDGET_S),
        )
        emit(serde, publisher, RECOMMENDATIONS_DRAFTED, out, draft)
        publisher.flush()

    return handle


def _first_draft(
    synthesizer: Synthesizer, meta: EventMeta, bundle: Any
) -> payloads_pb2.RecommendationDrafted:
    parts = [p for p in bundle.parts if p.status in _STATUS]
    views = tuple(
        PartView(specialist_from_proto(p.specialist).value, _STATUS[p.status], p.text)
        for p in parts
    )
    claims = _dedupe_claims(parts)
    missing = [specialist_from_proto(s) for s in bundle.missing]
    usable = [v for v in views if v.status != "declined" or v.text.strip()]
    if not usable:
        text, fallback = NO_ANSWER, True
    else:
        inp = SynthInput(
            question=bundle.question,
            risk=risk_from_proto(bundle.risk).value,
            parts=tuple(usable),
            facts=tuple(c.text for c in claims),
            allowed_numbers=frozenset(claim_numbers(claims)),
            missing=tuple(m.value for m in missing),
        )
        try:
            result = synthesizer.synthesize(
                inp, user_id=meta.user_id, session_id=meta.correlation_id
            )
            text, fallback = result.text, result.used_fallback
        except AgentUnavailable:
            text, fallback = assemble(usable), True  # checked notes, no model needed
    return _draft(
        meta,
        bundle.question,
        risk_from_proto(bundle.risk),
        text,
        fallback,
        claims,
        _dedupe_citations(parts),
        bool(bundle.partial),
        missing,
        revision=0,
    )


def _rewrite(
    synthesizer: Synthesizer, meta: EventMeta, review: Any
) -> payloads_pb2.RecommendationDrafted:
    prev = review.draft
    claims = list(prev.claims)
    missing = [specialist_from_proto(s) for s in prev.missing]
    body = prev.text
    inp = SynthInput(
        question=prev.question,
        risk=risk_from_proto(prev.risk).value,
        parts=(PartView("previous-draft", "answered", body),),
        facts=tuple(c.text for c in claims),
        allowed_numbers=frozenset(claim_numbers(claims)),
        missing=tuple(m.value for m in missing),
        problems=tuple(review.problems),
        previous_text=body,
    )
    try:
        result = synthesizer.synthesize(inp, user_id=meta.user_id, session_id=meta.correlation_id)
        text, fallback = result.text, result.used_fallback
    except AgentUnavailable:
        text, fallback = body, True
    return _draft(
        meta,
        prev.question,
        risk_from_proto(prev.risk),
        text,
        fallback,
        claims,
        list(prev.citations),
        prev.partial,
        missing,
        revision=1,
    )


def _draft(
    meta: EventMeta,
    question: str,
    risk: Risk,
    text: str,
    used_fallback: bool,
    claims: list[payloads_pb2.RecommendationClaim],
    citations: list[payloads_pb2.Citation],
    partial: bool,
    missing: list[Specialist],
    *,
    revision: int,
) -> payloads_pb2.RecommendationDrafted:
    final = text
    if partial and "partial" not in text.lower():
        final = (
            f"{text} {partial_note(missing)}"  # appended by code: the disclosure is not optional
        )
    draft = payloads_pb2.RecommendationDrafted(
        recommendation_id=recommendation_id(meta.correlation_id),
        question=question,
        text=final,
        risk=risk_to_proto(risk),
        partial=partial,
        revision=revision,
        used_fallback=used_fallback,
    )
    draft.claims.extend(claims)
    draft.citations.extend(citations)
    draft.missing.extend(specialist_to_proto(m) for m in missing)
    return draft
