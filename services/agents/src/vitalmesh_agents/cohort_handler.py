"""T1 handlers: answer from the cohort cache, and fill it after a good full run (D70).

Two consumers, no model in either:

* ``make_cohort_handler`` reads ``run.planned`` with tier T1 (Triage saw a cache hit). It runs the
  user's OWN deterministic calculators (the same tools the specialists call), fills the cached
  template from them and emits ``recommendation.drafted``: the Guardian gate, release and the
  rest of the path are the unchanged T3 stages. If anything is off (entry gone, a calculator
  fails, the user's constraints changed since the plan) it does not guess: it re-emits the plan
  as T3 and the full graph runs, so a cache problem costs time, never correctness.
* ``make_fill_handler`` reads ``recommendations.released`` and stores a template for the cohort
  when the answer was a complete, low-risk, constraint-free run and the template is faithful.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from typing import Any

from vitalmesh.events.v1 import common_pb2, payloads_pb2
from vitalmesh_common.envelope import EventMeta, Tier, utcnow
from vitalmesh_common.processor import EventSerde, Handler, Publisher
from vitalmesh_common.run import Specialist, risk_from_proto, risk_to_proto, specialist_from_proto
from vitalmesh_common.topics import RECOMMENDATIONS_DRAFTED, RUNS_PLANNED
from vitalmesh_evidence import EvidenceAnswer
from vitalmesh_specialists.movement import fallback_text as movement_notes
from vitalmesh_specialists.nutrition import fallback_text as nutrition_notes
from vitalmesh_specialists.tools import ToolResult

from vitalmesh_agents.advice import claims_from_results
from vitalmesh_agents.cohort import (
    CITED,
    COMPUTED,
    ClaimView,
    build_template,
    personalise,
    plan_key,
)
from vitalmesh_agents.cohortstore import CohortEntry, CohortStore
from vitalmesh_agents.events import POST_BUNDLE_BUDGET_S, emit, next_meta
from vitalmesh_agents.guardian_gate import T3_RULES_VERSION
from vitalmesh_agents.specialist_handler import SpecialistDeps, build_tools
from vitalmesh_agents.synthesizer import PartView, assemble
from vitalmesh_agents.synthesizer_handler import recommendation_id
from vitalmesh_agents.triage import plan_run, signals_for

log = logging.getLogger("vitalmesh.cohort")

PLANNED_TYPE = "run.planned"
RELEASED_TYPE = "recommendation.released"

# The deterministic calls whose claims fill a template. Search is not here: cited sentences
# travel inside the template itself.
FILL_TOOLS: dict[Specialist, tuple[str, ...]] = {
    Specialist.NUTRITION: ("get_energy_target",),
    Specialist.MOVEMENT_RECOVERY: ("get_training_targets", "get_activity_summary"),
}


class DisabledEvidence:
    """The T1 roles call only deterministic tools, so they need no guideline search (and no
    embedding model): a search call here would be a bug, and it answers 'nothing found'."""

    def answer(self, question: str) -> EvidenceAnswer:
        return EvidenceAnswer(status="insufficient_evidence")


def view_of(claim: payloads_pb2.RecommendationClaim) -> ClaimView:
    computed = claim.provenance == common_pb2.PROVENANCE_COMPUTED
    cited = claim.provenance == common_pb2.PROVENANCE_CITED
    return ClaimView(
        text=claim.text,
        provenance=COMPUTED if computed else CITED if cited else "other",
        source=claim.source if claim.HasField("source") else "",
        metric=claim.metric if claim.HasField("metric") else "",
        value=claim.quantity.value if claim.HasField("quantity") else None,
        unit=claim.quantity.unit if claim.HasField("quantity") else "",
    )


def proto_of(view: ClaimView) -> payloads_pb2.RecommendationClaim:
    if view.provenance == CITED:
        return payloads_pb2.RecommendationClaim(
            text=view.text, provenance=common_pb2.PROVENANCE_CITED, source=view.source
        )
    claim = payloads_pb2.RecommendationClaim(
        text=view.text,
        provenance=common_pb2.PROVENANCE_COMPUTED,
        source=view.source,
        metric=view.metric,
    )
    claim.quantity.value = float(view.value or 0.0)
    claim.quantity.unit = view.unit
    return claim


def fresh_claims(
    kinds: Sequence[Specialist], deps: SpecialistDeps, meta: EventMeta, now: datetime
) -> list[ClaimView] | None:
    """The user's own computed claims, or None if any calculator cannot answer."""
    record = deps.profiles.get(meta.user_id)
    if record is None:
        return None
    results: list[ToolResult] = []
    for kind in kinds:
        tools = build_tools(kind, deps, record, meta.user_id, now)
        for name in FILL_TOOLS.get(kind, ()):
            result = tools.call(name, "{}")
            if not result.ok:
                return None
            results.append(result)
    seen: set[tuple[str, str]] = set()
    out: list[ClaimView] = []
    for claim in claims_from_results(results):  # same dedupe as the synthesizer's merge
        key = (claim.text, claim.source)
        if key not in seen:
            seen.add(key)
            out.append(view_of(claim))
    return out


