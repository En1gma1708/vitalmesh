"""Specialist handler: ``run.planned`` -> run ONE specialist -> ``advice.completed`` (D68).

Every specialist is its own consumer group on ``runs.planned`` and acts only when the plan names
it, so a slow or dead specialist is isolated and scales on its own lag. Failures that the run can
survive are PUBLISHED as a FAILED advice rather than raised: the Aggregator learns at once that
the part will not come and closes the run with what it has instead of waiting for the deadline.
Unexpected errors are raised so the processor retries them (bounded, then the DLQ).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from vitalmesh_common.envelope import EventMeta, Tier, utcnow
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
    ProgressSpecialist,
    ProgressTools,
    SpecialistUnavailable,
    ToolSpecialist,
)
from vitalmesh_specialists.agent import SCOPE_MARKER, RouterLike, ToolsLike
from vitalmesh_specialists.tools import EvidenceLike
from vitalmesh_stores import HistoryStore, LabStore, ProfileRecord, ProfileStore, WorkoutLog
from vitalmesh_tracing import NULL, Tracer

from vitalmesh_agents.advice import advice_to_proto, failed_advice
from vitalmesh_agents.events import emit, next_meta

log = logging.getLogger("vitalmesh.specialist")

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
    workouts: WorkoutLog | None = None  # what workout was last suggested (D104)
    attempts: int = 2
    retry_pause_s: float = 5.0
    max_wait_s: float = 60.0
    sleep: Callable[[float], None] = field(default=time.sleep)


SCOPE = {
    Specialist.NUTRITION: "food and nutrition",
    Specialist.MOVEMENT_RECOVERY: "exercise, training and sleep",
    Specialist.CONDITION_MEDICATION: "declared conditions, medications and lab results",
    Specialist.PROGRESS: "everyday progress, sleep, weight and BMI",
}
MIN_WAIT_S = 10.0
# one model call may wait this long for a provider's rate limit; a longer wait (a daily token
# cap asks for minutes) fails over to the next provider at once (D102)
MAX_WAIT_S = 60.0
DEADLINE_MARGIN_S = 15.0
# model work stops this long before the run's deadline, so the helper can still answer from its
# pure-code tools and publish before the aggregator closes the run without it (D100)
FALLBACK_RESERVE_S = 25.0


def scoped_question(kind: Specialist, question: str) -> str:
    """Each specialist answers only its own part: overlap wastes quota and contradicts."""
    return (
        f"{question}{SCOPE_MARKER}{SCOPE[kind]} specialist. Answer only the "
        f"{SCOPE[kind]} part of this question; other specialists cover the rest.)"
    )


def wait_budget(deps: SpecialistDeps, deadline: datetime | None, now: datetime) -> float:
    """How long a model call may wait for free-tier budget: what is left of the run deadline,
    less a margin to publish, so a throttled specialist still reports before the run closes."""
    if deadline is None:
        return deps.max_wait_s
    left = (deadline - now).total_seconds() - DEADLINE_MARGIN_S
    return max(MIN_WAIT_S, min(left, MAX_WAIT_S))


def time_budget(deadline: datetime | None, now: datetime) -> float | None:
    """How long a helper may keep asking the model: until shortly before the run's deadline."""
    if deadline is None:
        return None
    return max(MIN_WAIT_S, (deadline - now).total_seconds() - FALLBACK_RESERVE_S)


def build_specialist(
    kind: Specialist,
    deps: SpecialistDeps,
    max_calls: int,
    max_wait_s: float | None = None,
    budget_s: float | None = None,
) -> ToolSpecialist:
    common: dict[str, Any] = {
        "tracer": deps.tracer,
        "attempts": deps.attempts,
        "retry_pause_s": deps.retry_pause_s,
        "max_wait_s": deps.max_wait_s if max_wait_s is None else max_wait_s,
        "sleep": deps.sleep,
        "max_model_calls": max_calls,
        "time_budget_s": budget_s,
    }
    if kind is Specialist.NUTRITION:
        return NutritionSpecialist(deps.router, **common)
    if kind is Specialist.MOVEMENT_RECOVERY:
        return MovementSpecialist(deps.router, **common)
    if kind is Specialist.PROGRESS:
        return ProgressSpecialist(deps.router, **common)
    return ConditionSpecialist(deps.router, **common)


RECENT_NIGHTS = 14
MIN_LOGGED_NIGHTS = 3


def recent_sleep(
    record: ProfileRecord, history: HistoryStore, user_id: str, now: datetime
) -> list[float]:
    """The person's own last nights when they have logged enough of them, else the stored
    neutral value. The stored value is a constant seven hours, which reads as a 14-hour sleep debt
    for everyone: every answer said "keep it easy" and no workout day could ever be a strength day
    (D103)."""
    try:
        points = history.series(user_id, "sleep_duration", now - timedelta(days=RECENT_NIGHTS))
    except Exception:  # a history outage must not stop the answer
        points = []
    nights = [p.value for p in points if 0.0 <= p.value <= 24.0][-RECENT_NIGHTS:]
    return nights if len(nights) >= MIN_LOGGED_NIGHTS else list(record.recent_sleep_hours)


def build_tools(
    kind: Specialist, deps: SpecialistDeps, record: ProfileRecord, user_id: str, now: datetime
) -> ToolsLike:
    profile = record.profile
    sleep = recent_sleep(record, deps.history, user_id, now)
    if kind is Specialist.NUTRITION:
        return NutritionTools(profile, sleep, deps.evidence, now.date())
    if kind is Specialist.MOVEMENT_RECOVERY:
        return MovementTools(
            profile, sleep, deps.history, deps.evidence, now, workouts=deps.workouts
        )
    if kind is Specialist.PROGRESS:
        return ProgressTools(profile, deps.history, now)
    return ConditionTools(profile, deps.labs.list(user_id), deps.evidence)


def make_specialist_handler(
    kind: Specialist,
    deps: SpecialistDeps,
    serde: EventSerde,
    publisher: Publisher,
    clock: Callable[[], datetime] = utcnow,
) -> Handler:
    def handle(meta: EventMeta, payload: Any) -> None:
        if meta.type != INPUT_TYPE or meta.tier in (Tier.T0, Tier.T1):
            return  # a cache or shed run needs no specialist
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
                time_budget(meta.deadline_at, now),
            )
            tools = build_tools(kind, deps, record, meta.user_id, now)
            try:
                result = specialist.advise_or_degrade(
                    scoped_question(kind, payload.question),
                    tools,
                    user_id=meta.user_id,
                    session_id=meta.correlation_id,
                )
                if result.status == "fallback" and result.model_calls == 0:
                    # why the model could not be used (provider answers and our own budget wait)
                    log.info("%s answered from tools only: %s", kind.value, result.reason)
                advice = advice_to_proto(kind, result)
            except SpecialistUnavailable:
                advice = failed_advice(kind, "llm.unavailable")

        out_meta = next_meta(
            meta,
            OUTPUT_TYPE,
            f"advice.{kind.value}",
            clock(),  # stamped when the work FINISHED, not when it started (the console reads it)
            deadline_at=meta.deadline_at,  # the run's deadline still governs the fan-out
        )
        emit(serde, publisher, ADVICE_COMPLETED, out_meta, advice)
        publisher.flush()

    return handle
