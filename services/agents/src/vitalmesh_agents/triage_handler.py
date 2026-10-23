"""Triage handler: ``question.asked`` -> plan -> ``run.planned`` (D68).

No LLM. The plan is a pure function (``triage.plan_run``) of the question, the user's stored
facts and the load. The handler adds what only an event can carry: the tier in the envelope and
the run deadline, after which the specialists' fan-out is considered failed and the Aggregator
closes the run with whatever arrived.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

from vitalmesh.events.v1 import payloads_pb2
from vitalmesh_common.envelope import EventMeta, Tier, utcnow
from vitalmesh_common.processor import EventSerde, Handler, Publisher
from vitalmesh_common.run import risk_to_proto, specialist_to_proto
from vitalmesh_common.topics import RUNS_PLANNED
from vitalmesh_stores import LabStore, ProfileStore

from vitalmesh_agents.budget import LoadMonitor, TierCounter
from vitalmesh_agents.cohort import plan_key
from vitalmesh_agents.cohortstore import CohortStore
from vitalmesh_agents.events import emit, next_meta
from vitalmesh_agents.guardian_gate import T3_RULES_VERSION
from vitalmesh_agents.triage import Load, Plan, plan_run, signals_for

INPUT_TYPE = "question.asked"
OUTPUT_TYPE = "run.planned"
DEFAULT_RUN_DEADLINE_S = 90.0


class UnknownUser(LookupError):
    """No profile yet. Retryable (it may be seeded moments later), then dead-lettered."""


def to_payload(question: str, plan: Plan) -> payloads_pb2.RunPlanned:
    msg = payloads_pb2.RunPlanned(
        question=question,
        risk=risk_to_proto(plan.risk),
        max_model_calls=plan.max_model_calls,
        degraded=plan.degraded,
    )
    msg.specialists.extend(specialist_to_proto(s) for s in plan.specialists)
    msg.rule_ids.extend(plan.rule_ids)
    return msg


def make_triage_handler(
    profiles: ProfileStore,
    labs: LabStore,
    serde: EventSerde,
    publisher: Publisher,
    load: Callable[[], Load] = lambda: Load.NORMAL,
    clock: Callable[[], datetime] = utcnow,
    run_deadline_s: float = DEFAULT_RUN_DEADLINE_S,
    cohort: CohortStore | None = None,
    monitor: LoadMonitor | None = None,
    tiers: TierCounter | None = None,
) -> Handler:
    def handle(meta: EventMeta, payload: Any) -> None:
        if meta.type != INPUT_TYPE:
            return
        record = profiles.get(meta.user_id)
        if record is None:
            raise UnknownUser(f"no profile for user {meta.user_id!r}")
        profile = record.profile
        signals = signals_for(profile.conditions, profile.medications, labs.list(meta.user_id))
        plan = plan_run(payload.question, signals, monitor.load() if monitor else load())
        if cohort is not None:
            key = plan_key(profile, signals, payload.question, plan, T3_RULES_VERSION)
            if key is not None:
                if cohort.get(key) is not None:
                    # the answer comes from the cohort cache: no specialist and no model call
                    plan = replace(
                        plan, tier=Tier.T1, rule_ids=(*plan.rule_ids, "cache.cohort_hit")
                    )
                else:
                    cohort.count("miss")

        if tiers is not None:
            tiers.add(plan.tier.value)
        if monitor is not None and plan.tier in (Tier.T2, Tier.T3):
            monitor.record()  # only model-backed runs are load
        now = clock()
        out_meta = next_meta(
            meta,
            OUTPUT_TYPE,
            "run.planned",
            now,
            tier=plan.tier,
            deadline_at=now + timedelta(seconds=run_deadline_s),
        )
        emit(serde, publisher, RUNS_PLANNED, out_meta, to_payload(payload.question, plan))
        publisher.flush()  # the offset commits only after the plan is durable

    return handle
