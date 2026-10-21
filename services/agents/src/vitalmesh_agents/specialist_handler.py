"""Specialist handler: ``run.planned`` -> run ONE specialist -> ``advice.completed`` (D68).

Every specialist is its own consumer group on ``runs.planned`` and acts only when the plan names
it, so a slow or dead specialist is isolated and scales on its own lag. Failures that the run can
survive are PUBLISHED as a FAILED advice rather than raised: the Aggregator learns at once that
the part will not come and closes the run with what it has instead of waiting for the deadline.
Unexpected errors are raised so the processor retries them (bounded, then the DLQ).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from vitalmesh_common.envelope import EventMeta, utcnow
from vitalmesh_common.processor import EventSerde, Handler, Publisher
from vitalmesh_common.run import Specialist, specialist_from_proto
from vitalmesh_common.topics import ADVICE_COMPLETED
from vitalmesh_specialists import (
    ConditionSpecialist,
    ConditionTools,
    MovementSpecialist,
    MovementTools,
    NutritionSpecialist,
    NutritionTools,
    SpecialistUnavailable,
    ToolSpecialist,
)
from vitalmesh_specialists.agent import RouterLike, ToolsLike
from vitalmesh_specialists.tools import EvidenceLike
from vitalmesh_stores import HistoryStore, LabStore, ProfileRecord, ProfileStore
from vitalmesh_tracing import NULL, Tracer

from vitalmesh_agents.advice import advice_to_proto, failed_advice
from vitalmesh_agents.events import emit, next_meta

INPUT_TYPE = "run.planned"
OUTPUT_TYPE = "advice.completed"


@dataclass
class SpecialistDeps:
    """What a specialist needs from outside: the model router, the Evidence agent and the stores.
    Provider trouble is retried briefly: a run has a deadline, so the 30-second pauses of an
    interactive specialist would burn it."""

    router: RouterLike
    evidence: EvidenceLike
    profiles: ProfileStore
    labs: LabStore
    history: HistoryStore
    tracer: Tracer = NULL
    attempts: int = 2
    retry_pause_s: float = 5.0
    max_wait_s: float = 60.0
    sleep: Callable[[float], None] = field(default=time.sleep)


SCOPE = {
    Specialist.NUTRITION: "food and nutrition",
    Specialist.MOVEMENT_RECOVERY: "exercise, training and sleep",
    Specialist.CONDITION_MEDICATION: "declared conditions, medications and lab results",
}
MIN_WAIT_S = 10.0
MAX_WAIT_S = 240.0
DEADLINE_MARGIN_S = 15.0


def scoped_question(kind: Specialist, question: str) -> str:
    """Each specialist answers only its own part: overlap wastes quota and contradicts."""
    return (
        f"{question}\n\n(You are the {SCOPE[kind]} specialist. Answer only the "
        f"{SCOPE[kind]} part of this question; other specialists cover the rest.)"
    )


def wait_budget(deps: SpecialistDeps, deadline: datetime | None, now: datetime) -> float:
    """How long a model call may wait for free-tier budget: what is left of the run deadline,
    less a margin to publish, so a throttled specialist still reports before the run closes."""
    if deadline is None:
        return deps.max_wait_s
    left = (deadline - now).total_seconds() - DEADLINE_MARGIN_S
    return max(MIN_WAIT_S, min(left, MAX_WAIT_S))


def build_specialist(
    kind: Specialist, deps: SpecialistDeps, max_calls: int, max_wait_s: float | None = None
) -> ToolSpecialist:
    common: dict[str, Any] = {
        "tracer": deps.tracer,
        "attempts": deps.attempts,
        "retry_pause_s": deps.retry_pause_s,
        "max_wait_s": deps.max_wait_s if max_wait_s is None else max_wait_s,
        "sleep": deps.sleep,
        "max_model_calls": max_calls,
    }
    if kind is Specialist.NUTRITION:
        return NutritionSpecialist(deps.router, **common)
    if kind is Specialist.MOVEMENT_RECOVERY:
        return MovementSpecialist(deps.router, **common)
    return ConditionSpecialist(deps.router, **common)


def build_tools(
    kind: Specialist, deps: SpecialistDeps, record: ProfileRecord, user_id: str, now: datetime
) -> ToolsLike:
    profile, sleep = record.profile, list(record.recent_sleep_hours)
    if kind is Specialist.NUTRITION:
        return NutritionTools(profile, sleep, deps.evidence, now.date())
    if kind is Specialist.MOVEMENT_RECOVERY:
        return MovementTools(profile, sleep, deps.history, deps.evidence, now)
    return ConditionTools(profile, deps.labs.list(user_id), deps.evidence)


def make_specialist_handler(
    kind: Specialist,
    deps: SpecialistDeps,
    serde: EventSerde,
    publisher: Publisher,
    clock: Callable[[], datetime] = utcnow,
) -> Handler:
    def handle(meta: EventMeta, payload: Any) -> None:
        if meta.type != INPUT_TYPE:
            return
        if kind not in {specialist_from_proto(s) for s in payload.specialists}:
            return  # this run does not involve me

        now = clock()
        record = deps.profiles.get(meta.user_id)
        if record is None:
            advice = failed_advice(kind, "profile.missing")
        else:
            specialist = build_specialist(
                kind,
                deps,
                int(payload.max_model_calls) or 1,
                wait_budget(deps, meta.deadline_at, now),
            )
            tools = build_tools(kind, deps, record, meta.user_id, now)
            try:
                result = specialist.advise(
                    scoped_question(kind, payload.question),
                    tools,
                    user_id=meta.user_id,
                    session_id=meta.correlation_id,
                )
                advice = advice_to_proto(kind, result)
            except SpecialistUnavailable:
                advice = failed_advice(kind, "llm.unavailable")

        out_meta = next_meta(
            meta,
            OUTPUT_TYPE,
            f"advice.{kind.value}",
            now,
            deadline_at=meta.deadline_at,  # the run's deadline still governs the fan-out
        )
        emit(serde, publisher, ADVICE_COMPLETED, out_meta, advice)
        publisher.flush()

    return handle
