"""Aggregator: fan-in of the specialists' parts into one bundle, with a deadline (D68).

Choreography has no caller waiting for the specialists, so something must decide when a run is
"done": here, a stateful join keyed by ``correlation_id`` (code, not an agent). ``run.planned``
tells it WHICH parts to expect and the deadline; each ``advice.completed`` is a part. It emits
``advice.bundle.ready`` when

* every expected part has arrived (``reason = "complete"``), or
* the deadline has passed (``reason = "deadline"``): whatever arrived goes on, flagged partial,
  so a slow or dead specialist can never stall a run, and the run still ends in a result.

A FAILED part counts as arrived (it will not get better by waiting) but is reported as missing.
Properties the tests pin down:

* order independence: a part may arrive before its plan (different topics);
* idempotence: redelivered events and a second closer produce the same bundle event id, so the
  consumer downstream drops the duplicate; one closer wins a short Redis lock;
* zero trust: a part from a specialist the plan did not name is ignored;
* a late part (after the run closed) is dropped, never published again.

The Kafka-native alternative (Quix Streams state in changelog topics, D4) was not spiked; this is
the documented Redis-state fallback. Its cost: state lives in Redis (no persistence locally), so a
Redis restart loses open runs, which the deadline of the DLQ'd retries cannot recover.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from vitalmesh.events.v1 import payloads_pb2
from vitalmesh_common.envelope import EventMeta, Tier, utcnow
from vitalmesh_common.processor import EventSerde, Publisher
from vitalmesh_common.run import (
    Risk,
    Specialist,
    risk_from_proto,
    risk_to_proto,
    specialist_from_proto,
    specialist_to_proto,
)
from vitalmesh_common.topics import ADVICE_BUNDLES

from vitalmesh_agents.events import POST_BUNDLE_BUDGET_S, derived_id, emit, next_meta
from vitalmesh_agents.runstore import PlanRecord, RunStore

OUTPUT_TYPE = "advice.bundle.ready"
_ORDER = {s: i for i, s in enumerate(Specialist)}


def bundle_event_id(correlation_id: str) -> str:
    return derived_id(correlation_id, "advice.bundle.ready")


class Aggregator:
    def __init__(
        self,
        store: RunStore,
        serde: EventSerde,
        publisher: Publisher,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._store = store
        self._serde = serde
        self._publisher = publisher
        self._clock = clock

    # ------------------------------------------------------------------- handler
    def handle(self, meta: EventMeta, payload: Any) -> None:
        cid = meta.correlation_id
        if meta.type == "run.planned":
            if meta.tier is Tier.T1:
                return  # answered from the cohort cache: no fan-in to wait for
            if meta.deadline_at is None:
                raise ValueError("run.planned without a deadline: the run could never time out")
            expected = tuple(
                sorted(
                    {specialist_from_proto(s).value for s in payload.specialists},
                    key=lambda v: _ORDER[Specialist(v)],
                )
            )
            self._store.save_plan(
                PlanRecord(
                    correlation_id=cid,
                    plan_event_id=meta.event_id,
                    user_id=meta.user_id,
                    household_id=meta.household_id,
                    trace_id=meta.trace_id,
                    tier=meta.tier.value if meta.tier else None,
                    question=payload.question,
                    risk=risk_from_proto(payload.risk).value,
                    expected=expected,
                    deadline_ms=int(meta.deadline_at.timestamp() * 1000),
                )
            )
        elif meta.type == "advice.completed":
            kind = specialist_from_proto(payload.specialist)
            self._store.save_part(cid, kind.value, payload.SerializeToString())
        else:
            return
        self._close_if_ready(cid, force=False)

    # ------------------------------------------------------------------- deadline
    def sweep(self) -> int:
        """Close every run whose deadline has passed. Called on a timer; returns how many."""
        closed = 0
        now_ms = int(self._clock().timestamp() * 1000)
        for cid in self._store.due(now_ms):
            if self._close_if_ready(cid, force=True):
                closed += 1
        return closed

    # ------------------------------------------------------------------- closing
    def _close_if_ready(self, cid: str, *, force: bool) -> bool:
        if self._store.is_closed(cid):
            return False
        snap = self._store.snapshot(cid)
        plan = snap.plan
        if plan is None:
            if force:
                self._store.finish(cid)  # a deadline entry with no plan left: nothing to emit
            return False  # parts without a plan wait for it: order across topics is not defined
        arrived = {Specialist(k) for k in snap.parts if k in plan.expected}
        expected = {Specialist(v) for v in plan.expected}
        if not force and arrived < expected:
            return False
        if not self._store.try_close(cid):
            return False
        try:
            self._publish(plan, snap.parts, deadline=force and arrived < expected)
        except BaseException:
            self._store.release_close(cid)  # the next event or sweep tries again
            raise
        self._store.finish(cid)
        return True

    def _publish(self, plan: PlanRecord, raw_parts: dict[str, bytes], *, deadline: bool) -> None:
        parts: list[payloads_pb2.AdviceCompleted] = []
        missing: list[Specialist] = []
        for value in plan.expected:
            kind = Specialist(value)
            data = raw_parts.get(value)
            if data is None:
                missing.append(kind)
                continue
            part = payloads_pb2.AdviceCompleted()
            part.ParseFromString(data)
            parts.append(part)
            if part.status == payloads_pb2.ADVICE_FAILED:
                missing.append(kind)

        bundle = payloads_pb2.AdviceBundleReady(
            question=plan.question,
            risk=risk_to_proto(Risk(plan.risk)),
            partial=bool(missing),
            reason="deadline" if deadline else "complete",
        )
        bundle.parts.extend(parts)
        bundle.missing.extend(specialist_to_proto(s) for s in missing)

        now = self._clock()
        # The envelope of the plan, reconstructed: the bundle belongs to the same run and user.
        parent = EventMeta(
            event_id=plan.plan_event_id,
            correlation_id=plan.correlation_id,
            user_id=plan.user_id,
            household_id=plan.household_id,
            type="run.planned",
            occurred_at=now,
            trace_id=plan.trace_id,
            tier=Tier(plan.tier) if plan.tier else None,
        )
        out = next_meta(
            parent,
            OUTPUT_TYPE,
            "advice.bundle.ready",
            now,
            deadline_at=now + timedelta(seconds=POST_BUNDLE_BUDGET_S),
            event_id=bundle_event_id(plan.correlation_id),
        )
        emit(self._serde, self._publisher, ADVICE_BUNDLES, out, bundle)
        self._publisher.flush()  # durable before the run is marked closed