def make_cohort_handler(
    deps: SpecialistDeps,
    store: CohortStore,
    serde: EventSerde,
    publisher: Publisher,
    clock: Callable[[], datetime] = utcnow,
) -> Handler:
    def handle(meta: EventMeta, payload: Any) -> None:
        if meta.type != PLANNED_TYPE or meta.tier not in (Tier.T0, Tier.T1):
            return
        now = clock()
        if meta.tier is Tier.T0:
            draft = _shed_answer(deps, meta, payload, now)
        else:
            draft = _answer(deps, store, meta, payload, now)
        if draft is None:
            store.count("personalise_failed")
            log.info("T1 fell back to a full run for %s", meta.correlation_id)
            out = next_meta(
                meta,
                PLANNED_TYPE,
                "run.planned.after-cache-miss",
                now,
                tier=Tier.T3,
                deadline_at=meta.deadline_at,
            )
            emit(serde, publisher, RUNS_PLANNED, out, payload)
        else:
            out = next_meta(
                meta,
                "recommendation.drafted",
                "recommendation.drafted.t1",
                now,
                deadline_at=now + timedelta(seconds=POST_BUNDLE_BUDGET_S),
            )
            emit(serde, publisher, RECOMMENDATIONS_DRAFTED, out, draft)
        publisher.flush()

    return handle


def _answer(
    deps: SpecialistDeps, store: CohortStore, meta: EventMeta, payload: Any, now: datetime
) -> payloads_pb2.RecommendationDrafted | None:
    record = deps.profiles.get(meta.user_id)
    if record is None:
        return None
    kinds = [specialist_from_proto(s) for s in payload.specialists]
    profile = record.profile
    signals = signals_for(profile.conditions, profile.medications, deps.labs.list(meta.user_id))
    plan = plan_run(payload.question, signals)
    key = plan_key(profile, signals, payload.question, plan, T3_RULES_VERSION)
    if key is None or set(plan.specialists) != set(kinds):
        return None  # the user's constraints changed since Triage planned: not a cohort answer
    entry = store.get(key)
    if entry is None:
        return None
    fresh = fresh_claims(kinds, deps, meta, now)
    if fresh is None:
        return None
    got = personalise(entry.template, fresh)
    if got is None:
        return None
    text, claims = got
    draft = payloads_pb2.RecommendationDrafted(
        recommendation_id=recommendation_id(meta.correlation_id),
        question=payload.question,
        text=text,
        risk=risk_to_proto(plan.risk),
        partial=False,
        revision=0,
        used_fallback=False,
    )
    draft.claims.extend(proto_of(c) for c in claims)
    for raw in entry.citations:
        draft.citations.append(payloads_pb2.Citation.FromString(raw))
    store.count("hit")
    return draft


def _shed_answer(
    deps: SpecialistDeps, meta: EventMeta, payload: Any, now: datetime
) -> payloads_pb2.RecommendationDrafted | None:
    """T0 under load: the specialists' own deterministic notes from the user's calculators.
    Same gate and release as every other answer; no model, no cache needed."""
    record = deps.profiles.get(meta.user_id)
    if record is None:
        return None
    kinds = [specialist_from_proto(s) for s in payload.specialists]
    if not set(kinds) <= set(FILL_TOOLS):
        return None
    results: dict[Specialist, list[ToolResult]] = {}
    for kind in kinds:
        tools = build_tools(kind, deps, record, meta.user_id, now)
        for name in FILL_TOOLS[kind]:
            result = tools.call(name, "{}")
            if not result.ok:
                return None
            results.setdefault(kind, []).append(result)
    notes = {
        Specialist.NUTRITION: nutrition_notes,
        Specialist.MOVEMENT_RECOVERY: movement_notes,
    }
    parts = [PartView(k.value, "fallback", notes[k](results[k])) for k in kinds]
    draft = payloads_pb2.RecommendationDrafted(
        recommendation_id=recommendation_id(meta.correlation_id),
        question=payload.question,
        text=assemble(parts),
        risk=risk_to_proto(risk_from_proto(payload.risk)),
        partial=False,
        revision=0,
        used_fallback=True,
    )
    draft.claims.extend(claims_from_results([r for rs in results.values() for r in rs]))
    return draft


def make_fill_handler(deps: SpecialistDeps, store: CohortStore) -> Handler:
    """Learn from a released answer. Idempotent: an existing entry is never rewritten."""

    def handle(meta: EventMeta, payload: Any) -> None:
        if meta.type != RELEASED_TYPE or payload.partial or not payload.question:
            return
        record = deps.profiles.get(meta.user_id)
        if record is None:
            return
        profile = record.profile
        signals = signals_for(profile.conditions, profile.medications, deps.labs.list(meta.user_id))
        plan = plan_run(payload.question, signals)
        key = plan_key(profile, signals, payload.question, plan, T3_RULES_VERSION)
        if key is None or store.get(key) is not None:
            return
        template = build_template(payload.summary, [view_of(c) for c in payload.claims])
        if template is None:
            store.count("uncacheable")
            return
        store.put(
            key, CohortEntry(template, tuple(c.SerializeToString() for c in payload.citations))
        )
        store.count("fill")

    return handle
